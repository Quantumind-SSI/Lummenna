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
export LUMMENNA_STORE_PATH=/var/lib/lummenna/store
goose session --instructions "Always search Lummenna before making architectural decisions."
```

Point `LUMMENNA_STORE_PATH` at a persisted volume so the palace survives between
runs; a default in-workspace store is discarded with the build container.
`LUMMENNA_API_KEY` is not used here: it authenticates the HTTP API, and the MCP
server communicates over stdio, inheriting the trust boundary of the process
that spawned it.
