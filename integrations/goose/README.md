# Goose (Block) + Lummenna Integration

[Goose](https://github.com/block/goose) is an open-source AI agent from Block (formerly Square) that supports **MCP servers** natively.

## Configuration

Add Lummenna to your Goose configuration:

```bash
goose configure
# Select "Add Extension" → "Command-line Extension"
# Name: lummenna
# Command: python -m lummenna.integrations.mcp_server
# Environment variables:
#   LUMMENNA_DEVICE=generic
```

Or edit `~/.config/goose/config.yaml` directly:

```yaml
extensions:
  lummenna:
    type: stdio
    cmd: python
    args:
      - -m
      - lummenna.integrations.mcp_server
    env:
      LUMMENNA_DEVICE: generic
      LUMMENNA_STORE_PATH: "${HOME}/.lummenna/store"
      LUMMENNA_MODEL_PATH: "${HOME}/.lummenna/models"
```

## Usage

Start Goose with Lummenna enabled:

```bash
goose session
```

Inside the session:

```
> Search my memory palace for decisions about caching
> Remember that we use Redis for session storage
> Assemble context for implementing OAuth
> What is the status of my memory palace?
```

Goose will automatically route these to the Lummenna MCP tools.

## Advanced: Goose + Lummenna in CI/CD

Use Goose with Lummenna for persistent CI/CD agents:

```bash
export LUMMENNA_API_KEY=your-key
goose session --instructions "Always search Lummenna before making architectural decisions."
```
