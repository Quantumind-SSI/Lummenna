# Lummenna Integration Guide

This document describes how to integrate **Lummenna** (twin-force memory and context framework) into agentic coding platforms:

- [OpenCode](#opencode)

Each platform communicates with Lummenna through one of three interfaces:

| Interface | Best For | Platforms |
|-----------|----------|-----------|
| **MCP Server** (stdio) | Native tool calling | OpenCode, Claude Desktop |
| **HTTP API** | Remote / cloud deployments | Custom agents, CI/CD |
| **Python SDK** (`ConversationMemory`) | In-process, LangChain agents | Custom Python agents |

---

## Prerequisites

```bash
cd lummenna
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Initialize the palace
lummenna init --device generic
```

The FastAPI server must be running for HTTP-based integrations:

```bash
lummenna serve --host 0.0.0.0 --port 8848
```

Or use the built-in MCP stdio server (no HTTP required):

```bash
lummenna mcp              # serve over stdio
lummenna mcp --check      # diagnose a deployment, then exit
```

`lummenna mcp` is equivalent to `python -m lummenna.integrations.mcp_server`;
use the module form when the console script is not on `PATH`. Both require the
`mcp` extra: `pip install "lummenna[mcp]"`.

---

## OpenCode

OpenCode supports [MCP servers](https://modelcontextprotocol.io) natively. The cleanest integration is via the `mcp:` block in `opencode.json`.

### Option A: MCP Stdio Server (Recommended)

Create `opencode.json` in your project root. It must be the repository root, not
`.opencode/`: that directory holds agents, commands, and skills, and OpenCode
does not read a config file from it.

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

A copy of this file lives at `integrations/opencode/opencode.json`.

Three details that decide whether the server appears at all:

- The key is `environment`, not `env`. `env` is not in OpenCode's
  `McpLocalConfig` schema, so those variables never reach the server.
- `lummenna` must be on the `PATH` of the process that launches OpenCode. If it
  is not (a virtualenv you do not activate first), use the absolute path,
  `["/path/to/.venv/bin/lummenna", "mcp"]`, or the module form,
  `["/path/to/.venv/bin/python", "-m", "lummenna.integrations.mcp_server"]`.
  Avoid bare `python`: most Linux distributions ship only `python3`.
- Set `timeout`. OpenCode's default is 5000 ms and a server that misses it is
  dropped silently: no error, it simply does not appear in the tool list. First
  launch with `LUMMENNA_AUTO_PROVISION_MODEL=1` downloads and exports the
  embedding model, which takes far longer than 5 seconds.

If the tools do not appear, ask the server itself rather than guessing:

```bash
lummenna mcp --check
```

It runs the same startup path the server uses and prints a table of what
passed, exiting non-zero on the first condition that would leave a client with
no tools. A mock embedder counts as a failure: the server would start and every
result would be noise.

`LUMMENNA_API_KEY` is deliberately absent: it authenticates the HTTP API
(`lummenna/api/server.py`) and has no effect on the stdio MCP server, which
inherits the trust boundary of the process that spawned it.

Restart OpenCode after editing the config; MCP servers are started once at boot.

**What the agent can now do (7 tools):**

- `lummenna_search` — semantic + lexical hybrid search across the memory palace
- `lummenna_store` — persist a code insight, decision, or snippet
- `lummenna_assemble` — retrieve and assemble context in one call
- `lummenna_turn` — log a full conversation turn with implicit feedback
- `lummenna_feedback` — log explicit feedback on a retrieved memory
- `lummenna_status` — show palace health (rooms, active chunks, TFC state)
- `lummenna_dashboard` — effectiveness dashboard (health, locally measured benchmarks, lifecycle state)

**And two read-only resources, for clients that consume JSON rather than call tools:**

- `dashboard://lummenna/effectiveness` — full metrics document (version, system,
  palace, tfc, evaluation, feedback, runtime, design_properties)
- `dashboard://lummenna/capabilities` — the design_properties, evaluation, and
  runtime sections only

### Option B: HTTP API (remote / Docker deployments)

The MCP server supports three transports: `stdio` (one process per client),
`streamable-http`, and `sse`. For several editor sessions against one palace,
serve a single shared process and point every client at its URL — each stdio
server loads its own embedder (~750 MB resident), so N sessions cost N times
that, while one HTTP server costs it once:

```bash
lummenna mcp --transport streamable-http --host 127.0.0.1 --port 8849
# MCP endpoint: http://127.0.0.1:8849/mcp
```

Network MCP transports are unauthenticated by design: bind them to loopback
or a trusted LAN interface only. For remote hosts outside a trusted network,
use the REST API directly instead — it is a different protocol with a
different surface, and it is the path that enforces the API key:

```bash
# On the remote host
lummenna serve --host 0.0.0.0 --port 8848

# From the client
curl -X POST http://REMOTE_HOST:8848/v1/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $LUMMENNA_API_KEY" \
  -d '{"query": "cache layer decision", "top_k": 5}'
```

Set `LUMMENNA_API_KEY` on the server before exposing it to any network (see `SECURITY.md`).

### Option C: OpenCode Skill for Lummenna Workflows

Skills teach the agent *when* and *how* to use Lummenna. Create:

```
.opencode/skills/lummenna/SKILL.md
```

```markdown
---
name: lummenna
description: >
  Use when the user asks to remember, recall, persist, or search across
  project decisions, code snippets, architecture notes, or conversation
  history stored in the Lummenna memory palace. Trigger keywords:
  "remember", "recall", "what did we decide", "search my notes",
  "persist", "store this", "Lummenna".
---

# Lummenna Memory Skill

## When to use
- The user wants to save a decision, pattern, or insight for later retrieval.
- The user asks "what did we decide about X?" or "find my note on Y".
- You need to assemble relevant prior context before making a code change.

## Workflow
1. If the user asks to retrieve: call `lummenna_search` or `lummenna_assemble`.
2. If the user shares an insight to persist: call `lummenna_store` with an appropriate `room` (e.g., `architecture`, `decisions`, `snippets`).
3. After a successful turn (user request + your response): call `lummenna_turn` to store both messages and log implicit feedback.
4. If the user says a retrieved memory was wrong or unhelpful: call `lummenna_feedback` with `was_useful: false`.

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
→ Call `lummenna_store` with `room="decisions"`, `content="Use SQLite for cache layer because..."`
```

### Option D: Custom OpenCode Agent

For dedicated memory management, define a subagent:

```json
{
  "agent": {
    "lummenna-librarian": {
      "description": "Specialist for storing, retrieving, and curating Lummenna memories.",
      "mode": "subagent",
      "model": "anthropic/claude-sonnet-4-6",
      "prompt": "You are the Lummenna Librarian. You have access to lummenna_search, lummenna_store, lummenna_assemble, lummenna_turn, and lummenna_feedback. Your job is to help the user manage their long-term memory palace. Always use the correct room names."
    }
  }
}
```

---

## Platform Compatibility

Lummenna's MCP server (stdio) and HTTP API are supported by any platform that speaks MCP or JSON/REST, including
OpenCode, Claude Desktop, and any custom agent using the Python SDK. The OpenCode section above provides
configuration examples; the same `lummenna mcp` command (or its module form,
`python -m lummenna.integrations.mcp_server`) works for any MCP-compatible platform.

---

## Security & Privacy Notes

1. **Sovereign mode**: By default Lummenna stores everything locally in SQLite (`~/.lummenna/store/lummenna.db`). No cloud leakage.
2. **API Key**: Set `LUMMENNA_API_KEY` and provide it via headers / env vars for multi-user environments.
3. **P2P sharing**: `lummenna p2p share` is opt-in; memories do not leave the device unless explicitly shared.
4. **Forgetting**: Lummenna supports compliance-driven deletion: `lummenna compliance forget <chunk_id> --reason "..."` permanently forgets a chunk (audit event, FTS removal, provenance purge, content redaction), and `lummenna compliance audit` shows the audit trail. Use these to honour GDPR/CCPA deletion requests.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Database not initialised` | Run `lummenna init --device generic` |
| `embedder_fallback` warning | Download embedding model: `lummenna init --download-model` |
| MCP server not found | Ensure `lummenna` package is in the Python environment used by the platform |
| 413 Request too large | Increase `LUMMENNA_REQUEST_MAX_SIZE_BYTES` (default 1 MB) |
| CORS errors in browser | Set `LUMMENNA_ALLOWED_ORIGINS` to your domain |

---

## Next Steps

1. Start the Lummenna server: `lummenna serve`
2. Configure your platform's MCP / tool registry with the endpoints above.
3. Seed initial memories: `lummenna illuminate` (interactive onboarding wizard).
4. Iterate on room taxonomy for your team.

For programmatic Python usage, see `lummenna/integrations/langchain.py` and `lummenna/conversation.py`.
