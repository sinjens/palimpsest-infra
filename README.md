# Palimpsest

Persistent, scope-aware memory for Claude Code — conversation logs routed across multiple git-backed "brains", with write-time secrets redaction and a path toward an LLM-compiled curated knowledge base.

## Why "palimpsest"?

A **palimpsest** (Greek *palimpsēstos*, "scraped clean again") is a parchment or manuscript that has been written on, scraped off, and written on again — often with traces of the earlier text still visible beneath the new writing. In medieval scriptoria, where vellum was expensive, scribes routinely erased old texts to reuse the surface; modern archaeologists and philologists have since recovered the ghosted layers of lost works that would otherwise have vanished.

The name fits this project because the curated memory layer is meant to *evolve*. Articles get rewritten as knowledge refreshes, TTLs expire, and new context replaces outdated claims — but the underlying strata are never destroyed. Git history preserves every prior version; raw conversation logs stay immutable beneath; the live surface always reflects the current best understanding. The palimpsest is what grows on top.

## What this is

Every Claude Code session on your machine gets:

1. **Logged** to date-sharded `.md` + `.jsonl` files (human-readable audit view + full-fidelity transcript).
2. **Routed** by scope (private / work / both) into separate git repos, so each "brain" has its own privacy boundary and its own retention / sharing policy.
3. **Redacted** at write time — Google / Anthropic / OpenAI keys, GitHub PATs, JWTs, AWS keys, Slack tokens, Azure connection strings, and private keys are scrubbed before hitting disk.

A compile loop (not yet shipped) will later distill raw logs into a curated **palimpsest** — evolving markdown articles with TTL hygiene, per-scope strategies, and a promotion gate for cross-brain sharing.

## Architecture

```
session ──▶ UserPromptSubmit / Stop hook ──▶ scope resolver ──▶ routed to:
                                                                 ├── palimpsest-personal      (scope=private)
                                                                 ├── palimpsest-work          (scope=work)
                                                                 ├── palimpsest-both          (scope=both)
                                                                 └── palimpsest-unclassified  (scope=unset, staging)
```

Each brain is its own git repo. You clone only the brains you need on a given device; each brain's content is never duplicated across brains.

## Auto-sync across devices

With `auto_sync = true` (the default), the hook:

- **Pulls each configured brain once per session** on the first `UserPromptSubmit`, using `git pull --rebase --autostash` with a 5-second timeout.
- **Commits + async-pushes** to the target brain on every `Stop`. Commit is synchronous (so we know something was staged); push is a detached subprocess so Claude never waits for network.

Network failures never block the hook — they land in `~/.claude/palimpsest/errors.log`. Set `auto_sync = false` in `config.toml` to disable (useful when offline a lot, or when the machine is shared).

## What gets logged — and secrets hygiene

Two surfaces land on disk per turn (per brain):

- **`.md`** — a condensed, human-readable narrative: your prompts, Claude's text responses, and ExitPlanMode plans. No tool content.
- **`.jsonl`** — the richer transcript, governed by the `log_tool_calls` config setting.

### `log_tool_calls` modes

| Mode | What's in the `.jsonl` | When to pick it |
|---|---|---|
| `"none"` *(default)* | User prompts, Claude text blocks, ExitPlanMode plans. Tool calls and outputs are stripped entirely. | Almost always. The default. |
| `"minimal"` | Same as `"none"`, plus each tool call's **name + correlation ID** (inputs/outputs are replaced with `[STRIPPED]`). | You want the compile loop to reason about *which* tools were used, without capturing *what* they read or returned. |
| `"full"` | Everything verbatim — raw tool inputs, raw tool outputs, full file content pulled by `Read`, full stdout/stderr from `Bash`, everything. Write-time regex redaction still runs. | **Rarely. Read the warning below first.** |

#### Why `"full"` is risky

Most secrets that leak into AI-agent workflows leak via *tool output*. A `cat .env`, a `Read` on a config file, a database query dump, a `curl` with headers — any of these paste the raw content into the tool_result stream. In `"full"` mode, that content lands in your brain's `.jsonl` and gets git-pushed on the next Stop.

Regex redaction catches common, well-known formats (Google / Anthropic / OpenAI keys, GitHub PATs, AWS access keys, JWTs, Azure connection strings, Stripe / Twilio / DigitalOcean / SendGrid / Slack webhooks, DB URLs with embedded credentials, private-key blocks). It will **not** catch hand-rolled tokens, environment-specific API keys named nothing like a known pattern, random binary blobs pasted as base64, or anything matching a format that hasn't been added yet.

The audit radius of a leaked token in a `"full"`-mode brain:

- Every device you clone that brain to.
- Every teammate with read access.
- GitHub's / your git host's backups (private-repo content is not training fodder on major hosts, but a compromise of the host or your account is a compromise).
- Any future machine you haven't imagined yet.

**For a solo setup on a private repo**, the risk is bounded but real — one leaked backup copies every shell output you've ever run. **For a shared brain (e.g. a team's work brain)**, every other contributor sees your tool output verbatim every day. There's no "this tool output is just for me" mode in `"full"`.

Only pick `"full"` if:

- You have a specific need (e.g. a compile loop that requires raw tool I/O to reason well).
- The brain is private and you trust everyone with read access.
- You've accepted that gitleaks is your only backstop against novel secret formats — and you've actually installed the pre-commit hook (see INSTALL Step 7).
- You've extended `_REDACTION_PATTERNS` in your fork with secret formats specific to your stack.

If you're not sure, leave it on `"none"`.

### Secret redaction backstops

Two layers:

1. **Write-time regex** in the hook — best effort, catches well-known formats, runs before the file hits disk.
2. **Pre-commit gitleaks** on each brain repo — maintained ruleset with ~150 secret patterns + entropy heuristics, blocks commits containing any hit. Strongly recommended; see INSTALL Step 7.

## Running on private repos, or a self-hosted git server

Palimpsest is agnostic to the remote — the hooks call `git` directly, so whatever URL a brain's `origin` points at is what gets pulled from and pushed to.

**Private GitHub repos.** The default. Any combination of `palimpsest-infra` / personal / work / both can be private on `github.com` with no script changes. Auth via `gh auth login` or a classic PAT with `repo` scope. [GitHub's policy](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement) is that private-repo content isn't used for model training or made available to other users.

**Self-hosted git server.** If you don't want anything on `github.com` at all — e.g. a company running [Gitea](https://about.gitea.com/), [Forgejo](https://forgejo.org/), GitLab CE, or Bitbucket Server — just point each brain's `origin` at the internal URL:

```bash
cd <brain-path>
git remote set-url origin https://git.your-company.com/you/palimpsest-personal.git
```

Pulls and pushes work transparently. The hooks don't know or care which server they're talking to.

**GitHub Enterprise Server (GHES).** Same pattern as self-hosted — set remotes to `https://github.your-company.com/...` and authenticate per your org's SSO/SAML-backed PAT.

**Caveat for the future compile loop.** The nightly compile routine (see roadmap) runs inside Anthropic's infrastructure, so it needs network reach to whichever git server holds the brain repos. If that's an internal-only GHES or on-prem Gitea not exposed to the public internet, the managed agent can't clone. Two workarounds:

1. **Run the compile script on your own always-on machine** (Unraid, NAS, office server, CI runner) instead of as a managed Anthropic routine. The code is identical; managed agents are just one deployment target.
2. **Mirror to a reachable private GitHub repo** that the routine can clone from, and sync from your internal server on a schedule.

**Picking an option.**

- Solo developer, nothing legally sensitive beyond personal notes → private `github.com`. Easiest, zero infrastructure.
- Small team, strong company-confidentiality policies → private `github.com` or GHES; managed routines still work directly.
- Regulated industries (finance / defense / healthcare where even the *existence* of the repo is sensitive) → self-host, and run the compile loop on your own infrastructure, not Anthropic's.

### ⚠️ "Private" is relative

A private GitHub repo means no public read access and — per GitHub's current policy — no training on its contents at rest. But using GitHub still means your data lives on Microsoft-owned Azure infrastructure, is accessible to GitHub employees under policy-bounded circumstances (abuse investigation, legal response, support), subject to US legal processes, and present in GitHub's backup systems for some period after you delete.

Starting **April 24, 2026**, GitHub also trains on **Copilot interaction data** (prompts, suggestions, code snippets, surrounding context) from Copilot **Free / Pro / Pro+** users *by default*. Opt-out is in Settings → Privacy. Copilot **Business** and **Enterprise** are not affected. This policy covers interaction data generated while you work — including inside private repos — but **not** the repo contents at rest. If you run Copilot while editing files in a palimpsest brain repo and haven't opted out, the autocompletions on those files are collected.

For most personal use on a private GitHub repo, this is a manageable risk. But if the data in your brains is ever legally, contractually, or ethically sensitive enough that a Microsoft-managed third party seeing it would be a problem, consider:

- A **self-hosted git server** — Gitea, Forgejo, GitLab CE, Bitbucket Server. See the section above.
- **Fully local with no remote** — `git init` and never add an origin. You lose cross-device sync but nothing leaves your machine. Palimpsest's auto-sync silently no-ops without a remote, so everything else still works.

Sources:
- [Updates to GitHub's Privacy Statement and Terms of Service (2026-03-25)](https://github.blog/changelog/2026-03-25-updates-to-our-privacy-statement-and-terms-of-service-how-we-use-your-data/)
- [GitHub Privacy Statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement)

## Scope resolution

In order, first match wins:

1. **Title prefix** — `/rename [work] <title>`, `/rename [private] <title>`, `/rename [both] <title>`, or `/rename [nolog] <title>`. Prefix is stripped from the displayed title.
2. **CWD rule** — substring match on the session's `cwd` (configured in `config.toml`).
3. **Fallback** — unset → `palimpsest-unclassified/`. The hook nudges you once per session to classify. When you do, any already-logged entries auto-migrate into the chosen brain.

### Opt-out — `[nolog]`

Use `/rename [nolog] <title>` on any session you want excluded from logging (sensitive, personal, legal, etc.). The hook will:

- Write nothing new for that session.
- Delete any files for that session in every brain's working tree and in the unclassified staging area.
- Suppress the classification nudge.

#### ⚠️ `[nolog]` is not time-travel

**What `[nolog]` does NOT do:**

- It does **not** rewrite git history. If a previous Stop in this session already commit+pushed, the raw turns are in `origin/main` and on every teammate who has pulled. `[nolog]` can't recall that.
- It does **not** touch Claude Code's own transcripts at `~/.claude/projects/<slug>/<session_id>.jsonl`. That's Claude Code's native state — Palimpsest routes a *copy* of each turn, it doesn't own Claude Code's storage. If that also needs wiping, delete it manually.

**When `[nolog]` is actually enough:**

- Early in a session, before the first Stop fires. Since `[nolog]` runs synchronously on the next hook and purges working-tree files before any new push, nothing leaks if you tag it fast.
- On a local-only (no-remote) brain. No auto-push = nothing to recall.

### Wiping already-committed content from a private brain

If you realize too late that something sensitive was logged, you need real history rewriting — not `[nolog]`. Two paths:

**(a) Ask Claude Code to do it.** Open a session in the brain repo and say, for example:

> *"Wipe session `ed0acee3-…` from this brain. Look in `raw/logs/` for any file matching that session id, remove it, rewrite history with `git filter-repo`, force-push, and warn me to tell any collaborators to re-clone."*

Claude picks the commands, confirms before force-push, and handles the cleanup.

**(b) Do it by hand.**

```bash
cd <brain-path>
git filter-repo --invert-paths --path-glob 'raw/logs/**/*<session_id>*'
git push --force-with-lease origin main
# Anyone else who cloned this brain: tell them to re-clone. Their old
# local copy still has the deleted content.
```

Force-pushing a private repo is manageable; doing it on a shared brain requires coordinating with collaborators. Plan accordingly.

## File layout

Inside each brain:

```
<brain>/
└── raw/
    └── logs/
        └── YYYY-MM-DD/
            ├── HHMMSS_<title>_<session_id>.md      ← human audit
            └── HHMMSS_<title>_<session_id>.jsonl   ← full transcript
```

`HHMMSS` is the session's first-entry time for that date folder. `session_id` is the full UUID — stable join key across date folders if a session spans midnight.

Eventually a `palimpsest/` subtree at the brain root will hold the LLM-compiled curated articles.

## Requirements

- **Python 3.11+** (uses `tomllib` from stdlib, no external dependencies)
- **git**
- **Claude Code**
- **gitleaks** (optional but strongly recommended — pre-commit secret scanning)

## Install

See [INSTALL.md](INSTALL.md). Written to be read and executed by Claude Code itself on a new device:

> In a Claude Code session, tell Claude: "Read `INSTALL.md` in this repo and set up Palimpsest on this machine."

## License

MIT.
