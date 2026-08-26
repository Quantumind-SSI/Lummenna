# OpenCode + Lummenna Integration

[OpenCode](https://opencode.ai) is an open-source terminal coding agent. It supports **MCP servers** natively, so it can call Lummenna's memory palace directly.

## Prerequisites

1. Install Lummenna with MCP support:

   ```bash
   pip install lummenna[mcp]
   ```

2. Provision the local embedding model **once** (the MCP server refuses to start
   without it, because serving mock embeddings would make semantic search
   meaningless):

   ```bash
   lummenna model download bge-small-en-v1.5
   ```

   Alternatively, set `LUMMENNA_AUTO_PROVISION_MODEL=1` (see below) and Lummenna
   will download and export the model automatically on first launch. The
   auto-provision path is a one-time network download; the model is cached
   locally and every subsequent launch is fully sovereign (offline).

## Configuration

Copy `opencode.json` into your project root (or merge the `mcp.lummenna` block
into your existing `opencode.json` / global `~/.config/opencode/opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "lummenna": {
      "type": "local",
      "command": ["python3", "-m", "lummenna.integrations.mcp_server"],
      "enabled": true,
      "environment": {
        "LUMMENNA_DEVICE": "generic",
        "LUMMENNA_STORE_PATH": "./.lummenna/store",
        "LUMMENNA_MODEL_PATH": "./.lummenna/models",
        "LUMMENNA_AUTO_PROVISION_MODEL": "1"
      }
    }
  }
}
```

Notes:

- Use `python3` explicitly. Many systems (including most Linux distros) do not
  ship a bare `python` alias, and OpenCode does not run the command through a
  login shell, so `python` will fail with "command not found".
- Paths are relative to the OpenCode launch directory. Use absolute paths in a
  **global** config so Lummenna resolves the same store regardless of which
  project you launch from.
- `command` must be an array of strings, and the key is `environment` (not
  `env`) — this is OpenCode's schema, which differs from some other agents.

After editing config, **quit and restart OpenCode** — config is loaded once at
startup and is not hot-reloaded.

## Usage

Once configured, OpenCode can call Lummenna tools automatically:

```
Search my memory palace for decisions about the database layer.
Remember that we decided to use PostgreSQL for the main store.
Assemble relevant context for implementing user authentication.
```

OpenCode will invoke:

- `lummenna_search` for recall queries
- `lummenna_store` for persistence requests
- `lummenna_assemble` for context-rich tasks
- `lummenna_turn` after completing conversation turns
- `lummenna_status` / `lummenna_dashboard` for health and effectiveness

## Persistent memory instructions

To encourage OpenCode to persist its reasoning and decisions to Lummenna, add to
your project `AGENTS.md`:

```markdown
## Memory Management
- Use lummenna_store to persist important decisions, patterns, and architectural notes.
- Use lummenna_search to recall prior decisions before making changes.
- After completing a task, call lummenna_turn to log the conversation.
```

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `LUMMENNA_DEVICE` | `generic` | Device profile (tunes budgets/scheduler). |
| `LUMMENNA_STORE_PATH` | `./.lummenna/store` | SQLite palace location. |
| `LUMMENNA_MODEL_PATH` | `./.lummenna/models` | ONNX embedding model location. |
| `LUMMENNA_AUTO_PROVISION_MODEL` | unset | If `1`/`true`, auto-download+export the embedding model on first launch if missing. One-time network access; sovereign thereafter. |
