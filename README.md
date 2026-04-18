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

## Scope resolution

In order, first match wins:

1. **Title prefix** — `/rename [work] <title>`, `/rename [private] <title>`, `/rename [both] <title>`, or `/rename [nolog] <title>`. Prefix is stripped from the displayed title.
2. **CWD rule** — substring match on the session's `cwd` (configured in `config.toml`).
3. **Fallback** — unset → `palimpsest-unclassified/`. The hook nudges you once per session to classify. When you do, any already-logged entries auto-migrate into the chosen brain.

### Opt-out — `[nolog]`

Use `/rename [nolog] <title>` on any session you want excluded from logging entirely (sensitive, personal, legal, etc.). The hook will:

- Write nothing new for that session.
- **Purge** any prior entries for that session from every brain and the staging area — applying `[nolog]` mid-session retroactively erases what was captured earlier.
- Suppress the classification nudge.

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
