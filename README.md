# Palimpsest

Persistent, scope-aware memory for Claude Code — conversation logs routed across multiple git-backed "brains", with write-time secrets redaction and a path toward an LLM-compiled curated knowledge base.

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

## Scope resolution

In order, first match wins:

1. **Title prefix** — `/rename [work] <title>`, `/rename [private] <title>`, `/rename [both] <title>`. Prefix is stripped from the displayed title.
2. **CWD rule** — substring match on the session's `cwd` (configured in `config.toml`).
3. **Fallback** — unset → `palimpsest-unclassified/`. The hook nudges you once per session to classify. When you do, any already-logged entries auto-migrate into the chosen brain.

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
