# Palimpsest — Running the compile loop as a Claude Code Routine

This document is for **operators** deploying the nightly compile loop to [Claude Code Routines](https://platform.claude.com) so it runs on Anthropic's infrastructure instead of a contributor's computer. If you just want to use Palimpsest on your own machine, you don't need any of this — see `INSTALL.md`.

## Status: research preview

Routines shipped as a research preview in April 2026. The authoring surface is UI-only and the runtime environment has a couple of non-obvious rough edges documented below.

## What the routine does

The routine runs exactly ONE command: `python compile/nightly.py` (from any brain checkout — the runner discovers its siblings). The runner deterministically executes, per brain in fixed order (personal, work, both):

1. `git pull --rebase --autostash` — a brain that can't sync is skipped, never compiled stale.
2. `python compile/main.py` — Sonnet synthesis: reads the day's raw logs, updates curated articles.
3. `python compile/supervise.py` — Opus review: consolidates, reconciles contradictions, flags `share: true`.
4. Writes + commits a run receipt (`compile/last-run.json`: per-step exit codes and durations), then pushes.

Then once: `python compile/promote.py` (from `palimpsest-work`, with `PALIMPSEST_BOTH_BRAIN`/`PALIMPSEST_WORK_SHARED` set by the runner) — copies `share: true` articles into the shared company brain and pushes it.

The cursor file (`compile/cursor.txt`) makes runs idempotent — re-running on the same day is a no-op. The receipt makes completion auditable from any checkout: if `compile/last-run.json` on origin doesn't carry last night's date with all-zero exit codes, the run did not fully happen.

**Why a runner script instead of prompt steps:** on 2026-06-06 the executing agent parallelized the per-brain steps as background tasks (despite explicit ordering in the prompt); one task never completed and a brain was silently skipped. Orchestration is now code; the agent only launches it and relays the report.

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

Paste this into the Routine's "setup script" field. It installs gitleaks pinned to a known version, verifies, and makes it available on PATH for all subsequent steps including the Claude Code session.  Setup script can be accessed in the environment setting (bottom right in the Routine dialog, create a new env or edit the default)

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

If you want signed commits, also mount an SSH signing key as a routine secret (Add SSH_SIGNING_KEY to the Env vars) and extend the setup script:

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

Copy everything between the `*** PROMPT STARTS HERE ***` and `*** PROMPT ENDS HERE ***` markers into the routine's prompt body (exclusive of the markers themselves). Discovery is baked in — don't hardcode mount paths.

*** PROMPT STARTS HERE ***

You are the Palimpsest nightly compile runner. Your entire job is to launch ONE script in the foreground and relay its report. All orchestration lives in the script.

**Step 1 — locate the work-brain checkout.**

```bash
WORK=$(find / -maxdepth 6 -type d -name "palimpsest-work" 2>/dev/null | head -1)
echo "WORK=$WORK"
```

If empty, abort and report that the checkout is missing — do not fabricate paths.

**Step 2 — run the pipeline, foreground, single command.**

```bash
cd "$WORK" && git checkout main && python compile/nightly.py
```

Hard rules:

- Run it as one FOREGROUND command and wait for it to finish. NEVER move it to a background task, never parallelize anything, and never end your turn while it is still running. It can take up to ~60 minutes on a backfill night — that is normal; wait.
- Do NOT run `compile/main.py`, `compile/supervise.py`, or `compile/promote.py` yourself — the runner sequences them.
- Do NOT commit, push, retry failed steps, or edit brain content yourself. The runner commits receipts and pushes; commits must land on `main` (no `claude/*` branches — if you see one being created, stop and report it).

**Step 3 — report.**

Relay the runner's final `NIGHTLY REPORT` block verbatim. If its exit code was non-zero, also include the last 50 lines of output preceding the report.

*** PROMPT ENDS HERE ***

Keep the prompt in the console — tweaks shouldn't require a repo release + pin bump.

## The `claude/` branch-prefix guardrail — disable it

Routines defaults to rewriting pushes from `main` onto a per-run `claude/<adjective>-<scientist>-<suffix>` branch as a safety measure. That's good hygiene for cases where Claude is writing arbitrary code, but it's wrong for our use case: the compile scripts are already the trusted code path, and we want their pushes to land on `main` so downstream consumers (the contributor's laptop, other routines, collaborators' checkouts) see curated content without a manual merge step.

**Before deploying the routine, disable the guardrail** for the four brain repos in the Routine settings. Also set the routine's default target branch to `main`. If the guardrail is left on, runs will appear to succeed — the scripts' `git push` exits 0 — but the commits silently land on `claude/*` branches instead of `main` and pile up as unmerged noise.

If you run a routine before disabling and end up with commits on `claude/*` branches, you have three options per branch:

- **Fast-forward merge into main** if main hasn't moved: `git checkout main && git merge --ff-only origin/claude/<branch> && git push && git push origin --delete claude/<branch>`.
- **Cherry-pick the commits** onto current main if main has diverged. Preserves routine-bot authorship.
- **Delete the branch** if the commits were no-ops (supervisor: library coherent): `git push origin --delete claude/<branch>`.

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
- **2026-04-20**: Second real routine run. All four pushes landed on `claude/*` branches, not `main`, because the default-on branch-prefix guardrail rewrote them. Supervisor edits on personal + both were genuinely useful and merged back manually. Added explicit main-only instruction to prompt and reframed the guardrail section as "disable before first run, with recovery recipe if you forgot".
- **2026-06-06**: The 01:05 run parallelized the per-brain steps as background tasks despite the prompt's explicit ordering; the both-brain task never completed and the brain was silently skipped (caught next morning, recovered by a local run). Orchestration moved from prompt prose into `compile/nightly.py` — deterministic sequence, per-step timeouts, explicit pushes, committed `compile/last-run.json` receipts per brain. Prompt reduced to: locate checkout, run the script in the foreground, relay its report.
- **2026-06-06** (v0.7.0): Supervise went incremental. The full-library nightly read (~280k tokens at 361 articles, 70% no-op verdicts historically) is replaced by: dirty set since the last reviewed SHA (`compile/supervise-state.txt`, committed) + `related:` neighbours + python pre-checks (TTL expiry, GDPR email screen on dirty) + a rotating 1/`PALIMPSEST_AUDIT_SHARDS` audit shard (default 7 — full coverage weekly). Quiet nights skip the model call entirely. The supervisor may only edit articles shown in full (blind-edit guard). Synthesis gained a trivial-session skip-gate (`PALIMPSEST_MIN_SESSION_BYTES`, default 1000). Measured: bootstrap night 59/361 articles ≈ 61k tokens.
