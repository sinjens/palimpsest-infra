"""Palimpsest nightly runner — deterministic orchestration of the compile loop.

Owns the entire nightly sequence so no agent ever has to improvise it:

    for each brain (personal, work, both):  pull -> compile -> supervise
                                            -> receipt -> push
    then:                                   promote (work -> shared)

Born from the 2026-06-06 incident: the routine prompt described this sequence
in prose and the executing agent parallelized the brains as background tasks,
one of which never completed — silently skipping a brain. This script replaces
agent discretion with a fixed pipeline, real exit codes, per-step timeouts,
and a committed receipt (compile/last-run.json) per brain so "did last night
fully run?" is answerable from any checkout with one file read.

Usage:
    python compile/nightly.py                # full run
    python compile/nightly.py --dry-run     # cascade --dry-run; no commits/pushes
    python compile/nightly.py --work PATH   # override discovery per brain

Brain discovery, first hit wins per brain:
    1. CLI flags (--personal/--work/--both/--shared)
    2. env vars (PALIMPSEST_PERSONAL / _WORK / _BOTH / _WORK_SHARED)
    3. ~/.claude/palimpsest/config.toml [brains] (developer machines;
       note config key `private` maps to the personal brain)
    4. siblings of this script's own brain checkout (routine containers
       clone all repos into one parent directory)

A brain that isn't found is reported and skipped — never fabricated.
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

THIS_BRAIN = Path(__file__).resolve().parent.parent

# Fixed processing order; promote runs after all three.
BRAIN_ORDER = ["personal", "work", "both"]
SLUGS = {
    "personal": "palimpsest-personal",
    "work": "palimpsest-work",
    "both": "palimpsest-both",
    "shared": "palimpsest-work-shared",
}
ENV_KEYS = {
    "personal": "PALIMPSEST_PERSONAL",
    "work": "PALIMPSEST_WORK",
    "both": "PALIMPSEST_BOTH",
    "shared": "PALIMPSEST_WORK_SHARED",
}
CONFIG_PATH = Path.home() / ".claude" / "palimpsest" / "config.toml"
# config.toml's [brains] keys use the scope names, not the brain kinds.
CONFIG_KEY = {"personal": "private", "work": "work", "both": "both"}

GIT_TIMEOUT = 180
COMPILE_TIMEOUT = 5400   # backfills are legitimate; one stuck claude call is not
SUPERVISE_TIMEOUT = 1800
PROMOTE_TIMEOUT = 900

RECEIPT_NAME = "last-run.json"  # no leading dot: compile/.gitignore has .last-*.txt


def log(msg: str) -> None:
    print(f"[nightly] {msg}", flush=True)


def is_brain(path: Path, kind: str) -> bool:
    """A usable checkout: git repo, and (for compile brains) has the scripts."""
    if not (path / ".git").exists():
        return False
    if kind == "shared":
        return True
    return (path / "compile" / "main.py").exists()


def discover_brains(args: argparse.Namespace) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for kind in (*BRAIN_ORDER, "shared"):
        # 1. CLI flag
        flag = getattr(args, kind, None)
        if flag:
            p = Path(flag).resolve()
            if is_brain(p, kind):
                found[kind] = p
                continue
            log(f"WARNING: --{kind} {flag} is not a usable checkout; ignoring")
        # 2. env var
        env = os.environ.get(ENV_KEYS[kind], "")
        if env and is_brain(Path(env).resolve(), kind):
            found[kind] = Path(env).resolve()
            continue
        # 3. config.toml (developer machines)
        if kind != "shared" and CONFIG_PATH.exists():
            try:
                import tomllib
                with CONFIG_PATH.open("rb") as f:
                    brains = tomllib.load(f).get("brains", {}) or {}
                cfg = brains.get(CONFIG_KEY[kind], "")
                if cfg and is_brain(Path(cfg).resolve(), kind):
                    found[kind] = Path(cfg).resolve()
                    continue
            except Exception:
                pass
        # 4. sibling of this script's own brain checkout
        sibling = THIS_BRAIN.parent / SLUGS[kind]
        if is_brain(sibling, kind):
            found[kind] = sibling.resolve()
    return found


def run_step(label: str, cmd: list[str], cwd: Path, timeout: int) -> dict:
    """Run one pipeline step in the foreground, streaming its output.
    Returns {rc, seconds}; rc -1 means timeout, -2 means spawn failure."""
    log(f"--- {label}: {' '.join(cmd)}  (cwd={cwd.name}, timeout={timeout}s)")
    t0 = time.monotonic()
    try:
        rc = subprocess.run(cmd, cwd=str(cwd), timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        log(f"--- {label}: TIMEOUT after {timeout}s")
        return {"rc": -1, "seconds": round(time.monotonic() - t0, 1)}
    except OSError as e:
        log(f"--- {label}: spawn failed: {e}")
        return {"rc": -2, "seconds": round(time.monotonic() - t0, 1)}
    secs = round(time.monotonic() - t0, 1)
    log(f"--- {label}: rc={rc} in {secs}s")
    return {"rc": rc, "seconds": secs}


def git(brain: Path, *args: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(brain), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def push_with_retry(brain: Path) -> dict:
    """git push; on a non-fast-forward rejection, pull --rebase once and retry."""
    t0 = time.monotonic()
    try:
        first = git(brain, "push")
        if first.returncode == 0:
            return {"rc": 0, "seconds": round(time.monotonic() - t0, 1)}
        combined = (first.stderr or "") + (first.stdout or "")
        if any(m in combined for m in ("non-fast-forward", "rejected", "fetch first")):
            rebase = git(brain, "pull", "--rebase", "--autostash")
            if rebase.returncode == 0:
                retry = git(brain, "push")
                if retry.returncode == 0:
                    return {"rc": 0, "seconds": round(time.monotonic() - t0, 1)}
                combined = (retry.stderr or "") + (retry.stdout or "")
        log(f"--- push failed: {combined.strip()[:400]}")
        return {"rc": 1, "seconds": round(time.monotonic() - t0, 1)}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "seconds": round(time.monotonic() - t0, 1)}


def write_receipt(brain: Path, receipt: dict, dry_run: bool) -> dict:
    """Write compile/last-run.json, commit it. The commit is unconditional on
    real runs — the receipt's value is its guaranteed presence."""
    if dry_run:
        log(f"--- receipt (dry-run, not committed): {json.dumps(receipt)}")
        return {"rc": 0, "seconds": 0.0}
    t0 = time.monotonic()
    try:
        (brain / "compile" / RECEIPT_NAME).write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
        git(brain, "add", f"compile/{RECEIPT_NAME}")
        msg = "nightly: receipt {} ({})".format(
            receipt["date"],
            " ".join(f"{k}={v['rc']}" for k, v in receipt["steps"].items()),
        )
        commit = git(brain, "commit", "-m", msg)
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
            log(f"--- receipt commit failed: {(commit.stderr or commit.stdout)[:300]}")
            return {"rc": 1, "seconds": round(time.monotonic() - t0, 1)}
        return {"rc": 0, "seconds": round(time.monotonic() - t0, 1)}
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"--- receipt failed: {e}")
        return {"rc": 1, "seconds": round(time.monotonic() - t0, 1)}


def process_brain(kind: str, brain: Path, dry_run: bool) -> dict:
    log(f"=== brain: {kind} ({brain})")
    steps: dict[str, dict] = {}
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Pull first; a brain we can't sync gets skipped entirely rather than
    # compiled against a stale or diverged tree.
    t0 = time.monotonic()
    try:
        pull = git(brain, "pull", "--rebase", "--autostash")
        steps["pull"] = {"rc": pull.returncode, "seconds": round(time.monotonic() - t0, 1)}
        if pull.returncode != 0:
            log(f"--- pull failed, skipping {kind}: {(pull.stderr or pull.stdout)[:300]}")
    except subprocess.TimeoutExpired:
        steps["pull"] = {"rc": -1, "seconds": round(time.monotonic() - t0, 1)}
        log(f"--- pull timeout, skipping {kind}")

    if steps["pull"]["rc"] == 0:
        dry = ["--dry-run"] if dry_run else []
        steps["compile"] = run_step(
            f"{kind}/compile", [sys.executable, "compile/main.py", *dry],
            brain, COMPILE_TIMEOUT,
        )
        # Supervise reviews current state; it runs even after a compile
        # failure — partial output is exactly what most needs review.
        steps["supervise"] = run_step(
            f"{kind}/supervise", [sys.executable, "compile/supervise.py", *dry],
            brain, SUPERVISE_TIMEOUT,
        )

    receipt = {
        "schema": 1,
        "date": datetime.now(timezone.utc).date().isoformat(),
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": platform.node(),
        "dry_run": dry_run,
        "steps": steps,
    }
    steps["receipt"] = write_receipt(brain, receipt, dry_run)
    if not dry_run:
        steps["push"] = push_with_retry(brain)
    return steps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="cascade --dry-run to all scripts; no commits, no pushes")
    for kind in (*BRAIN_ORDER, "shared"):
        ap.add_argument(f"--{kind}", help=f"path to the {SLUGS[kind]} checkout")
    args = ap.parse_args()

    brains = discover_brains(args)
    for kind in (*BRAIN_ORDER, "shared"):
        log(f"discovered {kind}: {brains.get(kind, '(not found — will skip)')}")

    results: dict[str, dict] = {}
    for kind in BRAIN_ORDER:
        if kind in brains:
            results[kind] = process_brain(kind, brains[kind], args.dry_run)

    # Promote: work -> shared. promote.py pushes the shared repo itself.
    if "work" in brains and "shared" in brains and "both" in brains:
        env = {**os.environ,
               "PALIMPSEST_BOTH_BRAIN": str(brains["both"]),
               "PALIMPSEST_WORK_SHARED": str(brains["shared"])}
        dry = ["--dry-run"] if args.dry_run else []
        log(f"--- promote: work -> shared  (timeout={PROMOTE_TIMEOUT}s)")
        t0 = time.monotonic()
        try:
            rc = subprocess.run(
                [sys.executable, "compile/promote.py", *dry],
                cwd=str(brains["work"]), env=env, timeout=PROMOTE_TIMEOUT,
            ).returncode
        except (subprocess.TimeoutExpired, OSError):
            rc = -1
        results["promote"] = {"promote": {"rc": rc, "seconds": round(time.monotonic() - t0, 1)}}
        log(f"--- promote: rc={rc}")
    else:
        missing = [k for k in ("work", "shared", "both") if k not in brains]
        log(f"promote skipped (missing checkout(s): {', '.join(missing)})")

    # Final report — this is what the routine agent relays verbatim.
    print("\n================ NIGHTLY REPORT ================")
    failed = False
    for kind, steps in results.items():
        for step, r in steps.items():
            ok = r["rc"] == 0
            failed |= not ok
            print(f"  {kind:<9} {step:<10} rc={r['rc']:<3} {r['seconds']:>7}s  {'OK' if ok else 'FAILED'}")
    for kind in BRAIN_ORDER:
        if kind not in results:
            print(f"  {kind:<9} SKIPPED (checkout not found)")
            failed = True
    print(f"RESULT: {'FAILED' if failed else 'OK'}")
    print("================================================")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
