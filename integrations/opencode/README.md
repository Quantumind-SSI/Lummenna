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
      "command": ["lummenna", "mcp"],
      "enabled": true,
      "timeout": 60000,
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

- `lummenna` must be on the `PATH` of the process that launches OpenCode.
  OpenCode does not run the command through a login shell, so a virtualenv you
  activate in your terminal is not necessarily visible to it. When in doubt use
  the absolute path, `["/path/to/.venv/bin/lummenna", "mcp"]`, or the module
  form, `["/path/to/.venv/bin/python", "-m", "lummenna.integrations.mcp_server"]`.
  Avoid bare `python`: most Linux distributions ship only `python3`.
- Paths are relative to the OpenCode launch directory. Use absolute paths in a
  **global** config so Lummenna resolves the same store regardless of which
  project you launch from.
- `command` must be an array of strings, and the key is `environment` (not
  `env`) — this is OpenCode's schema, which differs from some other agents.
- Keep the explicit `timeout`. OpenCode's default is 5000 ms, and a server that
  exceeds it is dropped **silently**: there is no log line and no error, the
  tools simply never appear. A first launch with
  `LUMMENNA_AUTO_PROVISION_MODEL=1` downloads and exports the embedding model,
  which takes far longer than 5 seconds.

After editing config, **quit and restart OpenCode** — config is loaded once at
startup and is not hot-reloaded.

## Troubleshooting

If Lummenna's tools do not appear, do not guess at the config. Ask the server:

```bash
lummenna mcp --check
```

This runs the same startup path the server uses (dependency, store paths,
schema, embedder, tool/resource registry) and prints a table of results,
exiting non-zero on the first condition that would leave a client with no
tools. A mock embedder is reported as a failure: the server would start
normally and every semantic result would be noise.

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

Two read-only resources are published alongside the tools:
`dashboard://lummenna/effectiveness` (the full metrics document) and
`dashboard://lummenna/capabilities` (design properties, evaluation, runtime).

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
