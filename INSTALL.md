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

## Step 0.5 — First machine, or adding to an existing ecosystem?

Ask the user up front — several later steps branch on the answer:

- **First machine**: no other device has palimpsest brains yet, no GitHub remotes for brains exist, git identity/signing may not be configured anywhere.
- **Subsequent machine**: brains are already on GitHub from another device, and the user already has a git identity (and possibly signing) they want to replicate here.

Where it matters: **Step 1.6** (identity + signing — subsequent machines should mirror the existing identity so the ecosystem's author metadata stays consistent), **Step 3** (subsequent machines clone remotes in 3b; first machines `git init` in 3a), **Step 8** (only applies after 3a).

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

## Step 1.6 — Git identity and commit signing

Brain auto-sync commits run unattended from the hook script. Bad identity here poisons every brain commit on every device; missing signing means you can never turn on "Require signed commits" branch protection on the brain repos without breaking auto-sync. Get this right before any commit fires.

### Check the current state

```bash
git config --global user.name
git config --global user.email
git config --global commit.gpgsign
```

If `user.email` is unset or something like `your@email.com` / `user@example.com`, fix it here. If this is a subsequent machine (per Step 0.5), match what's already on the other device.

### Ask the user

- **Default name + email** for this machine's commits.
- **Per-path overrides?** Common case: work repos under `~/source/work-org/` should use a work email while everything else uses personal. Use git's `includeIf` — see below.
- **Sign commits?** Strongly recommended. Required if the user ever plans to turn on GitHub branch protection "Require signed commits" on the brain repos (auto-sync pushes will fail silently otherwise).
- **Signing mechanism?** Default to SSH signing — simpler than GPG, reuses the same key type that's used for `git push`, no extra tooling on Windows.

### Set default identity

```bash
git config --global user.name  "Firstname Lastname"
git config --global user.email "you@example.com"
```

### Per-path email override (optional)

Example: repos under `~/source/work-org/` use a different email. The `gitdir/i:` prefix is case-insensitive (matters on Windows); paths must use forward slashes and a trailing slash.

```bash
cat > ~/.gitconfig-work <<'EOF'
[user]
	email = you@work.example.com
EOF

git config --global \
  "includeIf.gitdir/i:C:/Users/<you>/source/work-org/.path" \
  "C:/Users/<you>/.gitconfig-work"
```

Verify: from inside a repo under that tree, `git config user.email` should return the work email; from outside, the default.

### SSH signing

Check for an existing key (`ls ~/.ssh/id_ed25519.pub` etc). If none, generate one:

```bash
ssh-keygen -t ed25519 -C "<email> (<machine-label>)" -f ~/.ssh/id_ed25519 -N ""
```

**Passphrase note**: empty passphrase makes auto-sync signing just work unattended. A non-empty passphrase needs an `ssh-agent` holding the unlocked key whenever the hook fires, or signed commits fail silently mid-session. Pick consciously — most palimpsest users want no passphrase and rely on OS file permissions for the key.

Turn on signing:

```bash
git config --global gpg.format ssh
git config --global user.signingkey "C:/Users/<you>/.ssh/id_ed25519.pub"
git config --global commit.gpgsign true
git config --global tag.gpgsign true
```

Optional — `allowed_signers` lets `git log --show-signature` verify signatures locally (GitHub's Verified badge is independent of this file):

```bash
mkdir -p ~/.config/git
cat >> ~/.config/git/allowed_signers <<EOF
<default-email> $(awk '{print $1,$2}' ~/.ssh/id_ed25519.pub)
EOF
# add one line per identity if using per-path overrides
git config --global gpg.ssh.allowedSignersFile "C:/Users/<you>/.config/git/allowed_signers"
```

### Register the pubkey on GitHub — **pause here for the user**

Show them the pubkey:

```bash
cat ~/.ssh/id_ed25519.pub
```

They install it at <https://github.com/settings/keys> as **two separate entries**:

- **Authentication Key** — needed for SSH pushes; skip only if they push exclusively via HTTPS + credential helper.
- **Signing Key** — needed for GitHub to display the Verified badge on signed commits.

GitHub treats auth and signing keys independently. Registering only one leaves the other side broken.

Wait for the user to confirm both entries are in place before moving on. Commits pushed before the key is registered as a Signing Key will show as Unverified forever on GitHub even though the signature itself is valid — GitHub only runs the check once, at push time.

### Subsequent machines

Never copy a private SSH key between machines. Each device generates its own keypair; the user registers each new device's pubkey on GitHub as its own auth + signing pair. Keep the git identity (`user.name`, `user.email`) identical across machines so the brain histories don't fragment by author.

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
- The `.jsonl` sidecar preserves full fidelity for the compile loop.

---

## Step 9 — Compile loop (optional)

The `compile-template/` directory in this infra repo ships the full LLM-synthesis + Opus-supervisor pipeline. Adopters who want curated articles in their brains (not just raw logs) run it per-brain, locally first and on a nightly Anthropic routine once it's tuned.

### Per-brain setup

For each of the user's own brains (personal, work, both):

```bash
# Inside the brain repo
mkdir -p compile/prompts
cp $INFRA/compile-template/main.py       compile/
cp $INFRA/compile-template/supervise.py  compile/
cp $INFRA/compile-template/.gitignore    compile/
# Pick the right scope prompt variant:
cp $INFRA/compile-template/prompts/synthesize.<scope>.md  compile/prompts/synthesize.md
cp $INFRA/compile-template/prompts/supervise.<scope>.md   compile/prompts/supervise.md
echo "2026-04-17" > compile/cursor.txt   # or yesterday's date; pick a recent starting point

# Create the output tree
mkdir -p palimpsest/patterns palimpsest/projects palimpsest/decisions
cat > palimpsest/index.md <<'EOF'
# Palimpsest — curated knowledge index

*Machine-maintained — do not edit.*
EOF
```

For the **work brain specifically**, also copy `promote.py`:

```bash
cp $INFRA/compile-template/promote.py  compile/promote.py
```

### How to run

Each brain's compile is independent:

```bash
cd <brain>
python compile/main.py          # Sonnet synthesis; creates/updates palimpsest/ articles
python compile/supervise.py     # Opus review pass; merge/delete, confirm share: true flags
```

For the work brain, after main+supervise run the promotion:

```bash
cd $WORK_BRAIN
PALIMPSEST_BOTH_BRAIN=$BOTH_BRAIN python compile/promote.py
```

`PALIMPSEST_BOTH_BRAIN` tells promote.py to also scan the both-scope brain for `share: true` articles. `PALIMPSEST_WORK_SHARED` points at the shared repo clone location (defaults to a sibling directory of the work brain).

All three scripts commit locally. `main.py` and `supervise.py` do not push (let auto-sync handle it or push manually). `promote.py` does push, with rebase-retry if another contributor raced in.

---

## Step 10 — Team architecture & the shared company brain (optional)

Palimpsest scales to a team via one shared repo, `palimpsest-work-shared`, that every contributor pushes their promoted content to. Its [README](https://github.com/sinjens/palimpsest-work-shared) covers the invariants.

### The model

- Each contributor owns their own private **personal / work / both** brains. Raw logs stay here, never shared.
- There is **one** `palimpsest-work-shared` repo for the whole team — owned either by one employee (who invites colleagues as collaborators) or by a GitHub org with team access.
- Each contributor runs their own nightly compile + promote. Their `promote.py` pushes `share: true`-flagged articles from their private work + both brains into `palimpsest-work-shared`.
- Concurrent pushes from multiple contributors are safe: `promote.py` retries with `pull --rebase --autostash` on non-fast-forward rejection.

### Onboarding a new team member

1. They clone palimpsest-infra, check out the latest tag, and set up their own three private brain repos (Steps 0–8 above).
2. Grant them access to `palimpsest-work-shared` (GitHub web UI → Settings → Collaborators, or add to the org team).
3. They clone `palimpsest-work-shared` locally (default: sibling of their work brain, otherwise set `PALIMPSEST_WORK_SHARED`).
4. They set up the compile loop (Step 9).
5. They schedule their nightly routine (see below).

Nothing in the per-contributor flow references anyone else's private brains. No raw log ever crosses contributor boundaries.

### GDPR note

Work-brain synthesis and supervisor prompts (`synthesize.work.md`, `supervise.work.md`) explicitly separate **systems from people**:

- *Systems* — customer integrations, configs, data-model quirks, deployment specifics — are retained freely. This is the brain for how customers' systems actually work.
- *People* — names, emails, phone numbers, personal preferences, attribution to individuals — are stripped or rewritten by role ("the customer's team decided X"). The supervisor's priority-1 check is personal-data scrubbing.

Articles promoted to the shared brain inherit this scrubbing. Confirm before shipping widely if your team handles data subject to GDPR or equivalent.

---

## Step 11 — Run the nightly compile on Anthropic's infrastructure (optional)

Once the compile loop runs cleanly locally for a few days, you can move it to a [Claude Code Routine](https://platform.claude.com) so it runs every night without any contributor's machine needing to be on.

That's a separate operational concern from per-machine install, and has enough unknowns (Routines is a research-preview product, several config details are UI-only and not yet publicly documented) that it lives in its own file rather than cluttering this one:

→ See [`ROUTINE.md`](./ROUTINE.md) in this repo.

TL;DR for planning purposes: 1 routine per contributor, ~$0.10–$0.50 per nightly run, counts against plan routine quota (Max: 15/day), each contributor pushes to the same `palimpsest-work-shared` and rebase-retry handles collisions.
