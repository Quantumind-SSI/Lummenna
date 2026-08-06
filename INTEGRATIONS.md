# Lumena Integration Guide

This document describes how to integrate **Lumena** (twin-force memory and context framework) into agentic coding platforms:

- [OpenCode](#opencode)

Each platform communicates with Lumena through one of three interfaces:

| Interface | Best For | Platforms |
|-----------|----------|-----------|
| **MCP Server** (stdio) | Native tool calling | OpenCode, Claude Desktop |
| **HTTP API** | Remote / cloud deployments | Custom agents, CI/CD |
| **Python SDK** (`ConversationMemory`) | In-process, LangChain agents | Custom Python agents |

---

## Prerequisites

```bash
cd lumena
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Initialize the palace
lumena init --device generic
```

The FastAPI server must be running for HTTP-based integrations:

```bash
lumena serve --host 0.0.0.0 --port 8848
```

Or use the built-in MCP stdio server (no HTTP required):

```bash
python -m lumena.integrations.mcp_server
```

---

## OpenCode

OpenCode supports [MCP servers](https://modelcontextprotocol.io) natively. The cleanest integration is via the `mcp:` block in `opencode.json`.

### Option A: MCP Stdio Server (Recommended)

Create `.opencode/opencode.json` in your project root:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "lumena-memory": {
      "type": "local",
      "command": ["python", "-m", "lumena.integrations.mcp_server"],
      "enabled": true,
      "env": {
        "LUMENA_DEVICE": "generic",
        "LUMENA_API_KEY": ""
      }
    }
  }
}
```

**What the agent can now do:**

- `lumena_search` — semantic + lexical hybrid search across the memory palace
- `lumena_store` — persist a code insight, decision, or snippet
- `lumena_assemble` — retrieve and assemble context in one call
- `lumena_turn` — log a full conversation turn with implicit feedback
- `lumena_feedback` — log explicit feedback on a retrieved memory
- `lumena_status` — show palace health (rooms, active chunks, TFC state)

### Option B: HTTP API via Remote MCP

If Lumena runs on a remote host or in Docker:

```json
{
  "mcp": {
    "lumena-remote": {
      "type": "remote",
      "url": "http://localhost:8848/mcp",
      "enabled": true,
      "headers": {
        "X-API-Key": "{env:LUMENA_API_KEY}"
      }
    }
  }
}
```

### Option C: OpenCode Skill for Lumena Workflows

Skills teach the agent *when* and *how* to use Lumena. Create:

```
.opencode/skills/lumena-memory/SKILL.md
```

```markdown
---
name: lumena-memory
description: >
  Use when the user asks to remember, recall, persist, or search across
  project decisions, code snippets, architecture notes, or conversation
  history stored in the Lumena memory palace. Trigger keywords:
  "remember", "recall", "what did we decide", "search my notes",
  "persist", "store this", "Lumena".
---

# Lumena Memory Skill

## When to use
- The user wants to save a decision, pattern, or insight for later retrieval.
- The user asks "what did we decide about X?" or "find my note on Y".
- You need to assemble relevant prior context before making a code change.

## Workflow
1. If the user asks to retrieve: call `lumena_search` or `lumena_assemble`.
2. If the user shares an insight to persist: call `lumena_store` with an appropriate `room` (e.g., `architecture`, `decisions`, `snippets`).
3. After a successful turn (user request + your response): call `lumena_turn` to store both messages and log implicit feedback.
4. If the user says a retrieved memory was wrong or unhelpful: call `lumena_feedback` with `was_useful: false`.

## Room naming conventions
| Room | Purpose |
|------|---------|
| `conversations` | Default chat history |
| `decisions` | ADRs, design decisions, trade-offs |
| `architecture` | Component diagrams, boundaries, interfaces |
| `snippets` | Reusable code patterns, one-liners |
| `bugs` | Root-cause analyses, incident post-mortems |
| `onboarding` | Palace Construction wizard output |

## Example
User: "Remember that we decided to use SQLite for the cache layer."
→ Call `lumena_store` with `room="decisions"`, `content="Use SQLite for cache layer because..."`
```

### Option D: Custom OpenCode Agent

For dedicated memory management, define a subagent:

```json
{
  "agent": {
    "lumena-librarian": {
      "description": "Specialist for storing, retrieving, and curating Lumena memories.",
      "mode": "subagent",
      "model": "anthropic/claude-sonnet-4-6",
      "prompt": "You are the Lumena Librarian. You have access to lumena_search, lumena_store, lumena_assemble, lumena_turn, and lumena_feedback. Your job is to help the user manage their long-term memory palace. Always use the correct room names."
    }
  }
}
```

---

## Platform Compatibility

Lumena's MCP server (stdio) and HTTP API are supported by any platform that speaks MCP or JSON/REST, including
OpenCode, Claude Desktop, and any custom agent using the Python SDK. The OpenCode section above provides
configuration examples; the same `python -m lumena.integrations.mcp_server` command works for any MCP-compatible
platform.

---

## Security & Privacy Notes

1. **Sovereign mode**: By default Lumena stores everything locally in SQLite (`~/.lumena/store/lumena.db`). No cloud leakage.
2. **API Key**: Set `LUMENA_API_KEY` and provide it via headers / env vars for multi-user environments.
3. **P2P sharing**: `lumena p2p share` is opt-in; memories do not leave the device unless explicitly shared.
4. **Forgetting**: Lumena supports compliance-driven forgetting (`lumena compliance audit`). Platforms should respect GDPR/CCPA deletion requests by calling the forgetting API.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Database not initialised` | Run `lumena init --device generic` |
| `embedder_fallback` warning | Download embedding model: `lumena init --download-model` |
| MCP server not found | Ensure `lumena` package is in the Python environment used by the platform |
| 413 Request too large | Increase `LUMENA_REQUEST_MAX_SIZE_BYTES` (default 1 MB) |
| CORS errors in browser | Set `LUMENA_ALLOWED_ORIGINS` to your domain |

---

## Next Steps

1. Start the Lumena server: `lumena serve`
2. Configure your platform's MCP / tool registry with the endpoints above.
3. Seed initial memories: `lumena illuminate` (interactive onboarding wizard).
4. Iterate on room taxonomy for your team.

For programmatic Python usage, see `lumena/integrations/langchain.py` and `lumena/conversation.py`.
