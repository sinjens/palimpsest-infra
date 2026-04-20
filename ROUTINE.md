# Palimpsest — Running the compile loop as a Claude Code Routine

This document is for **operators** deploying the nightly compile loop to [Claude Code Routines](https://platform.claude.com) so it runs on Anthropic's infrastructure instead of a contributor's laptop. If you just want to use Palimpsest on your own machine, you don't need any of this — see `INSTALL.md`.

## Status: research preview

Routines shipped as a research preview in April 2026. The authoring surface is UI-only and the runtime environment has a couple of non-obvious rough edges documented below.

## What the routine does

For each brain repo the contributor owns, the routine runs three things:

1. `python compile/main.py` — Sonnet synthesis: reads the day's raw logs, updates curated articles.
2. `python compile/supervise.py` — Opus review: consolidates, reconciles contradictions, flags `share: true`.
3. `python compile/promote.py` (from `palimpsest-work` only, with `PALIMPSEST_BOTH_BRAIN` set) — copies `share: true` articles from `palimpsest-work` and `palimpsest-both` into the shared company brain.

Each script commits + pushes on its own. The cursor file (`compile/cursor.txt`) makes runs idempotent — re-running on the same day is a no-op.

## Container environment (verified as of 2026-04-20)

The routine runtime ships with:

- `claude` at `/opt/node22/bin/claude` (Claude Code CLI, authenticated as the routine's owner — subprocess calls to `claude -p` just work)
- `python` at `/usr/local/bin/python`
- `git` at `/usr/bin/git`
- `curl`, `tar`, standard GNU utils
- bun, cargo, rustup, npm, gradle (not needed by us)

**Missing, must be added by setup script:**

- `gitleaks` — the brain repos' pre-commit hooks call it and fail closed if absent.

## Container layout

Routines has **two separate initialization phases**:

1. **Cloud container setup** (UI): configure repos to clone. Routines handles the git clone into the container. You do NOT need to put clone URLs or a GitHub PAT in the routine prompt — the UI-configured repo list is cloned before anything else runs.
2. **Setup script** (UI): a shell script that runs after the clones, before the Claude Code prompt starts. This is the place to install missing binaries, pin versions, and set env vars.

**Repo mount path**: not `/workspace/...` — that was an earlier speculative guess that turned out wrong. The actual path is discovered at prompt start; see below.

## Setup script

Paste this into the Routine's "setup script" field. It installs gitleaks pinned to a known version, verifies, and makes it available on PATH for all subsequent steps including the Claude Code session.

```bash
#!/usr/bin/env bash
set -euo pipefail

# Install gitleaks — required by brain repos' pre-commit hook.
# Pinned version; bump when you verify a newer release.
GITLEAKS_VERSION="8.30.1"

arch=$(uname -m)
case "$arch" in
  x86_64)  gl_arch="x64" ;;
  aarch64) gl_arch="arm64" ;;
  *) echo "unsupported arch: $arch" >&2; exit 1 ;;
esac

url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${gl_arch}.tar.gz"
tmp=$(mktemp -d)
curl -sSL "$url" | tar -xz -C "$tmp" gitleaks
install -m 0755 "$tmp/gitleaks" /usr/local/bin/gitleaks
rm -rf "$tmp"

gitleaks version

# Git identity for commits the compile scripts will push.
# Change to the contributor's preferred bot identity.
git config --global user.name  "Palimpsest Bot"
git config --global user.email "palimpsest-bot@example.com"

# Sanity: make sure all our deps are resolvable.
for bin in claude python git gitleaks; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done

echo "setup: ok"
```

If you want signed commits, also mount an SSH signing key as a routine secret and extend the setup script:

```bash
# optional: ssh signing
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo "$SSH_SIGNING_KEY" > ~/.ssh/palimpsest_sign && chmod 600 ~/.ssh/palimpsest_sign
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/palimpsest_sign
git config --global commit.gpgsign true
```

The corresponding public key must be registered as a **signing** key (not just auth) on the GitHub account whose name appears on the commits.

## Routine prompt

Paste this as the prompt body. It uses `git rev-parse --show-toplevel` plus discovery to find wherever Routines put the clones — don't hardcode paths.

> You are the Palimpsest nightly compile runner.
>
> **Step 1 — discover brain checkouts.**
>
> The cloud-container init cloned four repos somewhere on disk. Find them:
>
> ```bash
> for slug in palimpsest-personal palimpsest-work palimpsest-both palimpsest-work-shared; do
>   path=$(find / -maxdepth 6 -type d -name "$slug" 2>/dev/null | head -1)
>   echo "$slug=$path"
> done
> ```
>
> Export the four discovered paths as `PERSONAL`, `WORK`, `BOTH`, `SHARED`. If any are empty, abort and report which are missing — do not fabricate.
>
> **Step 2 — pull each brain.**
>
> For each of `$PERSONAL`, `$WORK`, `$BOTH`, `$SHARED`: `cd` into it and `git pull --rebase --autostash`.
>
> **Step 3 — synthesis + supervisor per brain.**
>
> For each brain in that order (personal, work, both):
>
> 1. `cd $<BRAIN>`
> 2. `python compile/main.py`
> 3. `python compile/supervise.py`
>
> Each script commits + pushes on its own. If either exits non-zero, capture the last 50 lines of its stderr, continue to the next brain — don't abort the whole run.
>
> **Step 4 — promote to shared.**
>
> ```bash
> cd $WORK
> PALIMPSEST_BOTH_BRAIN="$BOTH" \
>   PALIMPSEST_WORK_SHARED="$SHARED" \
>   python compile/promote.py
> ```
>
> **Step 5 — report.**
>
> Summarise per brain: rows compiled, articles created/updated/deleted, commit SHA pushed (or "no changes"). For any non-zero exit, include the stderr tail. One line per brain if everything succeeded.
>
> **Do not**: retry failed scripts, edit brain content directly, or touch `palimpsest/index.md` or `palimpsest/CHANGELOG.md` (the scripts regenerate those).

Keep the prompt in the console — tweaks shouldn't require a repo release + pin bump.

## The `claude/` branch-prefix guardrail

Routines defaults to only allowing pushes to branches prefixed `claude/` as a safety measure. Our compile scripts push directly to `main`. Options:

- **Disable the guardrail** for these specific repos in the routine settings. This is the intended path for our use case since the compile scripts are already the trusted code path — the guardrail is designed for cases where Claude is writing arbitrary code.
- **Reroute to `claude/` branches** by changing the push logic in `main.py`, `supervise.py`, and `promote.py` to push to a nightly branch and opening a PR. This reintroduces a human-gate, which is the opposite of the design.

Confirm the guardrail setting before declaring the routine deployed. Runs will appear to succeed but actually fail to push if the guardrail is still on.

## Scheduling

- **Frequency**: once daily. Cursor is idempotent, so extra runs are waste, not breakage.
- **Time**: 03:00 local is a decent default — picks up the previous day's logs after contributors are offline.
- **Drift**: skipped days (routine paused, outage) catch up on the next run from the cursor forward. No backfill logic needed.

## Cost and quota

- Per-run runtime: 2–10 minutes depending on how many new raw-log days there are.
- Token cost: dominated by Sonnet input tokens in synthesis. Rough estimate $0.10–$0.50 per nightly run.
- Runtime cost: Anthropic's hourly container fee (≈ $0.01 per run at current rates).
- Quota: counts against daily routine limit (Pro 5, Max 15, Team/Enterprise 25), separate from interactive-session quota.

## Concurrency across a team

Each contributor runs their own routine from their own plan. They all push to the same `palimpsest-work-shared`. Collisions are handled by `promote.py`'s `pull --rebase --autostash` + single-retry on non-fast-forward. Per-contributor brain repos can't collide because each lives under a distinct GitHub account.

No time staggering required. Spreading across a 2-hour window is harmless if you'd rather avoid a thundering herd on the shared repo.

## Failure handling

**VERIFY in the console**: what Routines sends on run failure (email, Slack, nothing), and how long execution logs are retained. As a fallback, the compile scripts' own push mechanism means "no new commits from the bot today" is a visible signal that a run failed silently.

## Why not just cron it locally?

Local cron works and avoids every routine-specific wrinkle above. Reasons to move to a routine:

- Contributor's laptop is off or asleep at 03:00 — local cron doesn't fire.
- Multi-device contributors don't want N redundant cron jobs racing each other.
- Shared-brain promotion wants a reliable cadence even when no one is logged in.

If none of those apply, a local Scheduled Task (Windows) or launchd/cron job (macOS/Linux) pointed at the same three scripts is strictly simpler.

## Changelog

- **2026-04-20**: First real routine run. Confirmed `claude`/`python`/`git` preinstalled, `gitleaks` missing. Confirmed two-phase init (cloud-container clone then setup script). Replaced speculative `/workspace/` mount paths with prompt-time discovery. Pinned gitleaks 8.30.1 in setup script.
