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

## Option 2: Community Sidecar Container

Devin's sandbox base images are not publicly distributed, so run Lummenna as a
sidecar on the same network. The Community Dockerfile and its pinned package are
served directly from lummenna.com; no source repository access is required.

```bash
curl --fail --show-error --silent --remote-name \
  https://lummenna.com/downloads/lummenna-community-1.0.1rc1.Dockerfile

docker build --pull \
  --file lummenna-community-1.0.1rc1.Dockerfile \
  --tag lummenna-community:1.0.1rc1 .
```

Follow the model-provisioning and authenticated run commands at
`https://lummenna.com/community`. Then use the Option 1 API calls with
`http://lummenna:8848` from another container on the same Docker network.
