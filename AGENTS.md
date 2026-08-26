# AGENTS.md — Engineering Constitution for Lummenna

This file governs **every** contribution to Lummenna made by an AI agent or a
human. It is loaded automatically by coding agents at the start of each session.
Its two supreme rules — **No Vaporware** and **No False Claims** — are not
guidance; they are enforced in CI by the offline guardrail gate
(`python -m lummenna.dev`, see `lummenna/dev/guardrails.py`) and are non-negotiable.

---

## Rule 1 — No Vaporware

Every function, method, and module that lands must be **complete, executable,
and correct**. A description of code is not code. An outline is not an
implementation.

**Prohibited (each is a failed contribution):**

- `TODO`, `FIXME`, `XXX`, "implement this", "your code here" markers.
- Function/method bodies that are only `pass`, `...`, or
  `raise NotImplementedError` used as a placeholder.
- "for now", "in a real implementation", "in production you would…",
  "left as an exercise", "rest of the implementation follows the same pattern".
- Calls to functions never defined; imports of modules that do not exist and
  were not created in the same change.
- Schemas, configs, or files referenced but never written.
- Partial solutions that require a human to iterate them to functionality.

If a task genuinely requires 400 lines, write 400 lines. If it needs a schema, a
query layer, a service layer, and an API handler — write all four, completely.
The iteration budget for a correctly-executed implementation is **one**.

**The only legitimate empty bodies** are interface contracts: methods decorated
`@abstractmethod`, methods on an `abc.ABC` subclass, or members of a
`typing.Protocol`. The guardrail exempts these automatically. Any other genuine
exception must carry an explicit `# noqa: guardrail` on the offending line, with
a comment justifying it.

## Rule 2 — No False Claims

Never describe something as working, available, or complete when it is not.

- **Code** must not claim to do something it does not do. If a code path is a
  simplification, it must actually be correct for its stated scope — not a fake
  standing in for real behaviour.
- **Docs, READMEs, and marketing** (including `COMMERCIAL-LICENSE.md`) must only
  advertise capabilities that **exist in the code today**.
- **Commercial / Pro features** must be enforced by
  `LummennaConfig.assert_licensed(<feature>)` before they are listed as available.
  A feature that is declared but ungated is either a revenue leak (give it a
  gate) or not real (move it to the roadmap).

## Rule 3 — Roadmap ≠ Present Capability

Unbuilt work is legitimate — **as an explicitly-labelled plan**, never as a
stub or a "done" claim.

- Future features live in `ROADMAP.md`, clearly marked as **not yet available**,
  each with concrete, enterprise-grade acceptance criteria.
- Planned Pro features live in `PLANNED_PRO_FEATURES` in `lummenna/licensing.py`,
  **separate** from the enforceable `PRO_FEATURES` registry. Nothing in
  `PRO_FEATURES` may be undelivered; nothing in `PLANNED_PRO_FEATURES` may be
  advertised as currently available.
- When you build a planned feature to completion, move it from the roadmap /
  `PLANNED_PRO_FEATURES` into the shipped set **and** add its license gate in the
  same change.

---

## Enforcement

The guardrail gate is deterministic, offline, and runs in CI on every changed
Python file (`.github/workflows/ci.yml` → `guardrails` job). Rules
`no-vaporware-stub` and `no-vaporware-marker` fail the build at `HIGH` severity.

Run it locally before committing:

```bash
# scan specific files
python -m lummenna.dev path/to/file.py

# scan everything changed vs main
git diff --name-only origin/main...HEAD -- '*.py' | python -m lummenna.dev --stdin
```

A passing guardrail scan is **necessary but not sufficient**. Before delivering
code also confirm: it runs unmodified with stated dependencies; every function
has a real implementation; happy path and error paths are traced; every import
and call resolves; and it solves the actual problem at the stated scale.

## Definition of Done

A change is done only when **all** of the following hold:

1. `python -m lummenna.dev` passes on every changed `.py` file (no HIGH/BLOCKER).
2. `ruff check` and `mypy` are clean on changed files.
3. Non-trivial code ships with tests covering happy path, boundaries, and error
   paths, and the suite is green.
4. Docs and capability claims match the code exactly — nothing over-promised.
5. New Pro capabilities are license-gated; planned work is in `ROADMAP.md` /
   `PLANNED_PRO_FEATURES`, not stubbed into the codebase.
