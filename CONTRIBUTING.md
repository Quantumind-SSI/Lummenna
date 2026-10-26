# Contributing to Lummenna

Thank you for considering contributing to Lummenna. Every contribution — code, documentation, design, testing, or community support — helps local AI agents develop minds of their own.

---

## Code of Conduct

This project adheres to a standard of respect, inclusivity, and intellectual honesty. We expect all contributors to:

- Be respectful in all interactions
- Welcome newcomers and help them learn
- Accept constructive criticism gracefully
- Focus on what is best for the community and the project

---

## Contributor License Agreement (CLA)

**Lummenna is dual-licensed** — the Community Edition under **AGPL-3.0-or-later**,
and a **commercial Pro Edition** under a proprietary license from QuantumindSSI.
To keep this model legally sound, **every contributor must agree to the Lummenna
Contributor License Agreement (CLA) before their contribution can be merged.**

By submitting a contribution (pull request, patch, or any other form) you
represent and agree that:

1. **Ownership.** You are the original author of the contribution, or you have
   the right to submit it, and it does not knowingly infringe any third party's
   rights.
2. **Grant of rights.** You grant QuantumindSSI a perpetual, worldwide,
   non-exclusive, royalty-free, irrevocable license — **including the right to
   sublicense and relicense** — to use, reproduce, modify, distribute, and
   commercially exploit your contribution, **under both the AGPL-3.0 Community
   license and the QuantumindSSI commercial license**, in current and future
   versions of Lummenna.
3. **You retain copyright** in your contribution; this CLA is a license grant,
   not an ownership transfer, but it gives QuantumindSSI everything required to
   dual-license Lummenna.
4. **No obligation.** QuantumindSSI is not obligated to use or include your
   contribution.

**How to sign:** For your first contribution, add your name to `AUTHORS` and
include the line `I agree to the Lummenna CLA (see CONTRIBUTING.md).` in your pull
request description. For significant contributions, a signed CLA may be
requested and stored privately outside the public repository.

> Without an agreed CLA, contributions **cannot be merged**, because they could
> not lawfully be offered under the commercial license.

---

## How to Contribute

### 0. Commit identity policy (enforced)

Every commit and push to `origin/main` **must be made by the `QuantumindSSI`
account**. This is enforced at two layers:

- **Server-side (authoritative):** a GitHub repository ruleset on the default
  branch requires the commit *author* and *committer* email to be the
  `QuantumindSSI` account's address. Non-conforming pushes are rejected by
  GitHub.
- **Local (fast feedback):** versioned git hooks in `.githooks/` block commits
  and pushes with the wrong identity before they leave your machine.

Enable the local checks once after cloning:

```bash
./scripts/setup-identity.sh
```

This sets `core.hooksPath=.githooks` and your local commit identity. Pushing
also requires a token with write access on the `QuantumindSSI` account (and the
`workflow` scope when changing `.github/workflows/`).

### 1. Reporting Bugs

Before opening an issue:

1. Search existing issues to avoid duplicates
2. Test against the latest `main` branch

When reporting, include:

```markdown
**Device:** (e.g., Raspberry Pi 5 4GB, x86_64 desktop)
**Lummenna version:** (e.g., 0.1.0)
**Python version:** (e.g., 3.12.3)
**What happened:**
**What you expected:**
**Steps to reproduce:**
**Relevant logs:** (with `[LUMMENNA:*]` prefix lines)
```

### 2. Suggesting Features

Feature requests should align with Lummenna's core philosophy:

> Sovereign-first. Edge-native. Biologically-grounded. Palace-structured.

Open a GitHub Discussion first for major features. For small enhancements, an issue is fine.

### 3. Contributing Code

#### Development Setup

```bash
git clone https://github.com/QuantmindSSI/lummenna.git
cd lummenna
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

#### Code Standards

- **Lint:** `ruff check lummenna/` must pass
- **Tests:** `pytest tests/` must pass with >50% coverage (target: >70%)
- **Types:** Add type hints for new public APIs
- **Docstrings:** Google-style for all public modules/classes/functions
- **Naming:** Follow the twin-force lexicon (room, locus, dim, illuminate — not delete, slot, fetch)

#### Branch Naming

```
feat/<short-description>       # New feature
fix/<short-description>        # Bug fix
docs/<short-description>       # Documentation
refactor/<short-description>   # Code restructuring
test/<short-description>       # Test-only changes
```

#### Commit Messages

Use conventional commits:

```
feat: add optical forgetting scheduler for RPi5
fix: resolve race condition in consolidation pipeline
docs: update deployment guide with Nginx example
test: add BEIR benchmark for retrieval_dense
refactor: simplify TFC state machine transitions
```

#### Pull Request Process

1. Fork the repository
2. Create a feature branch
3. Make your changes with tests
4. Run `ruff check lummenna/` and `pytest tests/`
5. Update `CHANGELOG.md` under `[Unreleased]`
6. Open a PR with a clear description and reference any related issues
7. Address review feedback
8. A maintainer will merge when CI passes and review is approved

---

## Architecture Guidelines

### The Twin-Force Rule

Every change should respect the duality:

- **Memory (Force A)** = substantive, concrete, archival
- **Context (Force B)** = active, verbal, ephemeral
- **Lummenna (Unification)** = light, balance, the intersection

### Adding a New Room

Rooms are top-level categories in the palace. To add one:

1. Define in `lummenna/data/schema.sql` (if schema change needed)
2. Add retrieval path in `lummenna/force/mnemonic/retrieval_*.py`
3. Register in `lummenna/search.py` pipeline
4. Add test in `tests/test_retrieval_*.py`
5. Update the relevant official root document when public behavior changes

### Adding a Forgetting Level

The forgetting stack is L1-L4. If you need L5:

1. Create `lummenna/force/mnemonic/forgetting_l5_*.py`
2. Hook into `lummenna/sleep.py` consolidation scheduler
3. Add compliance audit logging
4. Write test in `tests/test_forgetting_*.py`

---

## Documentation Contributions

Docs live in:
- `README.md` — project overview and quickstart
- `DEPLOYMENT.md` — production guide
- `SECURITY.md` — security model and disclosure policy
- `INTEGRATIONS.md` — supported integration setup
- Inline docstrings — API reference

The local `docs/` directory contains internal development and brand material and
is intentionally excluded from Git. Do not reference it from public changes.

When documenting:
- Use the Lummenna lexicon (Memory Palace, Context Window, Dimming, Consolidation, etc.)
- Provide working code examples
- Include expected output where helpful
- Keep line length <= 100 characters

---

## Testing Guidelines

### Unit Tests

```python
def test_consolidation_creates_summary_chunk(db_conn, mock_embedder):
    """Consolidation should produce a summary chunk with V(m) > 0.5."""
    from lummenna.force.mnemonic.consolidation import consolidate_room

    result = consolidate_room(db_conn, room_name="test_room")
    assert result > 0
```

### Integration Tests

Use `tests/conftest.py` fixtures:
- `db_conn` — in-memory SQLite with full schema
- `mock_embedder` — deterministic embedding generator
- `test_config` — generic device profile

### Benchmarks

Performance-critical changes should include benchmarks:

```bash
cd benchmarks
python benchmark_retrieval.py --backend sqlite-vec --dataset beir/scifact
```

---

## Recognition

Contributors are recognized as **Luminary** members. Significant contributions receive:

- Attribution in `CHANGELOG.md` and release notes
- Luminary badge in GitHub profile
- Optional: enamel pin for major architectural contributions

---

## Questions?

- GitHub Discussions: https://github.com/QuantmindSSI/lummenna/discussions
- Matrix: #lummenna:matrix.org
- Security: security@lummenna.ai

---

*Thank you for helping local AI agents remember what matters and forget what doesn't.*
