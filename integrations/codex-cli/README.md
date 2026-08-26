# OpenAI Codex CLI + Lummenna Integration

[OpenAI Codex CLI](https://github.com/openai/codex) is an open-source coding agent from OpenAI. It supports **MCP servers** natively.

> **Honesty note:** Codex CLI's configuration surface changes between releases; these snippets were written against the TOML-based config (`~/.codex/config.toml`) current at the time of writing and are not continuously re-verified. If a snippet fails, consult `codex --help` and the upstream docs — the Lummenna side (the stdio MCP server) is stable.

## Configuration

Add to your `~/.codex/config.toml`:

```toml
[mcp_servers.lummenna]
command = "python"
args = ["-m", "lummenna.integrations.mcp_server"]

[mcp_servers.lummenna.env]
LUMMENNA_DEVICE = "generic"
LUMMENNA_STORE_PATH = "/home/YOU/.lummenna/store"
LUMMENNA_MODEL_PATH = "/home/YOU/.lummenna/models"
```

Or use the CLI to add it (if your Codex version provides `codex mcp`):

```bash
codex mcp add lummenna -- python -m lummenna.integrations.mcp_server
```

## Usage

Once configured, Codex can call Lummenna tools automatically:

```bash
codex "Search my memory palace for decisions about the database layer"
codex "Remember that we decided to use PostgreSQL for the main store"
codex "Assemble relevant context for implementing user authentication"
```

Codex will invoke:
- `lummenna_search` for recall queries
- `lummenna_store` for persistence requests
- `lummenna_assemble` for context-rich tasks
- `lummenna_turn` after completing conversation turns

## Persistent memory instructions

To encourage Codex to persist its reasoning and decisions to Lummenna, add to your `~/.codex/AGENTS.md` (or project `AGENTS.md`):

```markdown
## Memory Management
- Use lummenna_store to persist important decisions, patterns, and architectural notes.
- Use lummenna_search to recall prior decisions before making changes.
- After completing a task, call lummenna_turn to log the conversation.
```
