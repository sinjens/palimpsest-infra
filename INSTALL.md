# Palimpsest — Install Instructions

This file is intended to be read and executed by **Claude Code on behalf of the user**. Ask Claude:

> Read `INSTALL.md` in this repo and set up Palimpsest on this machine.

Claude walks through the steps below, asking for paths and confirmations when needed. Nothing destructive happens without confirmation.

---

## Step 0 — Clone the infra, then pin to a tagged release

The user has already cloned this repo. Note its absolute path (call it `$INFRA`). You'll need it when wiring up hooks.

**Critical: pin to a tag, don't follow `main`.** The hook script executes on every user prompt + every response. Running directly off `main` means any future push to the infra repo runs on the user's machine. Instead:

```bash
cd "$INFRA"
git fetch --tags
# Pick the latest release (check https://github.com/sinjens/palimpsest-infra/releases)
git checkout v0.3.1   # or the latest tag — see the repo's Releases page
```

When you later want to adopt a newer version, the user should review the release notes first, then re-run this checkout with the new tag. That explicit step is the only defence adopters have against a compromised or accidentally-bad commit in the public infra repo.

## Step 1 — Check prerequisites

```bash
python --version   # must be >= 3.11
git --version
```

If **Python** is older than 3.11, install:

- **Windows**: `winget install Python.Python.3.11`
- **macOS**: `brew install python@3.11`
- **Linux**: use host package manager

If **git** is missing, install:

- **Windows**: `winget install Git.Git`
- **macOS**: `brew install git` (or ships with Xcode Command Line Tools)
- **Linux**: use host package manager

**Required** (pre-commit secret scanning — gitleaks is the brain repos' main backstop against novel secret formats the hook's regex doesn't catch): **gitleaks**:

- **Windows**: `winget install gitleaks.gitleaks`
- **macOS**: `brew install gitleaks`
- **Linux**: use host package manager (or fetch a release binary from <https://github.com/gitleaks/gitleaks>)

If you install Palimpsest without gitleaks, a single tool-output containing a hand-rolled token or an unlisted secret format can get committed and pushed before anyone notices. Don't skip Step 7.

## Step 1.5 — Know the opt-out

Users can exclude any session from logging with `/rename [nolog] <title>`. The hook writes nothing new AND purges any prior entries for that session. Worth telling the user about this during install so they know it exists.

## Step 2 — Decide brain layout

Ask the user where each brain should live. Typical defaults:

| Brain | Purpose | Example path |
|---|---|---|
| personal | `scope=private` sessions | `~/source/palimpsest-personal` |
| work | `scope=work` sessions | `~/source/palimpsest-work` |
| both | `scope=both` sessions (infra, meta) | `~/source/palimpsest-both` |
| unclassified | `scope=unset` staging | `~/source/palimpsest-unclassified` |

A user may want the work brain under a work-specific repos directory, or separate roots for each. Ask.

**Also ask: does the user already have GitHub remotes for these brains from another device?** Brains are the sync boundary between devices, so most installs after the first will want to clone existing remotes rather than create fresh repos. Typical naming is `github.com/<user>/palimpsest-{personal,work,both}` (private). Collect the URLs up front — Step 3 branches on the answer.

## Step 3 — Create or clone each brain repo

The flow splits depending on whether the brain already exists as a GitHub remote:

### Step 3a — First device ever (no remote yet)

For each brain path `$BRAIN` that does not yet exist anywhere:

```bash
mkdir -p "$BRAIN"
cd "$BRAIN" && git init -b main
```

Don't commit anything yet — that happens after Step 6 verification. Step 8 covers adding a remote once the user is ready.

### Step 3b — Subsequent device (remote already exists)

For each brain that already has a GitHub remote, clone it at the chosen path instead of `git init`:

```bash
git clone git@github.com:<you>/<brain-name>.git "$BRAIN"
# or the https URL if the user prefers
```

This brings down the existing history from other devices. The clone already has `origin` configured, so Step 8 is a no-op for these brains.

### Either path

`palimpsest-unclassified` does not need to be a git repo (it's transient staging), but can be if desired. It is never pushed — no remote, on any device.

## Step 4 — Create `~/.claude/palimpsest/config.toml`

```bash
mkdir -p ~/.claude/palimpsest
cp "$INFRA/config/config.example.toml" ~/.claude/palimpsest/config.toml
```

Then edit `~/.claude/palimpsest/config.toml`:

- `[brains]` — set each absolute path to what the user chose in Step 2
- `[[rule]]` entries — adjust CWD substrings to the user's actual directory conventions (ask them about their top-level project folders and what scope each should have)

## Step 5 — Register hooks in `~/.claude/settings.json`

Add two hooks calling the script directly from the infra repo. Use an absolute path — on Windows prefer the forward-slash form (e.g. `/c/Users/foo/...`) for cross-shell compatibility.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python <INFRA>/hooks/palimpsest-log.py prompt"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python <INFRA>/hooks/palimpsest-log.py stop"
          }
        ]
      }
    ]
  }
}
```

Replace `<INFRA>` with the repo path. **Preserve any existing `hooks`, `permissions`, or other settings** — merge, don't overwrite.

## Step 6 — Verify

Fire a synthetic Stop payload at the script and confirm a file appears in the expected location:

```bash
TRANSCRIPT=$(mktemp --suffix=.jsonl)
cat > "$TRANSCRIPT" <<'EOF'
{"type":"custom-title","customTitle":"Install verification","sessionId":"install-verify"}
{"type":"user","message":{"content":"hi"}}
{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}
EOF

echo "{\"session_id\":\"install-verify\",\"transcript_path\":\"$TRANSCRIPT\",\"cwd\":\"$HOME\"}" \
  | python "$INFRA/hooks/palimpsest-log.py" stop
```

Then look under each brain's `raw/logs/<today>/` and `palimpsest-unclassified/<today>/` to confirm a `install-verify` pair was created. Clean it up afterward.

Start a fresh Claude Code session and verify a real file lands where expected after one prompt + response.

## Step 7 — Required: pre-commit gitleaks on each brain repo

**Runs on every device**, whether you created the brain in 3a or cloned it in 3b. Git hooks in `.git/hooks/` aren't versioned and therefore don't come down with `git clone` — each machine has to install them locally.

For every brain repo `$BRAIN`, add a pre-commit hook that blocks commits containing any secret format gitleaks recognises. This is the backstop for anything the hook's regex doesn't catch — skipping it is how a stray hand-rolled token ends up in the remote.

**Two pieces per brain:**

**(a)** The pre-commit hook script (local to each device, not versioned):

```bash
cat > "$BRAIN/.git/hooks/pre-commit" <<'EOF'
#!/usr/bin/env bash
if ! command -v gitleaks >/dev/null 2>&1; then
  echo "pre-commit: gitleaks not on PATH — aborting commit." >&2
  echo "Install with your platform's package manager (winget/brew/apt) and retry." >&2
  exit 1
fi
exec gitleaks protect --staged --no-banner
EOF
chmod +x "$BRAIN/.git/hooks/pre-commit"
```

**(b)** A `.gitleaks.toml` at the brain's root (**versioned** — ships with the repo so teammates share the same allowlist). Copy it from this infra repo's template:

```bash
cp "$INFRA/config/gitleaks.toml" "$BRAIN/.gitleaks.toml"
```

The template:
- Skips `raw/logs/*.jsonl` files (they're machine-generated transcript mirrors, already passed through the hook's write-time redaction; re-scanning them with gitleaks is high-noise / low-signal).
- Allowlists SSH public-key fingerprints (`SHA256:<43 base64 chars>`) — they look high-entropy but are public by design.

Customise per-brain if you end up with local false positives (hand-rolled token formats that gitleaks mistakes for something real).

Git hooks in `.git/hooks/` are **not** versioned — each device needs to install (a) separately. The `.gitleaks.toml` from (b) **is** versioned and travels with the repo.

This is the belt to the suspenders. The logger's write-time redaction catches known patterns; gitleaks catches the long tail (and anything a pattern update adds after your logger was installed).

## Step 8 — Add a remote (only if you created the brain in Step 3a)

Skip this step for any brain you cloned in 3b — `origin` is already set.

For brains freshly `git init`'d in 3a, add a private GitHub remote once the user is ready to push. On subsequent devices they'll fall through Step 3b's `git clone` path instead.

```bash
cd "$BRAIN"
git remote add origin git@github.com:<you>/<brain-name>.git
# commit + push when ready
```

Keep brains **private** — they contain raw conversation data, even after redaction.

---

## What happens after install

- Every Claude Code session's prompts + responses land in the matching brain, date-sharded, redacted.
- Unclassified sessions stage in `palimpsest-unclassified/` and auto-migrate on classification.
- The `.jsonl` sidecar preserves full fidelity for a future compile loop (palimpsest curation layer — not part of v1).
