# Aider + Lumena Integration

[Aider](https://aider.chat) is an AI pair programming tool that works with your local git repo. It does not support MCP natively, but you can integrate Lumena via **custom commands** and the **HTTP API**.

## Option 1: Aider Custom Commands

Add to your `~/.aider.conf.yml`:

```yaml
# Aider configuration with Lumena integration

# Custom slash commands
commands:
  # Search Lumena palace
  - name: lumena-search
    description: Search the Lumena memory palace
    run: |
      curl -s -X POST http://localhost:8848/v1/search \
        -H "Content-Type: application/json" \
        -d "{\"query\":\"$args\",\"top_k\":5}"

  # Store a decision in Lumena
  - name: lumena-store
    description: Store a memory in the Lumena palace
    run: |
      curl -s -X POST http://localhost:8848/v1/store \
        -H "Content-Type: application/json" \
        -d "{\"content\":\"$args\",\"room\":\"decisions\"}"

  # Assemble context from Lumena
  - name: lumena-context
    description: Assemble relevant context from Lumena
    run: |
      curl -s -X POST http://localhost:8848/v1/assemble \
        -H "Content-Type: application/json" \
        -d "{\"query\":\"$args\",\"top_k\":5}"
```

Usage in Aider:
```
/lumena-search database indexing decision
/lumena-store We will use composite indexes on (user_id, created_at)
/lumena-context implement retry logic
```

## Option 2: Python Hook (Advanced)

Create `~/.aider/lumena_hook.py`:

```python
"""Aider pre-commit hook that stores file changes in Lumena."""

import subprocess
import sys


def main():
    # Read the diff or commit message from stdin
    content = sys.stdin.read()
    if not content.strip():
        return

    # Store the commit/context in Lumena
    subprocess.run(
        [
            "curl", "-s", "-X", "POST", "http://localhost:8848/v1/store",
            "-H", "Content-Type: application/json",
            "-d", f'{{"content":{repr(content[:2000])},"room":"commits","source_type":"import"}}',
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
aider-lumenaa "Refactor the database layer to use connection pooling"
```
