"""Log Claude Code conversation turns, routed by session scope.

Usage: python palimpsest-log.py prompt   (from UserPromptSubmit hook)
       python palimpsest-log.py stop     (from Stop hook)

Reads the hook payload JSON from stdin. Writes two files per session per
date folder (HHMMSS_<title>_<session_id>.md + .jsonl), inside the brain(s)
matching the session's resolved scope.

Scope resolution (first match wins):
    1. Title prefix   /rename [work]|[private]|[both]|[nolog] <rest>  — stripped
    2. CWD rule match — substring match on `cwd` from the hook payload
    3. Fallback       — config.default_scope (typically "unset")

Routing:
    scope=private  →  brains.private  / raw / logs / YYYY-MM-DD / ...
    scope=work     →  brains.work     / raw / logs / YYYY-MM-DD / ...
    scope=both     →  brains.both     / raw / logs / YYYY-MM-DD / ...
    scope=unset    →  palimpsest-unclassified (unrouted staging)
    scope=nolog    →  nothing is written; any prior entries for this
                       session are purged from all brains and staging

Config lives at ~/.claude/palimpsest/config.toml; missing/broken config
degrades safely to the fallback path. The unclassified fallback defaults
to `~/source/palimpsest-unclassified` and can be overridden via the
optional `unclassified_path = "..."` key in config.toml.

MD = condensed human-readable view (user prompts + Claude text + plans).
JSONL = byte-for-byte copy of the Claude Code transcript (full fidelity
for the downstream compile loop). Both pass through the same write-time
redaction pass as a belt-and-suspenders complement to gitleaks on push.
"""
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

# Staging folder for unset-scope sessions. Files live here until the user
# classifies the session (via /rename [work]|[private]|[both]), at which
# point they're migrated into the matching brain on the next hook firing.
# Default is `~/source/palimpsest-unclassified`; override via config.toml.
_UNCLASSIFIED_DEFAULT = Path.home() / "source" / "palimpsest-unclassified"
CONFIG_PATH = Path.home() / ".claude" / "palimpsest" / "config.toml"
# Per-session markers so we don't spam the classification nudge every turn.
_NUDGED_DIR = Path.home() / ".claude" / "palimpsest" / ".nudged"
# Per-session markers so we only pull each brain once per session.
_PULLED_DIR = Path.home() / ".claude" / "palimpsest" / ".pulled"
# Where auto-sync (and other) errors get appended.
_ERRORS_LOG = Path.home() / ".claude" / "palimpsest" / "errors.log"
# Hard timeout on network ops so a flaky connection never hangs the hook.
_PULL_TIMEOUT_SECONDS = 5
_COMMIT_TIMEOUT_SECONDS = 10

# Characters Windows filenames can't contain
_ILLEGAL_CHARS = '<>:"/\\|?*'

# Write-time secret redaction — belt-and-suspenders with gitleaks on the
# pre-commit side. Ordered: specific patterns first so generic fallbacks
# (like the `bearer <anything>` catch-all) don't steal matches from more
# informative ones (like JWT).
_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"),                               "[REDACTED:GOOGLE_API_KEY]"),
    (re.compile(r"sk-ant-[a-zA-Z0-9_-]{30,}"),                           "[REDACTED:ANTHROPIC_KEY]"),
    (re.compile(r"sk-(?:proj|user|svcacct)-[a-zA-Z0-9_-]{20,}"),          "[REDACTED:API_KEY]"),
    (re.compile(r"sk-[a-zA-Z0-9]{32,}"),                                 "[REDACTED:API_KEY]"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"),                                "[REDACTED:GITHUB_PAT]"),
    (re.compile(r"gho_[A-Za-z0-9]{30,}"),                                "[REDACTED:GITHUB_OAUTH]"),
    (re.compile(r"ghs_[A-Za-z0-9]{30,}"),                                "[REDACTED:GITHUB_APP]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),                        "[REDACTED:GITHUB_PAT]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),                        "[REDACTED:SLACK_TOKEN]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"),                                    "[REDACTED:AWS_KEY_ID]"),
    (re.compile(r"eyJ[A-Za-z0-9_=-]+\.eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_./+=-]+"), "[REDACTED:JWT]"),
    (re.compile(
        r"-----BEGIN (?:RSA |OPENSSH |DSA |EC |ENCRYPTED |)PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END [A-Z ]*PRIVATE KEY-----"
    ), "[REDACTED:PRIVATE_KEY]"),
    (re.compile(r"DefaultEndpointsProtocol=https;AccountName=[^;]+;AccountKey=[A-Za-z0-9+/=]+[^;\s\"]*"),
                                                                         "[REDACTED:AZURE_CONN_STRING]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),               "bearer [REDACTED:TOKEN]"),
]

_SCOPE_PREFIXES = {
    "[work]":    "work",
    "[private]": "private",
    "[both]":    "both",
    "[nolog]":   "nolog",
}


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    mode = sys.argv[1]

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    session_id = payload.get("session_id", "unknown")
    transcript_path = payload.get("transcript_path")
    cwd = payload.get("cwd", "")

    config = _load_config()
    raw_title = _custom_title(Path(transcript_path)) if transcript_path else None
    scope, title = _resolve_scope(raw_title, cwd, config)

    # [nolog] is an opt-out: purge any prior entries for this session and
    # write nothing new. Never warn, never nudge, never sync — total silence.
    if scope == "nolog":
        _purge_session(session_id, config)
        return 0

    # On the first prompt of a session, pull each brain once so local state
    # reflects any work pushed from another device. Skipped for unset (no
    # target brain yet) and silently degrades on network failure.
    if mode == "prompt" and _auto_sync_enabled(config) and scope != "unset":
        _pull_brains(config, session_id)

    target_roots = _target_log_roots(scope, config)

    # Pre-compute the shared content once so we don't re-read the transcript
    # per brain target when scope=both.
    claude_text: str | None = None
    jsonl_content: str | None = None
    if mode == "stop" and transcript_path:
        text = _last_assistant_text(Path(transcript_path))
        if text.strip():
            claude_text = _redact(text)
        try:
            jsonl_content = _redact(Path(transcript_path).read_text(encoding="utf-8"))
        except OSError:
            pass

    prompt_text: str | None = None
    if mode == "prompt":
        prompt_text = _redact(payload.get("prompt", ""))
        # Nudge Claude to ask the user for classification, once per session,
        # when the session is still unset and we're at a user prompt (the
        # only hook stage where stdout gets injected as prompt context).
        if scope == "unset":
            _nudge_unclassified(session_id)

    # When a session has just been classified, bring any prior entries in
    # palimpsest-unclassified over to the matching brain so the full
    # session history lives in one place.
    if scope != "unset":
        for logs_root in target_roots:
            _migrate_unclassified(session_id, logs_root, config)

    now = datetime.now()

    for logs_root in target_roots:
        log_path = _resolve_log_path(logs_root, session_id, title)
        new_file = not log_path.exists()

        with log_path.open("a", encoding="utf-8") as f:
            if new_file:
                header_name = title if title else session_id
                f.write(f"# Claude session: {header_name}\n\n")
                f.write(f"_session_id: {session_id}_  \n")
                f.write(f"_scope: {scope}_  \n")
                f.write(f"_Started: {now:%Y-%m-%d %H:%M:%S}_\n\n")

            if prompt_text is not None:
                f.write(f"\n## [{now:%H:%M:%S}] User\n\n{prompt_text}\n\n")
            elif claude_text is not None:
                f.write(f"### [{now:%H:%M:%S}] Claude\n\n{claude_text}\n\n---\n\n")

        if jsonl_content is not None:
            try:
                log_path.with_suffix(".jsonl").write_text(jsonl_content, encoding="utf-8")
            except OSError:
                pass  # MD already written; JSONL mirror is nice-to-have

    # After writing, fire an async commit+push back to the brain's remote.
    # Skipped for unset scope (fallback folder isn't a git repo) and nolog
    # (handled earlier). Failures never block Claude — they land in errors.log.
    if mode == "stop" and _auto_sync_enabled(config) and scope != "unset":
        commit_msg = f"log: {title or session_id} ({scope})"
        for logs_root in target_roots:
            brain_root = logs_root.parent.parent  # <brain>/raw/logs → <brain>
            _commit_and_push_async(brain_root, commit_msg)

    return 0


def _load_config() -> dict:
    """Load the TOML config, returning an empty-but-valid shape on any error."""
    fallback = {"default_scope": "unset", "rule": [], "brains": {}}
    if not CONFIG_PATH.exists():
        return fallback
    try:
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return fallback
    return {
        "default_scope": data.get("default_scope", "unset"),
        "rule": data.get("rule", []) or [],
        "brains": data.get("brains", {}) or {},
        "unclassified_path": data.get("unclassified_path"),
        "auto_sync": data.get("auto_sync", True),
    }


def _resolve_scope(title: str | None, cwd: str, config: dict) -> tuple[str, str | None]:
    """Decide the session scope.

    Title-prefix override first (strips the prefix from the returned title).
    Then CWD substring match. Finally the config default.
    """
    clean_title = title
    if title:
        lowered = title.strip()
        for marker, scope_name in _SCOPE_PREFIXES.items():
            if lowered.lower().startswith(marker):
                stripped = lowered[len(marker):].strip()
                return scope_name, stripped or None
        clean_title = lowered  # harmless normalisation

    normalised = cwd.replace("\\", "/")
    for rule in config.get("rule", []):
        needle = rule.get("match", "")
        if needle and needle in normalised:
            return rule.get("scope", "unset"), clean_title

    return config.get("default_scope", "unset"), clean_title


def _nudge_unclassified(session_id: str) -> None:
    """Emit a context note asking Claude to prompt the user for scope.
    Idempotent per session — the marker file stops repeat nudges."""
    try:
        _NUDGED_DIR.mkdir(parents=True, exist_ok=True)
        marker = _NUDGED_DIR / session_id
        if marker.exists():
            return
        marker.touch()
    except OSError:
        return
    sys.stdout.write(
        "<palimpsest-note>\n"
        "This session's scope is unclassified — the CWD doesn't match any "
        "rule in ~/.claude/palimpsest/config.toml, so logs are landing in "
        "palimpsest-unclassified/. Ask the user to classify: "
        "\"Is this session work, private, or both?\" Once they tell you, "
        "suggest `/rename [work] <title>`, `/rename [private] <title>`, or "
        "`/rename [both] <title>`. The logger will then auto-migrate any "
        "prior entries from this session into the correct brain.\n"
        "</palimpsest-note>\n"
    )
    sys.stdout.flush()


def _migrate_unclassified(session_id: str, dest_logs_root: Path, config: dict) -> None:
    """Move all files for this session from palimpsest-unclassified into the
    destination brain, preserving the date-folder structure. No-op when the
    unclassified folder is missing or empty for this session."""
    unclassified = _unclassified_path(config)
    if not unclassified.exists():
        return
    for date_dir in unclassified.iterdir():
        if not date_dir.is_dir():
            continue
        matches = list(date_dir.glob(f"*_{session_id}.md")) + list(date_dir.glob(f"*_{session_id}.jsonl"))
        if not matches:
            continue
        target_dir = dest_logs_root / date_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)
        for src in matches:
            target = target_dir / src.name
            try:
                if not target.exists():
                    src.rename(target)
            except OSError:
                pass  # best effort; file stays in unclassified
        # Tidy up an empty date folder after the move
        try:
            if not any(date_dir.iterdir()):
                date_dir.rmdir()
        except OSError:
            pass


def _target_log_roots(scope: str, config: dict) -> list[Path]:
    """Return the logs-root directory to write this session's files into.

    Each scope (private, work, both) maps to a single dedicated brain; the
    "both" brain holds dual-scope content with its own compilation strategy
    rather than duplicating across the other two. Brains lay out as
    `<brain>/raw/logs/YYYY-MM-DD/...`. The unset / fallback path is flat:
    `<unclassified>/YYYY-MM-DD/...`.
    """
    brains = config.get("brains", {})
    brain_path = brains.get(scope)
    if brain_path:
        return [Path(brain_path) / "raw" / "logs"]
    return [_unclassified_path(config)]


def _unclassified_path(config: dict) -> Path:
    """Where scope=unset sessions stage. Config override wins; otherwise
    default to ~/source/palimpsest-unclassified."""
    override = config.get("unclassified_path")
    return Path(override) if override else _UNCLASSIFIED_DEFAULT


def _auto_sync_enabled(config: dict) -> bool:
    """Auto-sync defaults on. Disable with `auto_sync = false` in config."""
    return bool(config.get("auto_sync", True))


def _log_error(message: str) -> None:
    """Append a timestamped error to palimpsest's errors.log. Silent on
    failure — logging about logging-failures shouldn't itself fail loudly."""
    try:
        _ERRORS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _ERRORS_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError:
        pass


def _pull_brains(config: dict, session_id: str) -> None:
    """Once per session, pull --rebase --autostash on each configured brain
    repo so the local clone reflects any work pushed from another device.
    Times out fast and fails open — network errors never block the hook."""
    marker = _PULLED_DIR / session_id
    try:
        _PULLED_DIR.mkdir(parents=True, exist_ok=True)
        if marker.exists():
            return
        # Mark first; a crash during pull shouldn't trigger retry storms.
        marker.touch()
    except OSError:
        return

    for brain_name, brain_path in (config.get("brains") or {}).items():
        if not brain_path:
            continue
        p = Path(brain_path)
        if not (p / ".git").exists():
            continue  # not a git repo, skip quietly
        try:
            result = subprocess.run(
                ["git", "-C", str(p), "pull", "--rebase", "--autostash"],
                capture_output=True, text=True, timeout=_PULL_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                _log_error(
                    f"pull failed [{brain_name}]: "
                    f"{(result.stderr or result.stdout or '').strip()[:500]}"
                )
        except subprocess.TimeoutExpired:
            _log_error(f"pull timeout [{brain_name}] after {_PULL_TIMEOUT_SECONDS}s")
        except OSError as e:
            _log_error(f"pull error [{brain_name}]: {e}")


def _commit_and_push_async(brain_path: Path, commit_msg: str) -> None:
    """Stage + commit synchronously (fast), then fire a detached push.

    Commit is synchronous because it's a local-only op (~100ms) and we
    want to know right away whether anything was actually staged. Push is
    detached so the hook can return while the network round-trip finishes
    in the background — Claude never waits for git over the wire.
    """
    if not (brain_path / ".git").exists():
        return
    try:
        subprocess.run(
            ["git", "-C", str(brain_path), "add", "-A"],
            check=True, capture_output=True, text=True,
            timeout=_COMMIT_TIMEOUT_SECONDS,
        )
        diff = subprocess.run(
            ["git", "-C", str(brain_path), "diff", "--cached", "--quiet"],
            capture_output=True, timeout=_COMMIT_TIMEOUT_SECONDS,
        )
        if diff.returncode == 0:
            return  # nothing staged, skip the push
        subprocess.run(
            ["git", "-C", str(brain_path), "commit", "-m", commit_msg],
            check=True, capture_output=True, text=True,
            timeout=_COMMIT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as e:
        _log_error(f"commit failed [{brain_path.name}]: {(e.stderr or '').strip()[:500]}")
        return
    except (subprocess.TimeoutExpired, OSError) as e:
        _log_error(f"commit error [{brain_path.name}]: {e}")
        return

    # Detach push so the network delay doesn't block the hook.
    popen_kwargs = {
        "cwd": str(brain_path),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True
    try:
        subprocess.Popen(["git", "push"], **popen_kwargs)
    except OSError as e:
        _log_error(f"push spawn failed [{brain_path.name}]: {e}")


def _purge_session(session_id: str, config: dict) -> None:
    """Remove any previously-written files for this session from every brain
    and the unclassified staging area, and clear the nudge marker. Called
    when the session is marked [nolog] so no trace remains."""
    roots: list[Path] = [_unclassified_path(config)]
    for brain_path in (config.get("brains") or {}).values():
        if brain_path:
            roots.append(Path(brain_path) / "raw" / "logs")

    for root in roots:
        if not root.exists():
            continue
        for date_dir in list(root.iterdir()):
            if not date_dir.is_dir():
                continue
            for f in list(date_dir.glob(f"*_{session_id}.*")):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                if not any(date_dir.iterdir()):
                    date_dir.rmdir()
            except OSError:
                pass

    try:
        marker = _NUDGED_DIR / session_id
        if marker.exists():
            marker.unlink()
    except OSError:
        pass


def _resolve_log_path(logs_root: Path, session_id: str, title: str | None) -> Path:
    """Return this session's MD log path inside today's date folder.

    Looks for any existing session file (MD or JSONL) to preserve the
    original HHMMSS prefix across any title-change rename. Both files get
    renamed together so the pair stays in lockstep.
    """
    d = logs_root / datetime.now().strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)

    matches = list(d.glob(f"*_{session_id}.md")) + list(d.glob(f"*_{session_id}.jsonl"))

    if matches:
        name = matches[0].name
        if len(name) >= 7 and name[6] == "_" and name[:6].isdigit():
            hhmmss = name[:6]
        else:
            hhmmss = datetime.now().strftime("%H%M%S")
    else:
        hhmmss = datetime.now().strftime("%H%M%S")

    if title:
        stem = f"{hhmmss}_{_sanitize(title)}_{session_id}"
    else:
        stem = f"{hhmmss}_{session_id}"

    for existing in matches:
        desired = d / f"{stem}{existing.suffix}"
        if existing != desired:
            try:
                if not desired.exists():
                    existing.rename(desired)
            except OSError:
                pass  # best effort; keep the original file if rename fails

    return d / f"{stem}.md"


def _custom_title(transcript: Path) -> str | None:
    """Return the latest custom title set for this session, if any."""
    if not transcript.exists():
        return None
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    latest: str | None = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") == "custom-title":
            title = entry.get("customTitle")
            if title:
                latest = title
    return latest


def _sanitize(name: str) -> str:
    """Make a string safe to use as a Windows filename."""
    name = name.strip().strip('"').strip("'").strip()
    for ch in _ILLEGAL_CHARS:
        name = name.replace(ch, "-")
    name = name.strip(" -.")
    name = " ".join(name.split())
    return name or "session"


def _redact(text: str) -> str:
    """Run all redaction patterns. Safe for JSON text — replacement tokens
    contain no JSON-reserved characters."""
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _last_assistant_text(transcript: Path) -> str:
    """Return the concatenated text blocks of the last assistant turn.

    Claude Code splits a single assistant response across multiple JSONL
    entries (one per content block — `thinking`, `text`, `tool_use`). A
    "turn" is every assistant entry since the last real user message. We
    collect all `text` blocks plus any `ExitPlanMode` tool_use plans from
    main-session (non-sidechain) assistant entries in that range.
    """
    if not transcript.exists():
        return ""
    try:
        raw = transcript.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    entries = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    last_user_idx = -1
    for i, e in enumerate(entries):
        if e.get("type") != "user" or e.get("isSidechain"):
            continue
        if _is_real_user_message(e):
            last_user_idx = i

    if last_user_idx < 0:
        return ""

    texts: list[str] = []
    for e in entries[last_user_idx + 1:]:
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        content = e.get("message", {}).get("content", [])
        if isinstance(content, str):
            if content:
                texts.append(content)
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    texts.append(text)
            elif btype == "tool_use" and block.get("name") == "ExitPlanMode":
                plan = block.get("input", {}).get("plan", "")
                if plan:
                    texts.append(f"**[Plan]**\n\n{plan}")

    return "\n\n".join(texts)


def _is_real_user_message(entry: dict) -> bool:
    """Distinguish a prompt from a tool_result user entry."""
    content = entry.get("message", {}).get("content", [])
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and block.get("text", "").strip()
        for block in content
    )


if __name__ == "__main__":
    sys.exit(main())
