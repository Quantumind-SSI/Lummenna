# OpenAI Codex CLI + Lumena Integration

[OpenAI Codex CLI](https://github.com/openai/codex) is an open-source coding agent from OpenAI. It supports **MCP servers** natively.

## Configuration

Add to your `~/.codex/config.json` (or project-local `.codex/config.json`):

```json
{
  "mcp_servers": {
    "lumena": {
      "command": "python",
      "args": ["-m", "lumena.integrations.mcp_server"],
      "env": {
        "LUMENA_DEVICE": "generic",
        "LUMENA_STORE_PATH": "${HOME}/.lumena/store",
        "LUMENA_MODEL_PATH": "${HOME}/.lumena/models"
      }
    }
  }
}
```

Or use the CLI to add it:

```bash
codex mcp add lumena \
  --command "python -m lumena.integrations.mcp_server" \
  --env LUMENA_DEVICE=generic
```

## Usage

Once configured, Codex can call Lumena tools automatically:

```bash
codex "Search my memory palace for decisions about the database layer"
codex "Remember that we decided to use PostgreSQL for the main store"
codex "Assemble relevant context for implementing user authentication"
```

Codex will invoke:
- `lumena_search` for recall queries
- `lumena_store` for persistence requests
- `lumena_assemble` for context-rich tasks
- `lumena_turn` after completing conversation turns

## Full Agent Mode

In **agent mode**, Codex will automatically persist its reasoning and decisions to Lumena:

```bash
codex --mode agent "Refactor the API to use dependency injection"
```

Make sure your `~/.codex/instructions.md` includes:

```markdown
## Memory Management
- Use lumena_store to persist important decisions, patterns, and architectural notes.
- Use lumena_search to recall prior decisions before making changes.
- After completing a task, call lumena_turn to log the conversation.
```
