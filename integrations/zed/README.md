# Zed + Lummenna Integration

[Zed](https://zed.dev) is a high-performance AI-native code editor. Zed supports **context servers** (their term for MCP-like integrations) and **slash commands**.

## Option 1: Zed Context Server (Recommended)

Add to your Zed `settings.json` (via `cmd+,` or `ctrl+,`):

```json
{
  "context_servers": {
    "lummenna": {
      "command": {
        "path": "python",
        "args": ["-m", "lummenna.integrations.mcp_server"]
      },
      "settings": {
        "LUMMENNA_DEVICE": "generic"
      }
    }
  }
}
```

Restart Zed. The Lummenna tools will appear in the AI assistant panel.

## Option 2: Zed Slash Command (Workaround)

If context servers are not available in your Zed version, create a custom slash command:

Add to `~/.config/zed/settings.json`:

```json
{
  "assistant": {
    "default_model": {
      "provider": "openai",
      "model": "gpt-4o"
    }
  }
}
```

Then create a Zed extension or use the inline assistant with a custom prompt that calls Lummenna via HTTP. Zed's extension API for custom slash commands is evolving; check the [Zed extensions docs](https://zed.dev/docs/extensions) for the latest.

## Option 3: Zed Extension (Advanced)

Create `~/.config/zed/extensions/lummenna/extension.json`:

```json
{
  "id": "lummenna",
  "name": "Lummenna Memory",
  "version": "0.1.0",
  "authors": ["Lummenna Contributors"],
  "description": "Integrate Lummenna memory palace with Zed AI assistant",
  "repository": "https://github.com/QuantmindSSI/lummenna"
}
```

And `~/.config/zed/extensions/lummenna/src/lib.rs` (WASM extension that calls Lummenna HTTP API). This requires Rust/WASM knowledge. For a pilot, Option 1 (context server) is sufficient.

## Quick Test

1. Start Lummenna: `lummenna serve`
2. Open Zed AI panel (`ctrl+~` or `cmd+~`)
3. Type: `@lummenna search for decisions about caching`
