# Devin + Lummenna Integration

> **Honesty note:** Devin's tooling surface is proprietary and changes frequently; nothing in this document is an officially supported Devin feature, and we have not verified these recipes against a live Devin workspace. The only guaranteed interface is Lummenna's HTTP API (Option 1), which works from any environment that can run `curl`.

## Option 1: Direct HTTP (Bash tool) — Recommended

Devin can execute shell commands. Use the Lummenna REST API directly:

```bash
# Start Lummenna server in background (one-time)
nohup lummenna serve --host 0.0.0.0 --port 8848 > /tmp/lummenna.log 2>&1 &

# Search memories
curl -s -X POST http://localhost:8848/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query":"database indexing decision","top_k":5}' | jq .

# Store a memory
curl -s -X POST http://localhost:8848/v1/store \
  -H "Content-Type: application/json" \
  -d '{"content":"Use composite indexes on (user_id, created_at)","room":"decisions"}' | jq .

# Assemble context for current task
curl -s -X POST http://localhost:8848/v1/assemble \
  -H "Content-Type: application/json" \
  -d '{"query":"implement retry logic","top_k":5}' | jq .

# Log a full conversation turn
curl -s -X POST http://localhost:8848/v1/turn \
  -H "Content-Type: application/json" \
  -d '{"user_msg":"Add retry logic","assistant_msg":"Added exponential backoff with jitter.","room":"conversations"}' | jq .

# Check palace status
curl -s http://localhost:8848/v1/status | jq .
```

## Option 2: Sidecar Container (Advanced)

Devin's sandbox base images are not publicly distributed, so you cannot bake Lummenna into them. Instead, run Lummenna as a sidecar container on the same network and point Devin's shell commands at it:

```bash
docker run -d --name lummenna \
  -p 8848:8848 \
  -v lummenna-store:/root/.lummenna \
  -e LUMMENNA_API_HOST=0.0.0.0 \
  python:3.11-slim \
  sh -c "pip install 'lummenna @ git+https://github.com/QuantmindSSI/lummenna' && lummenna init --device generic && lummenna serve --host 0.0.0.0 --port 8848"
```

Then use the Option 1 curl commands with `http://host.docker.internal:8848` (or the container's address on your Docker network).
