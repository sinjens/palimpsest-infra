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
JSONL = full-fidelity transcript, sharded by day. Claude Code's own
transcript file is cumulative (one file per session, growing forever), so
mirroring it wholesale made every day's shard a full re-snapshot — a
7-week session cost ~3 GB that way. Instead we track a per-session byte
offset (~/.claude/palimpsest/.jsonl-state/) and append only the lines
added since the previous Stop. Concatenating a session's shards in date
order reconstructs the sanitized transcript. Each shard opens with a
`palimpsest-shard` marker line: kind=delta continues the stream from
sourceOffset; kind=full restarts it from byte 0 (source transcript was
rewritten — rewind/fork — or the session predates offset tracking) and
supersedes ALL of the session's earlier shards. Bookkeeping entry types
(file-history-snapshot rewind checkpoints, permission-mode, ... — see
_BOOKKEEPING_ENTRY_TYPES) are dropped in every log_tool_calls mode.

Both files pass through the same write-time redaction pass as a
belt-and-suspenders complement to gitleaks on push.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path


def _augment_path_for_gitleaks() -> None:
    """Prepend common gitleaks install locations to PATH so git pre-commit
    hooks can find the binary even when this process inherited a stale PATH
    (classic case: Claude Code was running before `winget install gitleaks`).
    No-op if none of the candidate locations exist on this system.
    """
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
# Per-session JSONL shard state: how many bytes of the source transcript
# earlier shards already carry, plus a fingerprint of the tail of that
# prefix so we notice when Claude Code rewrites the file under us.
_JSONL_STATE_DIR = Path.home() / ".claude" / "palimpsest" / ".jsonl-state"
# Bytes hashed immediately before the consumed offset for that check.
_JSONL_FP_WINDOW = 4096
# Transcript entry types that are session bookkeeping, not conversation —
# dropped from shards in every log_tool_calls mode. file-history-snapshot
# alone (Claude Code's rewind checkpoints: full copies of every edited
# file) measured 61% of a large real session's bytes. Unknown future
# types pass through: fidelity-first.
_BOOKKEEPING_ENTRY_TYPES = {
    "file-history-snapshot",
    "queue-operation",
    "permission-mode",
    "agent-name",
    "bridge-session",
    "mode",
    "last-prompt",
    "custom-title",
    "progress",
}
# Where auto-sync (and other) errors get appended.
_ERRORS_LOG = Path.home() / ".claude" / "palimpsest" / "errors.log"
# Hard timeout on network ops so a flaky connection never hangs the hook.
_PULL_TIMEOUT_SECONDS = 5
_COMMIT_TIMEOUT_SECONDS = 10
# Detached push can afford more slack than the synchronous pull-on-prompt:
# it runs out-of-band, so generosity here doesn't delay Claude's next turn.
_PUSH_TIMEOUT_SECONDS = 15
_REBASE_TIMEOUT_SECONDS = 15
# Per-brain push lockfiles so concurrent sessions don't fire overlapping
# pushes to the same brain repo and contend on .git/refs/*.lock (or stack
# up pack-objects children that pin the CPU). The detached push child
# owns releasing its lock; the parent only holds it across its brief
# commit window.
_LOCKS_DIR = Path.home() / ".claude" / "palimpsest" / ".locks"
# A legitimate push holds its lock for seconds; a lock older than this is
# a leftover from a dead process and gets reaped regardless of what the
# PID probe says (PIDs get recycled, especially on Windows). This is what
# un-wedges pushes if a stale lock ever survives the liveness check.
_LOCK_STALE_SECONDS = 900
# Cap git's pack-objects parallelism on the hook's pushes; on a 12-core
# box a default-fanout pack peaked at ~50% total CPU during the push.
# Applied via `-c`, not git config, so manual pushes from the user's
# shell keep their normal speed.
_PUSH_PACK_THREADS = 2
_PUSH_PACK_COMPRESSION = 1


def _no_window_kwargs() -> dict:
    """subprocess kwargs that suppress console-window popups on Windows.
    Git is a console app; without CREATE_NO_WINDOW, Windows creates a brief
    console flash every time we spawn it from a background hook. No-op on
    POSIX."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}

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
    # Payment / messaging provider keys
    (re.compile(r"(?:sk|rk|pk)_live_[0-9A-Za-z]{24,}"),                   "[REDACTED:STRIPE_KEY]"),
    (re.compile(r"SK[0-9a-f]{32}"),                                       "[REDACTED:TWILIO_KEY]"),
    (re.compile(r"SG\.[A-Za-z0-9_-]{16,32}\.[A-Za-z0-9_-]{32,}"),          "[REDACTED:SENDGRID_KEY]"),
    (re.compile(r"hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"), "[REDACTED:SLACK_WEBHOOK]"),
    # Cloud provider / infra tokens
    (re.compile(r"dop_v1_[a-f0-9]{64}"),                                  "[REDACTED:DIGITALOCEAN_TOKEN]"),
    (re.compile(r"dapi[a-f0-9]{32}"),                                     "[REDACTED:DATABRICKS_TOKEN]"),
    # Entra (Azure AD) app client secret — 40 chars, alphanumerics + ~._-.
    # Microsoft's generator almost always emits a `~` within positions 2-8,
    # which is a cheap-and-distinctive fingerprint vs random strings.
    (re.compile(r"\b[A-Za-z0-9]{2,7}~[A-Za-z0-9_~.-]{32,37}\b"),           "[REDACTED:ENTRA_SECRET]"),
    # Context-aware catch for CLI output that echoes secrets structurally
    # even when the value shape doesn't match a known pattern — e.g. az bot
    # authsetting, az ad app credential reset, az webapp config.
    (re.compile(r'"clientSecret"\s*:\s*"[^"]+"'),                          '"clientSecret": "[REDACTED:CLIENT_SECRET]"'),
    (re.compile(r'"value"\s*:\s*"[^"]+"(?=[^}]*"key"\s*:\s*"clientSecret")'),
                                                                          '"value": "[REDACTED:CLIENT_SECRET]"'),
    # Basic-auth or connection-string URLs with embedded credentials.
    # Matches scheme://user:password@host  (common for db / repo URLs).
    (re.compile(r"\b(?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?|ftp|ssh|git)://[^\s:/@]+:[^\s/@]+@[A-Za-z0-9.\-]+"),
                                                                         r"[REDACTED:URL_WITH_CREDS]"),
]

_SCOPE_PREFIXES = {
    "[work]":    "work",
    "[private]": "private",
    "[both]":    "both",
    "[nolog]":   "nolog",
}


def main() -> int:
    """Outer wrapper: never propagate an exception to Claude Code. Any
    crash inside the hook becomes a logged error in palimpsest/errors.log
    and a clean exit 0. The visible "non-blocking status code: Traceback"
    UI message that Claude Code shows on hook failure was the symptom of
    an unhandled exception escaping this function — we now swallow them
    here and capture the trace so it can be diagnosed offline."""
    try:
        return _main()
    except BaseException as exc:
        import traceback as _tb
        try:
            _log_error(
                f"hook crashed in mode={sys.argv[1] if len(sys.argv) > 1 else '?'}: "
                f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}"
            )
        except BaseException:
            pass
        return 0


def _main() -> int:
    if len(sys.argv) < 2:
        return 0
    mode = sys.argv[1]

    # `push-retry` is a self-dispatch from _commit_and_push_async. It runs in
    # a detached child process, takes a brain path on argv, and consumes no
    # stdin — handle it before the payload read below would block on empty
    # stdin.
    if mode == "push-retry":
        if len(sys.argv) >= 3 and sys.argv[2]:
            lock_arg = sys.argv[3] if len(sys.argv) >= 4 and sys.argv[3] else None
            _push_with_rebase_retry(Path(sys.argv[2]), Path(lock_arg) if lock_arg else None)
        return 0

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
    jsonl_chunk: str | None = None
    jsonl_kind = "delta"
    jsonl_start = 0
    jsonl_state_update: tuple[int, str] | None = None
    if mode == "stop" and transcript_path:
        text = _last_assistant_text(Path(transcript_path))
        if text.strip():
            claude_text = _redact(text)
        delta = _transcript_delta(Path(transcript_path), session_id)
        if delta is not None:
            raw_chunk, jsonl_start, new_offset, new_fp, jsonl_kind = delta
            jsonl_chunk = _redact(_sanitize_jsonl(raw_chunk, config.get("log_tool_calls", "none")))
            jsonl_state_update = (new_offset, new_fp)
    # State only advances after the shard write succeeds (or there was
    # nothing left to write post-filtering); on write failure the same
    # source bytes are retried next Stop.
    jsonl_write_ok = jsonl_chunk is not None and jsonl_chunk == ""

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

        # errors="replace" handles lone Unicode surrogates that sometimes
        # ride in via the Windows clipboard when pasting between Claude
        # sessions (e.g. \udc9d). The default errors="strict" raises
        # UnicodeEncodeError mid-write, crashing the hook. Replacement
        # is lossy but bounded — original chars remain in the .jsonl
        # transcript Claude Code maintains separately.
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
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

        if jsonl_chunk:
            shard = log_path.with_suffix(".jsonl")
            try:
                if jsonl_kind == "full":
                    # Restarted stream: overwrite today's shard (its earlier
                    # deltas are part of the superseded history too).
                    shard.write_text(
                        _shard_marker("full", 0, session_id) + jsonl_chunk,
                        encoding="utf-8", errors="replace",
                    )
                else:
                    new_shard = not shard.exists()
                    with shard.open("a", encoding="utf-8", errors="replace") as jf:
                        if new_shard:
                            jf.write(_shard_marker("delta", jsonl_start, session_id))
                        jf.write(jsonl_chunk)
                jsonl_write_ok = True
            except OSError:
                pass  # MD already written; JSONL shard is nice-to-have

    if jsonl_state_update is not None and jsonl_write_ok:
        _write_jsonl_state(session_id, *jsonl_state_update)

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
        "log_tool_calls": data.get("log_tool_calls", "none"),
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
                **_no_window_kwargs(),
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


def _is_pid_alive(pid: int) -> bool:
    """True if a process with this PID currently exists.

    POSIX: `os.kill(pid, 0)` is the standard existence probe. Windows:
    os.kill has NO probe semantics — any non-CTRL signal value is routed
    to TerminateProcess (i.e. it kills instead of probing), and on this
    ARM64 Python the failed-open path raises SystemError besides — so we
    probe via OpenProcess + GetExitCodeProcess instead. Returns False on
    any error; the lock-age TTL in _acquire_push_lock is the backstop for
    a probe that's wrong (e.g. PID recycled onto an unrelated process)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def _acquire_push_lock(brain_path: Path) -> Path | None:
    """Try to take a per-brain push lockfile. Returns the path on success,
    or None if another push for this brain is already in flight (or on FS
    error — callers treat both the same: skip this session's push and let
    the next one catch up). Stale locks (holder PID dead) are reaped."""
    try:
        _LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    lock = _LOCKS_DIR / (_sanitize(brain_path.name) + ".push.lock")
    for _ in range(2):
        try:
            # 'x' = exclusive create; atomic enough on both NTFS and POSIX
            # for this purpose.
            with lock.open("x", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            return lock
        except FileExistsError:
            try:
                held = int((lock.read_text(encoding="utf-8") or "0").strip() or "0")
            except (OSError, ValueError):
                held = 0
            stale = False
            try:
                stale = (time.time() - lock.stat().st_mtime) > _LOCK_STALE_SECONDS
            except OSError:
                pass
            if not stale and _is_pid_alive(held):
                return None  # in flight — next session catches up
            try:
                lock.unlink()
            except OSError:
                return None
    return None


def _release_push_lock(lock_path: Path | None) -> None:
    """Best-effort lock release; safe with None or a missing file."""
    if not lock_path:
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


def _commit_and_push_async(brain_path: Path, commit_msg: str) -> None:
    """Stage + commit synchronously (fast), then fire a detached push.

    Commit is synchronous because it's a local-only op (~100ms) and we
    want to know right away whether anything was actually staged. Push is
    detached so the hook can return while the network round-trip finishes
    in the background — Claude never waits for git over the wire.

    Per-brain push lock: if another push for this brain is already in
    flight (typically a previous session whose pack-objects is still
    running), skip the whole commit+push. The next session's `git add -A`
    picks up everything since the last successful run, so nothing is lost.
    """
    if not (brain_path / ".git").exists():
        return

    lock = _acquire_push_lock(brain_path)
    if lock is None:
        return  # another push is in flight (or no lock available); skip

    try:
        subprocess.run(
            ["git", "-C", str(brain_path), "add", "-A"],
            check=True, capture_output=True, text=True,
            timeout=_COMMIT_TIMEOUT_SECONDS,
            **_no_window_kwargs(),
        )
        diff = subprocess.run(
            ["git", "-C", str(brain_path), "diff", "--cached", "--quiet"],
            capture_output=True, timeout=_COMMIT_TIMEOUT_SECONDS,
            **_no_window_kwargs(),
        )
        if diff.returncode == 0:
            _release_push_lock(lock)
            return  # nothing staged, skip the push
        subprocess.run(
            ["git", "-C", str(brain_path), "commit", "-m", commit_msg],
            check=True, capture_output=True, text=True,
            timeout=_COMMIT_TIMEOUT_SECONDS,
            **_no_window_kwargs(),
        )
    except subprocess.CalledProcessError as e:
        _log_error(f"commit failed [{brain_path.name}]: {(e.stderr or '').strip()[:500]}")
        _release_push_lock(lock)
        return
    except (subprocess.TimeoutExpired, OSError) as e:
        _log_error(f"commit error [{brain_path.name}]: {e}")
        _release_push_lock(lock)
        return

    # Detach push so the network delay doesn't block the hook. The child
    # re-enters this script in `push-retry` mode, which handles the
    # non-fast-forward case (another device or a parallel session raced us)
    # by pulling --rebase --autostash once and retrying the push. Without
    # this, resumed sessions silently pile up unpushable local commits
    # because the pull-on-first-prompt marker prevents a re-pull.
    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # CREATE_NO_WINDOW suppresses the console-window flash every time
        # this fires. DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP leaves
        # git (a console app) to create its own console on spawn; that's
        # the "why is a git window popping up?" bug the user reported.
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "push-retry", str(brain_path), str(lock)],
            **popen_kwargs,
        )
    except OSError as e:
        _log_error(f"push spawn failed [{brain_path.name}]: {e}")
        _release_push_lock(lock)


def _push_with_rebase_retry(brain_path: Path, lock_path: Path | None = None) -> None:
    """Push `brain_path` to its remote; on non-fast-forward rejection,
    pull --rebase --autostash and retry the push exactly once.

    Runs out-of-band in a detached child (spawned by _commit_and_push_async)
    so network round-trips never delay the hook's parent process. Holds the
    per-brain push lock for its lifetime so concurrent sessions skip rather
    than stack. Fails silently to errors.log — the next Stop will retry, and
    `git status` remains the source of truth for the user.
    """
    # Take ownership of the lock from the (already-exited) parent: rewrite
    # the PID so a concurrent session's alive-check sees this long-lived
    # child, not the short-lived parent that just exited.
    if lock_path:
        try:
            lock_path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass

    try:
        if not (brain_path / ".git").exists():
            return

        # Cap pack-objects threads + compression on the hook's pushes only,
        # via `-c`, so manual pushes from the user's shell keep their
        # normal performance.
        push_cfg = [
            "-c", f"pack.threads={_PUSH_PACK_THREADS}",
            "-c", f"pack.compression={_PUSH_PACK_COMPRESSION}",
        ]

        def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *push_cfg, "-C", str(brain_path), *args],
                capture_output=True, text=True, timeout=timeout,
                **_no_window_kwargs(),
            )

        try:
            first = _run(["push"], _PUSH_TIMEOUT_SECONDS)
            if first.returncode == 0:
                return

            combined = (first.stderr or "") + (first.stdout or "")
            diverged = any(
                marker in combined
                for marker in ("non-fast-forward", "rejected", "fetch first", "Updates were rejected")
            )
            if not diverged:
                _log_error(f"push failed [{brain_path.name}]: {combined.strip()[:500]}")
                return

            rebase = _run(["pull", "--rebase", "--autostash"], _REBASE_TIMEOUT_SECONDS)
            if rebase.returncode != 0:
                _log_error(
                    f"push-retry rebase failed [{brain_path.name}]: "
                    f"{(rebase.stderr or rebase.stdout or '').strip()[:500]}"
                )
                return

            retry = _run(["push"], _PUSH_TIMEOUT_SECONDS)
            if retry.returncode != 0:
                _log_error(
                    f"push-retry push failed [{brain_path.name}]: "
                    f"{(retry.stderr or retry.stdout or '').strip()[:500]}"
                )
        except subprocess.TimeoutExpired:
            _log_error(f"push-retry timeout [{brain_path.name}]")
        except OSError as e:
            _log_error(f"push-retry error [{brain_path.name}]: {e}")
    finally:
        _release_push_lock(lock_path)


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

    for marker_dir in (_NUDGED_DIR, _JSONL_STATE_DIR):
        try:
            marker = marker_dir / _sanitize(session_id)
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


def _read_jsonl_state(session_id: str) -> tuple[int, str] | None:
    """Return (consumed_offset, fingerprint_hex) for this session, or None
    when the session has no shard state yet (or the file is unreadable —
    treated the same: restart with a full shard)."""
    try:
        data = json.loads(
            (_JSONL_STATE_DIR / _sanitize(session_id)).read_text(encoding="utf-8")
        )
        return int(data["offset"]), str(data["fp"])
    except (OSError, ValueError, KeyError):
        return None


def _write_jsonl_state(session_id: str, offset: int, fp: str) -> None:
    try:
        _JSONL_STATE_DIR.mkdir(parents=True, exist_ok=True)
        (_JSONL_STATE_DIR / _sanitize(session_id)).write_text(
            json.dumps({"offset": offset, "fp": fp}), encoding="utf-8"
        )
    except OSError:
        pass  # worst case: next Stop re-emits a full shard


def _shard_marker(kind: str, source_offset: int, session_id: str) -> str:
    """First line of every shard file. Lets any consumer reconstruct or
    validate without out-of-band state: concatenate a session's shards in
    date order, restarting from scratch at each kind=full marker."""
    return json.dumps({
        "type": "palimpsest-shard",
        "kind": kind,
        "sourceOffset": source_offset,
        "sessionId": session_id,
    }) + "\n"


def _transcript_delta(
    transcript: Path, session_id: str
) -> tuple[str, int, int, str, str] | None:
    """Read only what the source transcript gained since the last Stop.

    Returns (chunk, start, new_offset, new_fp, kind), or None when there is
    nothing new (or the file is unreadable). kind="delta" when the
    previously-consumed prefix is intact — chunk holds just the new lines,
    starting at byte `start`. kind="full" when the prefix check failed
    (transcript rewritten by rewind/fork, state lost, or a session that
    predates offset tracking) — chunk restarts from byte 0 and the caller
    writes a shard that supersedes all earlier ones.

    Only complete lines (ending in \\n) are consumed; a trailing partial
    line — Claude Code may be mid-write — is left for the next Stop. All
    offsets are byte positions in the source file; the fingerprint covers
    the last _JSONL_FP_WINDOW bytes of the consumed prefix.
    """
    state = _read_jsonl_state(session_id)
    try:
        with transcript.open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            kind, start = "full", 0
            if state is not None:
                offset, fp = state
                if 0 < offset <= size:
                    w = min(_JSONL_FP_WINDOW, offset)
                    f.seek(offset - w)
                    if hashlib.sha1(f.read(w)).hexdigest() == fp:
                        kind, start = "delta", offset
            f.seek(start)
            data = f.read()
            nl = data.rfind(b"\n")
            if nl < 0:
                return None  # no complete new line yet
            data = data[: nl + 1]
            new_offset = start + len(data)
            w = min(_JSONL_FP_WINDOW, new_offset)
            f.seek(new_offset - w)
            new_fp = hashlib.sha1(f.read(w)).hexdigest()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace"), start, new_offset, new_fp, kind


def _sanitize_jsonl(raw: str, mode: str) -> str:
    """Filter tool_use / tool_result content from the raw transcript
    according to `log_tool_calls` mode.

        "none"    (default) — strip tool_use and tool_result blocks entirely.
                              ExitPlanMode plans are always preserved.
        "minimal"           — keep tool name + correlation id, replace input
                              and output with a [STRIPPED] placeholder.
        "full"              — keep all tool content. Wider secrets surface;
                              gitleaks + expanded patterns become the backstop.

    Bookkeeping entry types (_BOOKKEEPING_ENTRY_TYPES) are dropped in EVERY
    mode — `log_tool_calls` governs tool content, and rewind checkpoints /
    UI state aren't that. They're also the bulk of the bytes.

    This operates on the byte-level JSONL before `_redact` so we never run
    regex over tool content we've decided to drop anyway.
    """
    if mode not in ("none", "minimal", "full"):
        mode = "none"  # safe default on typos

    out_lines: list[str] = []
    for line in raw.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if not stripped.strip():
            out_lines.append(line)
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            out_lines.append(line)  # pass through malformed
            continue

        if entry.get("type") in _BOOKKEEPING_ENTRY_TYPES:
            continue

        if mode != "full":
            message = entry.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    message["content"] = _filter_content_blocks(content, mode)

        # Preserve original line ending style
        newline = "\n" if line.endswith("\n") else ""
        out_lines.append(json.dumps(entry) + newline)

    return "".join(out_lines)


def _filter_content_blocks(blocks: list, mode: str) -> list:
    """Apply the mode's filtering to a content-block array."""
    filtered: list = []
    for block in blocks:
        if not isinstance(block, dict):
            filtered.append(block)
            continue
        btype = block.get("type")
        if btype == "tool_use":
            # ExitPlanMode plans are user-visible — keep them verbatim
            # regardless of mode (matches the MD handling).
            if block.get("name") == "ExitPlanMode":
                filtered.append(block)
            elif mode == "minimal":
                filtered.append({
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": "[STRIPPED]",
                })
            # mode == "none": drop entirely
        elif btype == "tool_result":
            if mode == "minimal":
                filtered.append({
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "content": "[STRIPPED]",
                })
            # mode == "none": drop entirely
        else:
            filtered.append(block)
    return filtered


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
