# Aider + Lumena Integration

[Aider](https://aider.chat) is an AI pair programming tool that works with your local git repo. It does not support MCP natively and has no custom slash-command configuration, so the integration paths below use aider's built-in `/run` command, shell wrappers, and the Lumena HTTP API. Start `lumena serve` first.

## Option 1: Aider's built-in `/run` command

Aider's `/run` executes a shell command and can add its output to the chat. Use it to query Lumena mid-session:

```
/run curl -s -X POST http://localhost:8848/v1/search -H "Content-Type: application/json" -d '{"query":"database indexing decision","top_k":5}'

/run curl -s -X POST http://localhost:8848/v1/store -H "Content-Type: application/json" -d '{"content":"Use composite indexes on (user_id, created_at)","room":"decisions"}'

/run curl -s -X POST http://localhost:8848/v1/assemble -H "Content-Type: application/json" -d '{"query":"implement retry logic","top_k":5}'
```

To avoid typing the full curl each time, wrap them in small scripts on your `PATH` (e.g. `lumena-search`, `lumena-store`) and call `/run lumena-search database indexing decision`.

## Option 2: Python Hook (Advanced)

Create `~/.aider/lumena_hook.py`:

```python
"""Aider pre-commit hook that stores file changes in Lumena."""

import json
import subprocess
import sys


def main():
    # Read the diff or commit message from stdin
    content = sys.stdin.read()
    if not content.strip():
        return

    # Store the commit/context in Lumena. json.dumps produces valid JSON
    # (Python repr() would emit single-quoted strings the API rejects).
    body = json.dumps(
        {"content": content[:2000], "room": "commits", "source_type": "import"}
    )
    subprocess.run(
        [
            "curl", "-s", "-X", "POST", "http://localhost:8848/v1/store",
            "-H", "Content-Type: application/json",
            "-d", body,
        ],
        capture_output=True,
    )


if __name__ == "__main__":
    main()
```

## Option 3: Shell Alias

Add to your shell profile:

```bash
# Aider with Lumena context injection
aider-lumena() {
  context=$(curl -s -X POST http://localhost:8848/v1/assemble \
    -H "Content-Type: application/json" \
    -d "{\"query\":\"$*\",\"top_k\":3}" | jq -r '.assembled_context // empty')
  if [ -n "$context" ]; then
    aider --message "$context\n\n$*"
  else
    aider --message "$*"
  fi
}
```

Usage:
```bash
aider-lumena "Refactor the database layer to use connection pooling"
```
