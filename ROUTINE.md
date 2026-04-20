# Palimpsest — Running the compile loop as a Claude Code Routine

This document is for **operators** deploying the nightly compile loop to [Claude Code Routines](https://platform.claude.com) so it runs on Anthropic's infrastructure instead of a contributor's laptop. If you just want to use Palimpsest on your own machine, you don't need any of this — see `INSTALL.md`.

## Status: research preview, expect gaps

Routines shipped as a research preview in April 2026. Some of what you need is **not yet publicly documented** and has to be confirmed in the Claude Code web console when you set it up. This document is organised around "here is what the routine must do" + "here is what you need to verify in the console because it isn't documented."

If you find the public schema / docs for any of the `VERIFY` items below, please PR back.

## What this routine does

For each brain repo the contributor owns, the routine runs three things:

1. `python compile/main.py` — Sonnet synthesis pass: reads the day's raw logs, updates curated articles.
2. `python compile/supervise.py` — Opus review pass: consolidates, reconciles contradictions, flags `share: true`.
3. `python compile/promote.py` (from `palimpsest-work` only, with `PALIMPSEST_BOTH_BRAIN` set) — copies `share: true` articles from `palimpsest-work` and `palimpsest-both` into the shared company brain.

Each script commits + pushes on its own. The cursor file (`compile/cursor.txt`) makes runs idempotent — re-running on the same day is a no-op.

## The non-obvious dependency: these scripts shell out to `claude -p`

`main.py` and `supervise.py` don't use the Anthropic API directly. They run `claude -p --model <sonnet|opus> --name "[nolog] ..."` as a subprocess and parse delimited blocks from stdout. This means the routine environment **must have the Claude Code CLI installed and authenticated**.

This is the single most fragile part of running the loop as a routine. **VERIFY** in the console:

- Is `claude` CLI preinstalled in the routine's runtime image? If not, can the prompt install it (`npm i -g @anthropic-ai/claude-code` or the platform installer) as a first step?
- Does the subprocess `claude -p` inherit the routine's own Claude credential, or does it need a separate auth step (e.g., `ANTHROPIC_API_KEY` env var, or a mounted `~/.claude/` directory)?
- Does it bill against the same Max plan quota the routine itself runs under, or does it hit API billing?

If the CLI path doesn't work in a routine, the fallback is to rewrite `invoke_claude()` to call the Anthropic Messages API directly. That's a ~20-line change but loses the subscription-bill path.

## Prerequisites (at infrastructure level)

- All brain repos exist on GitHub and the contributor has push rights.
- The shared repo `palimpsest-work-shared` exists and the contributor has push rights.
- Brain repos have their pre-commit gitleaks hook installed and pass locally — routines that can't commit stall silently.
- `compile/cursor.txt` exists in each brain (run the compile once locally first; creates it).

## Repo access

Two options for giving the routine push access to private brain repos:

1. **GitHub App** (preferred if supported) — install the Claude Code Routines GitHub App on the contributor's account, grant it access only to the specific brain repos. No token rotation, per-repo scope.
2. **Fine-grained PAT** (fallback) — create a PAT with `Contents: Read and write` scoped to just the required repos. Store as a routine secret (e.g., `GITHUB_TOKEN`). Have the routine prompt configure `git` to use it via `GIT_ASKPASS` or `.netrc`.

**VERIFY**: the current Routines product surface for GitHub access is not yet fully documented. Known fact: routines can only push to branches prefixed `claude/` by default as a safety guardrail — **this is incompatible with our scripts**, which push directly to `main`. Options:

- Disable or relax the `claude/` prefix guardrail for these repos (check console settings).
- Change the scripts to push to `claude/nightly-YYYY-MM-DD` and have a second step (or the contributor) fast-forward `main`. This re-introduces a human-gate and is the opposite of what we want.
- Use a PAT with direct push to `main` and skip the Routines-native GitHub integration entirely. Security tradeoff: the PAT has to be stored as a routine secret.

Confirm which of these applies before declaring the routine "deployed".

## Git identity and signing

The routine makes real commits to real branches, so `user.name` and `user.email` must be set in the routine's git config. Options:

- **No signing**: set `user.name` and `user.email` via `git config --global` in the prompt body's prelude. Commits will be unsigned. The shared brain's branch protection must NOT require signed commits.
- **SSH signing**: mount an Ed25519 signing key as a routine secret, write it to `~/.ssh/`, `git config gpg.format ssh`, `git config user.signingkey <path>`, `git config commit.gpgsign true`, and register the corresponding **public** key as a signing key on the GitHub account. Then pushes match the contributor's signed-commit policy.

Routine commits will appear under whatever identity you configure — typically a dedicated `palimpsest-bot@<yourdomain>` mailbox, or the contributor's own email if you want the commits to attribute to them.

## The routine prompt

Rough shape. Adapt paths to whatever mount points the console gives you.

> You are the Palimpsest nightly compile runner. Your job is to invoke three Python scripts across the contributor's brain repos and surface any failures in your completion output.
>
> **Prelude — run once:**
>
> 1. Verify `claude`, `python`, `git`, `gitleaks` are all on PATH. If `claude` is missing, install with `npm i -g @anthropic-ai/claude-code` (or whatever the routine-env install path is).
> 2. Configure git: `git config --global user.name "Palimpsest Bot"` and `git config --global user.email "<configured-email>"`. If an SSH signing key is mounted, configure signing too.
> 3. `git pull --rebase --autostash` each mounted brain repo in turn.
>
> **Main loop — for each brain in `[personal, work, both]`:**
>
> 1. `cd /workspace/<brain>`
> 2. `python compile/main.py` — synthesis pass. If exit ≠ 0, capture the last 50 lines of stderr, continue to next brain.
> 3. `python compile/supervise.py` — supervisor pass. Same failure handling.
>
> **Promotion — once at the end:**
>
> ```
> cd /workspace/work
> PALIMPSEST_BOTH_BRAIN=/workspace/both \
>   PALIMPSEST_WORK_SHARED=/workspace/shared \
>   python compile/promote.py
> ```
>
> **Completion output:**
>
> Report, per brain: rows compiled, articles created/updated/deleted, commit SHAs pushed. For any non-zero exit, include the captured stderr tail. If everything succeeded, one-line summary per brain is enough — do not dump full script output.
>
> **Do not**: retry failed scripts, edit brain content directly, or touch `palimpsest/index.md` / `palimpsest/CHANGELOG.md` (the scripts regenerate these).

Keep this in the console, not in the repo, so tweaks don't require a repo release + pin bump.

## Scheduling

- **Frequency**: once daily. The cursor is idempotent, so running more often than necessary is waste, not breakage.
- **Time**: 03:00 in the contributor's timezone is a decent default — picks up the previous day's logs after everyone's gone home.
- **Drift**: if you skip days (routine paused, machine offline), the next run catches up from the cursor forward. No backfill logic needed.

## Cost and quota

- Per-run runtime: typically 2–10 minutes depending on how many new raw-log days there are to process.
- Token cost: dominated by Sonnet input tokens on the synthesis pass. Rough estimate $0.10–$0.50 per nightly run.
- Runtime cost: Anthropic's routine runtime fee (as of writing, $0.08/hr, so ≈ $0.01 per run).
- Quota: routines count against your plan's daily routine limit (Pro: 5, Max: 15, Team/Enterprise: 25), separate from your interactive-session quota.

## Concurrency across a team

Each contributor runs their own routine from their own plan. They all push to the same `palimpsest-work-shared`. Collisions are handled by `promote.py`'s `pull --rebase --autostash` + single-retry on non-fast-forward rejection. Per-contributor brain repos (`-personal` / `-work` / `-both`) can't collide because each lives under a distinct GitHub account.

No time staggering is needed, but spreading across a 2-hour window is harmless if you'd rather avoid a thundering herd on the shared repo.

## Failure handling and observability

**VERIFY** in the console:

- What does the routine send on failure — email, Slack, nothing?
- How long are execution logs retained?
- Is there an API to query recent runs, or UI only?

If failure notification is too coarse, the fallback is to have the routine's completion message include a summary and rely on the daily cadence being noticed when it stops arriving. The compile scripts' own push mechanism means you can also see "no commits from bot today" as a signal.

## Why not just cron it locally?

Local cron works and avoids every unknown above. The reason to move to a routine is:

- Contributor's laptop is off or asleep at 03:00 — local cron doesn't fire.
- Multi-device contributors don't want N redundant cron jobs racing each other.
- Shared-brain promotion wants a reliable cadence even when no one is logged in.

If those don't apply, a local Scheduled Task (Windows) or launchd/cron job (macOS/Linux) pointed at the same three scripts is strictly simpler.
