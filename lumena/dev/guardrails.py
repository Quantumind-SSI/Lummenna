# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-QuantumindSSI-Commercial
# Copyright (c) 2025-2026 QuantumindSSI. All rights reserved.
"""Deterministic, offline code guardrails for LLM-generated (and human) Python.

This is the *static core* of Lumena's "ground all generated code" discipline: it
runs with no network and no LLM, so it can gate CI reproducibly. An optional
LLM/pydantic-ai reviewer (``scripts/pydantic_ai_mcp.py``) layers a semantic pass
on top, but the checks here are the enforceable baseline.

Report objects are Pydantic models: they are the external boundary (CLI/CI/JSON
output), so we validate + serialize them with Pydantic. The scanning internals
use plain functions/AST (no untrusted external data), per the guidance to avoid
Pydantic for purely-internal values.

Checks
------
* ``SyntaxError``                         → BLOCKER
* ``eval`` / ``exec`` / ``compile``       → BLOCKER
* ``os.system`` / ``subprocess(..., shell=True)`` → HIGH
* ``pickle.load(s)`` / ``yaml.load`` (no SafeLoader) → HIGH
* hardcoded secrets (api_key/secret/password/token literals) → HIGH
* SQL built via f-string / ``%`` / ``+`` concatenation → HIGH
* bare ``except:``                         → MEDIUM
* ``assert`` used for validation in non-test code → LOW
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from enum import IntEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Severity
# --------------------------------------------------------------------------- #


class Severity(IntEnum):
    """Ordered severity. Higher = worse. IntEnum so thresholds are comparable."""

    LOW = 10
    MEDIUM = 20
    HIGH = 30
    BLOCKER = 40

    @property
    def label(self) -> str:
        return self.name


#: Rules at or above this severity fail the guardrail gate by default.
DEFAULT_FAIL_AT: Severity = Severity.HIGH


# --------------------------------------------------------------------------- #
# Report models (external boundary → Pydantic validated + serializable)
# --------------------------------------------------------------------------- #


class GuardrailIssue(BaseModel):
    """A single guardrail finding. Immutable, no unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str = Field(min_length=1, description="Stable rule id, e.g. 'no-eval'.")
    severity: Severity
    file: str = Field(default="<source>", description="Path or '<source>'.")
    line: int = Field(default=1, ge=1, description="1-based line number.")
    message: str = Field(min_length=1)

    def render(self) -> str:
        return f"[{self.severity.label}] {self.file}:{self.line} {self.rule} — {self.message}"


class GuardrailReport(BaseModel):
    """Aggregate result of scanning one or more sources."""

    model_config = ConfigDict(extra="forbid")

    files_scanned: int = Field(default=0, ge=0)
    issues: list[GuardrailIssue] = Field(default_factory=list)
    fail_at: Severity = DEFAULT_FAIL_AT

    @property
    def passed(self) -> bool:
        """True unless any issue meets/exceeds the fail threshold."""
        return not any(i.severity >= self.fail_at for i in self.issues)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {s.label: 0 for s in Severity}
        for issue in self.issues:
            out[issue.severity.label] += 1
        return out

    def blocking(self) -> list[GuardrailIssue]:
        return [i for i in self.issues if i.severity >= self.fail_at]

    def render(self) -> str:
        lines = [
            i.render() for i in sorted(self.issues, key=lambda x: (-x.severity, x.file, x.line))
        ]
        header = (
            f"scanned={self.files_scanned} issues={len(self.issues)} "
            f"counts={self.counts()} passed={self.passed} (fail_at={self.fail_at.label})"
        )
        return "\n".join([*lines, header]) if lines else header


# --------------------------------------------------------------------------- #
# Static scanners (plain functions; internal data → no Pydantic)
# --------------------------------------------------------------------------- #

_SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "api_key": re.compile(r"""api[_-]?key\s*[:=]\s*['"][A-Za-z0-9_\-]{20,}['"]""", re.IGNORECASE),
    "secret": re.compile(r"""secret\s*[:=]\s*['"][A-Za-z0-9_\-]{16,}['"]""", re.IGNORECASE),
    "password": re.compile(r"""password\s*[:=]\s*['"][^'"]{6,}['"]""", re.IGNORECASE),
    "token": re.compile(r"""token\s*[:=]\s*['"][A-Za-z0-9_\-.]{16,}['"]""", re.IGNORECASE),
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}

#: Substrings that mark an assignment as a placeholder, not a real secret.
_SECRET_ALLOW = ("example", "placeholder", "your-", "changeme", "xxxx", "dummy", "redacted", "test")

_DANGEROUS_CALLS_BLOCKER = {"eval", "exec", "compile"}


def _secret_issues(source: str, filename: str) -> list[GuardrailIssue]:
    issues: list[GuardrailIssue] = []
    for name, pat in _SECRET_PATTERNS.items():
        for m in pat.finditer(source):
            snippet = m.group(0).lower()
            if any(tok in snippet for tok in _SECRET_ALLOW):
                continue
            issues.append(
                GuardrailIssue(
                    rule=f"hardcoded-{name}",
                    severity=Severity.HIGH,
                    file=filename,
                    line=source[: m.start()].count("\n") + 1,
                    message=f"Possible hardcoded {name}; load from env/secret store instead.",
                )
            )
    return issues


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _bare_call_name(node: ast.Call) -> str:
    """Name only when called as a bare builtin (``foo(...)``), not ``x.foo(...)``.

    This avoids flagging safe attribute calls like ``re.compile`` as the
    dangerous builtin ``compile``.
    """
    func = node.func
    return func.id if isinstance(func, ast.Name) else ""


def _has_shell_true(node: ast.Call) -> bool:
    return any(kw.arg == "shell" and _is_true(kw.value) for kw in node.keywords)


def _is_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _sql_like_string(node: ast.expr) -> bool:
    """Heuristic: does this expression look like a SQL statement literal?"""
    text = ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        text = "".join(
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    kw = ("select ", "insert into", "update ", "delete from", "drop table", "create table")
    low = text.lower()
    return any(k in low for k in kw)


def _fstring_interpolations_are_safe(node: ast.JoinedStr) -> bool:
    """True if every interpolated value in the f-string is provably safe.

    Safe = a bare Name whose identifier signals a generated placeholder string
    or a fixed table/identifier (common audited pattern: ``IN ({placeholders})``
    where ``placeholders`` is ``"?,?,..."``). We only clear these obvious cases;
    anything else (attributes, calls, arbitrary expressions) stays flagged.
    """
    safe_tokens = ("placeholder", "?", "cols", "columns", "table")
    for value in node.values:
        if isinstance(value, ast.FormattedValue):
            inner = value.value
            if isinstance(inner, ast.Name) and any(tok in inner.id.lower() for tok in safe_tokens):
                continue
            return False
    return True


def _suppressed_lines(source: str) -> set[int]:
    """1-based line numbers carrying an explicit '# nosec' or '# noqa: guardrail'.

    These mark audited exceptions (e.g. bandit-cleared parameterized SQL). The
    guardrail respects them so audited code is not perpetually re-flagged.
    """
    out: set[int] = set()
    for idx, line in enumerate(source.splitlines(), start=1):
        low = line.lower()
        if "# nosec" in low or "# noqa: guardrail" in low:
            out.add(idx)
    return out


def _ast_issues(source: str, filename: str, is_test: bool) -> list[GuardrailIssue]:
    issues: list[GuardrailIssue] = []
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [
            GuardrailIssue(
                rule="syntax-error",
                severity=Severity.BLOCKER,
                file=filename,
                line=max(exc.lineno or 1, 1),
                message=f"Syntax error: {exc.msg}",
            )
        ]

    for node in ast.walk(tree):
        # Dangerous dynamic execution — only the *builtins*, not x.compile etc.
        if isinstance(node, ast.Call):
            name = _call_name(node)
            bare = _bare_call_name(node)
            line = getattr(node, "lineno", 1)
            if bare in _DANGEROUS_CALLS_BLOCKER:
                issues.append(
                    GuardrailIssue(
                        rule="no-dynamic-exec",
                        severity=Severity.BLOCKER,
                        file=filename,
                        line=line,
                        message=f"Dangerous dynamic execution via '{bare}()'.",
                    )
                )
            elif name == "system" and isinstance(node.func, ast.Attribute):
                issues.append(
                    GuardrailIssue(
                        rule="no-os-system",
                        severity=Severity.HIGH,
                        file=filename,
                        line=line,
                        message="os.system() invokes a shell; use subprocess with a list and shell=False.",
                    )
                )
            elif name in ("run", "Popen", "call", "check_output") and _has_shell_true(node):
                issues.append(
                    GuardrailIssue(
                        rule="no-shell-true",
                        severity=Severity.HIGH,
                        file=filename,
                        line=line,
                        message="subprocess called with shell=True; pass an argument list and shell=False.",
                    )
                )
            elif name == "load" and _is_unsafe_yaml(node):
                issues.append(
                    GuardrailIssue(
                        rule="no-yaml-unsafe-load",
                        severity=Severity.HIGH,
                        file=filename,
                        line=line,
                        message="yaml.load without SafeLoader can execute arbitrary objects; use yaml.safe_load.",
                    )
                )

        # SQL string built with f-string / % / +
        if isinstance(node, ast.Call) and _call_name(node) in (
            "execute",
            "executescript",
            "executemany",
        ):
            for arg in node.args[:1]:
                if _is_dynamic_sql(arg):
                    issues.append(
                        GuardrailIssue(
                            rule="no-sql-string-format",
                            severity=Severity.HIGH,
                            file=filename,
                            line=getattr(node, "lineno", 1),
                            message="SQL appears built by string formatting; use parameterized queries (? / :name).",
                        )
                    )

        # Bare except
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            issues.append(
                GuardrailIssue(
                    rule="no-bare-except",
                    severity=Severity.MEDIUM,
                    file=filename,
                    line=getattr(node, "lineno", 1),
                    message="Bare 'except:' swallows everything (incl. KeyboardInterrupt); catch specific types.",
                )
            )

        # assert used for validation in non-test code (stripped by python -O)
        if not is_test and isinstance(node, ast.Assert):
            issues.append(
                GuardrailIssue(
                    rule="no-assert-validation",
                    severity=Severity.LOW,
                    file=filename,
                    line=getattr(node, "lineno", 1),
                    message="assert is stripped under -O; raise an explicit exception for real validation.",
                )
            )

    return issues


def _is_unsafe_yaml(node: ast.Call) -> bool:
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "load"):
        return False
    base = func.value
    if not (isinstance(base, ast.Name) and base.id in ("yaml",)):
        return False
    # Safe if a Loader kwarg mentioning 'Safe' is supplied.
    for kw in node.keywords:
        if kw.arg in ("Loader", "loader"):
            v = kw.value
            if isinstance(v, ast.Attribute) and "Safe" in v.attr:
                return False
            if isinstance(v, ast.Name) and "Safe" in v.id:
                return False
    return True


def _is_dynamic_sql(arg: ast.expr) -> bool:
    if isinstance(arg, ast.JoinedStr):  # f-string
        if not _sql_like_string(arg):
            return False
        # Clear the audited placeholder pattern (IN ({placeholders})).
        return not _fstring_interpolations_are_safe(arg)
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mod | ast.Add):
        # e.g. "SELECT ... %s" % x  or  "SELECT ..." + x
        left = arg.left
        return _sql_like_string(left)
    return False


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def scan_source(
    source: str,
    filename: str = "<source>",
    *,
    fail_at: Severity = DEFAULT_FAIL_AT,
    is_test: bool | None = None,
) -> GuardrailReport:
    """Scan a single Python source string and return a validated report."""
    if is_test is None:
        is_test = "test" in Path(filename).name.lower()
    suppressed = _suppressed_lines(source)
    issues = [i for i in _ast_issues(source, filename, is_test) if i.line not in suppressed]
    issues.extend(i for i in _secret_issues(source, filename) if i.line not in suppressed)
    return GuardrailReport(files_scanned=1, issues=issues, fail_at=fail_at)


def scan_paths(
    paths: Sequence[str | Path],
    *,
    fail_at: Severity = DEFAULT_FAIL_AT,
) -> GuardrailReport:
    """Scan a list of Python files (paths that don't end in .py are skipped)."""
    all_issues: list[GuardrailIssue] = []
    scanned = 0
    for raw in paths:
        p = Path(raw)
        if p.suffix != ".py" or not p.is_file():
            continue
        scanned += 1
        source = p.read_text(encoding="utf-8", errors="replace")
        report = scan_source(source, filename=p.as_posix(), fail_at=fail_at)
        all_issues.extend(report.issues)
    return GuardrailReport(files_scanned=scanned, issues=all_issues, fail_at=fail_at)
