"""Promote palimpsest articles from this employee's private brains into
the shared team/company brain (`palimpsest-work-shared`).

Scans two source brains for articles with `share: true` in frontmatter:

  1. `palimpsest-work/palimpsest/`  — always, resolved relative to this file
  2. `palimpsest-both/palimpsest/`  — if PALIMPSEST_BOTH_BRAIN env var is set

Copies flagged articles to `palimpsest-work-shared/palimpsest/` preserving
their relative paths. Articles that had `share: true` in a previous pass
but have now been unflagged (or the source deleted) are removed from the
shared repo so it stays an exact projection of currently-flagged content.

If multiple employees run their own promote concurrently, the push to
`palimpsest-work-shared` may hit a non-fast-forward rejection. The script
auto-recovers via one `pull --rebase --autostash` and a single retry.

No human review gate by design — the supervisor's `share: true` decision
is trusted, and the raw logs never transit here regardless.

Usage:
    python compile/promote.py                 # run, commit, push
    python compile/promote.py --dry-run       # print plan, no writes
    python compile/promote.py --no-commit     # write files, skip git
    python compile/promote.py --no-push       # commit locally, don't push

Environment:
    PALIMPSEST_BOTH_BRAIN    optional path to the both-scope brain
    PALIMPSEST_WORK_SHARED   optional path to the shared brain (default:
                             sibling of this work brain)
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Force UTF-8 on stdout/stderr so Sonnet responses containing non-ASCII
# (arrows, em-dashes, accented characters, etc.) don't crash `print()` on
# Windows terminals that default to cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def _augment_path_for_gitleaks() -> None:
    """Prepend common gitleaks install locations to PATH so the brain's
    pre-commit hook can find the binary. Classic case: Claude Code was
    running when `winget install gitleaks` ran, so its cached PATH never
    picked up the new binary. This script inherits that stale PATH unless
    we fix it here."""
    path = os.environ.get("PATH", "")
    sep = ";" if os.name == "nt" else ":"
    candidates: list[str] = []
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        winget_pkgs = Path(localappdata) / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.exists():
            for d in winget_pkgs.iterdir():
                if d.is_dir() and "Gitleaks" in d.name and (d / "gitleaks.exe").exists():
                    candidates.append(str(d))
    for p in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        if os.path.isdir(p):
            candidates.append(p)
    added = [c for c in candidates if c not in path.split(sep)]
    if added:
        os.environ["PATH"] = sep.join(added + [path])


_augment_path_for_gitleaks()

THIS = Path(__file__).resolve()
WORK_BRAIN = THIS.parent.parent
WORK_PAL = WORK_BRAIN / "palimpsest"

_both_env = os.environ.get("PALIMPSEST_BOTH_BRAIN")
BOTH_BRAIN: Path | None = Path(_both_env) if _both_env else None
BOTH_PAL: Path | None = (BOTH_BRAIN / "palimpsest") if BOTH_BRAIN else None

SHARED_BRAIN = Path(os.environ.get(
    "PALIMPSEST_WORK_SHARED",
    str(WORK_BRAIN.parent / "palimpsest-work-shared"),
))
SHARED_PAL = SHARED_BRAIN / "palimpsest"

_GENERATED_TOP_LEVEL = {"index.md", "CHANGELOG.md"}

_PUSH_TIMEOUT_SECONDS = 15
_REBASE_TIMEOUT_SECONDS = 15


def _no_window_kwargs() -> dict:
    """Suppress console flashes on Windows when spawning git."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


# ----- share flag + source enumeration --------------------------------------


def has_share_flag(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return False
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if k.strip() == "share":
            return v.strip().lower() in ("true", "yes", "1")
    return False


def _articles_under(palimpsest_root: Path) -> list[Path]:
    if not palimpsest_root.exists():
        return []
    return sorted(
        p for p in palimpsest_root.rglob("*.md")
        if not (p.parent == palimpsest_root and p.name in _GENERATED_TOP_LEVEL)
    )


def list_shareable_articles() -> list[tuple[Path, Path]]:
    """Return (source_article, source_palimpsest_root) pairs for every
    article flagged `share: true` across configured source brains."""
    sources: list[tuple[Path, Path]] = []
    for root in filter(None, (WORK_PAL, BOTH_PAL)):
        for a in _articles_under(root):
            if has_share_flag(a):
                sources.append((a, root))
    return sources


def list_shared_articles() -> list[Path]:
    return _articles_under(SHARED_PAL)


# ----- git helpers (with Windows-console suppression + rebase-retry) --------


def _git(repo: Path, args: list[str], *, check: bool = False, timeout: int | None = None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
        check=check, timeout=timeout,
        **_no_window_kwargs(),
    )


def git_commit(repo: Path, message: str, *, paths: list[str]) -> bool:
    _git(repo, ["add", *paths], check=True)
    diff = _git(repo, ["diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return False
    _git(repo, ["commit", "-m", message], check=True)
    return True


def git_push_with_rebase_retry(repo: Path) -> bool:
    """Push; on non-fast-forward rejection, pull --rebase --autostash and
    retry exactly once. Mirrors the behaviour of palimpsest-log.py's hook.
    Returns True if the final push succeeded."""
    first = _git(repo, ["push"], timeout=_PUSH_TIMEOUT_SECONDS)
    if first.returncode == 0:
        return True

    combined = (first.stderr or "") + (first.stdout or "")
    diverged = any(
        marker in combined
        for marker in ("non-fast-forward", "rejected", "fetch first", "Updates were rejected")
    )
    if not diverged:
        print(f"push failed (non-divergence): {combined.strip()[:500]}", file=sys.stderr)
        return False

    print("push rejected (non-fast-forward); rebasing once and retrying...")
    rebase = _git(repo, ["pull", "--rebase", "--autostash"], timeout=_REBASE_TIMEOUT_SECONDS)
    if rebase.returncode != 0:
        print(f"rebase failed: {(rebase.stderr or rebase.stdout or '').strip()[:500]}",
              file=sys.stderr)
        return False

    retry = _git(repo, ["push"], timeout=_PUSH_TIMEOUT_SECONDS)
    if retry.returncode != 0:
        print(f"retry push failed: {(retry.stderr or retry.stdout or '').strip()[:500]}",
              file=sys.stderr)
        return False
    return True


# ----- shared brain index ---------------------------------------------------


def regenerate_shared_index() -> None:
    articles = list_shared_articles()
    now = datetime.now()
    lines = [
        "# palimpsest-work-shared — company-brain index",
        "",
        f"_Last regenerated: {now:%Y-%m-%d %H:%M:%S}_",
        f"_Article count: {len(articles)}_",
        "",
        "*Maintained by `promote.py` across all contributor brains. Articles land here by auto-copy from contributors' `palimpsest-work/` and `palimpsest-both/` brains when their frontmatter sets `share: true`. Edit via a source brain, not here.*",
        "",
    ]
    if not articles:
        lines.append("_Empty — nothing has been promoted yet._")
    else:
        by_category: dict[str, list] = {}
        for a in articles:
            rel = a.relative_to(SHARED_PAL).as_posix()
            category = rel.split("/", 1)[0] if "/" in rel else "uncategorized"
            by_category.setdefault(category, []).append(a)
        for category in sorted(by_category):
            lines.append(f"## {category}")
            lines.append("")
            for a in sorted(by_category[category], key=lambda p: p.stem):
                rel = a.relative_to(SHARED_PAL).as_posix()
                content = a.read_text(encoding="utf-8")
                m = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
                title = m.group(1).strip() if m else a.stem
                m = re.search(r"^ttl:\s*(\S+)", content, re.MULTILINE)
                ttl = m.group(1).strip() if m else "?"
                m = re.search(r"^updated:\s*(\S+)", content, re.MULTILINE)
                updated = m.group(1).strip() if m else "?"
                lines.append(f"- [{title}]({rel}) — ttl:{ttl}, updated:{updated}")
            lines.append("")
    (SHARED_PAL / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----- main ------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    if not SHARED_BRAIN.exists():
        print(
            f"ERROR: palimpsest-work-shared not found at {SHARED_BRAIN}. "
            "Clone the shared repo there or set PALIMPSEST_WORK_SHARED=<path>.",
            file=sys.stderr,
        )
        return 2

    print(
        f"promote: scanning palimpsest-work"
        + (" + palimpsest-both" if BOTH_PAL else "")
        + f" -> palimpsest-work-shared at {SHARED_BRAIN}"
    )

    shareable = list_shareable_articles()
    target_relpaths: set[str] = set()
    to_copy: list[tuple[Path, Path]] = []
    for src, root in shareable:
        rel = src.relative_to(root)
        dst = SHARED_PAL / rel
        target_relpaths.add(rel.as_posix())
        to_copy.append((src, dst))

    shared_existing = {p.relative_to(SHARED_PAL).as_posix() for p in list_shared_articles()}
    to_delete = sorted(shared_existing - target_relpaths)

    print(
        f"  {len(to_copy)} to share, {len(to_delete)} to remove, "
        f"{len(shared_existing)} currently in shared."
    )

    if args.dry_run:
        for src, dst in to_copy:
            print(f"  COPY   {src.name}  -> {dst.relative_to(SHARED_BRAIN)}")
        for rel in to_delete:
            print(f"  DELETE {rel} (no longer flagged)")
        return 0

    for src, dst in to_copy:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    for rel in to_delete:
        p = SHARED_PAL / rel
        try:
            p.unlink()
        except OSError:
            pass
        parent = p.parent
        while parent != SHARED_PAL and parent.exists() and not any(parent.iterdir()):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    regenerate_shared_index()

    if args.no_commit:
        print("(--no-commit: changes not committed)")
        return 0

    summary = f"promote: {len(to_copy)} added/updated, {len(to_delete)} removed"
    if not git_commit(SHARED_BRAIN, summary, paths=["palimpsest"]):
        print("shared brain: no changes to commit.")
        return 0

    if args.no_push:
        print(f"shared brain: committed ({summary}); not pushing (--no-push).")
        return 0

    if git_push_with_rebase_retry(SHARED_BRAIN):
        print(f"shared brain: pushed ({summary}).")
    else:
        print("shared brain: push FAILED after retry; leave for next run.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
