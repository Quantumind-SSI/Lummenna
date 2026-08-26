# Goose (Block) + Lumena Integration

[Goose](https://github.com/block/goose) is an open-source AI agent from Block (formerly Square) that supports **MCP servers** natively.

## Configuration

Add Lumena to your Goose configuration:

```bash
goose configure
# Select "Add Extension" → "Command-line Extension"
# Name: lummenna
# Command: python -m lummenna.integrations.mcp_server
# Environment variables:
#   LUMENA_DEVICE=generic
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
      LUMENA_DEVICE: generic
      LUMENA_STORE_PATH: "${HOME}/.lumena/store"
      LUMENA_MODEL_PATH: "${HOME}/.lumena/models"
```

## Usage

Start Goose with Lumena enabled:

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

Goose will automatically route these to the Lumena MCP tools.

## Advanced: Goose + Lumena in CI/CD

Use Goose with Lumena for persistent CI/CD agents:

```bash
export LUMENA_API_KEY=your-key
goose session --instructions "Always search Lumena before making architectural decisions."
```
