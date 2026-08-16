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
* vaporware stub bodies (``pass`` / ``...`` / ``raise NotImplementedError``
  as a function/class placeholder, outside a real ABC/Protocol) → HIGH
* vaporware / false-claim text markers (TODO, FIXME, "implement this",
  "your code here", "in a real implementation", "left as an exercise",
  "rest of the implementation", "placeholder", ...) → HIGH
* bare ``except:``                         → MEDIUM
* ``assert`` used for validation in non-test code → LOW

No-Vaporware / No-False-Claims discipline
-----------------------------------------
The stub and marker rules exist to enforce Lumena's engineering constitution
(see ``AGENTS.md``): every function that ships must be fully implemented, and
nothing may be described as working when it is not. Unbuilt capabilities belong
in ``ROADMAP.md`` as explicitly-labelled future work — never as a stubbed body
or a "done" claim in code. Legitimate abstractions (``abc.ABC`` subclasses,
``typing.Protocol`` bodies, ``@abstractmethod``) are exempt because their empty
bodies are the interface contract, not a placeholder. Genuinely-necessary
exceptions on any rule can be marked ``# noqa: guardrail`` on the line.
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

# --------------------------------------------------------------------------- #
# Anti-vaporware / no-false-claims markers
# --------------------------------------------------------------------------- #

#: Case-insensitive substrings that signal unfinished work or a false "done"
#: claim embedded in code or comments. Matched against comment text and string
#: literals only (see ``_marker_issues``) so ordinary identifiers are safe.
#: Each entry is a (needle, human-message) pair.
_VAPORWARE_MARKERS: tuple[tuple[str, str], ...] = (
    ("todo", "Unfinished-work marker 'TODO' — implement fully or move to ROADMAP.md."),
    ("fixme", "Unfinished-work marker 'FIXME' — fix before landing; do not ship broken code."),
    ("implement this", "'implement this' placeholder — write the real implementation."),
    ("implement me", "'implement me' placeholder — write the real implementation."),
    ("your code here", "'your code here' placeholder — write the real implementation."),
    ("your implementation here", "placeholder text — write the real implementation."),
    ("for now", "'for now' signals a temporary shortcut — ship the real behaviour."),
    ("in a real implementation", "false-claim marker — this IS the implementation; make it real."),
    ("in a real system", "false-claim marker — this IS the system; make it real."),
    ("in production you would", "false-claim marker — build the production behaviour now."),
    ("left as an exercise", "iteration-deferral marker — complete the implementation."),
    ("rest of the implementation", "scaffold-deception marker — write the whole implementation."),
    ("rest of implementation", "scaffold-deception marker — write the whole implementation."),
    ("placeholder implementation", "placeholder implementation — write the real one."),
    ("placeholder for now", "placeholder marker — write the real implementation."),
    ("just a placeholder", "placeholder marker — write the real implementation."),
    ("is a placeholder", "placeholder marker — write the real implementation."),
    ("this is a stub", "stub marker — provide a real implementation or gate as roadmap."),
    ("stubbed out", "'stubbed out' — provide a real implementation or gate as roadmap."),
    ("not implemented yet", "false-availability claim — implement it or mark it roadmap in docs."),
    ("coming soon", "false-availability claim — do not claim availability before it exists."),
    ("mock implementation", "'mock implementation' in non-test code — ship the real one."),
    ("dummy implementation", "'dummy implementation' in non-test code — ship the real one."),
)

#: A ``pass`` / ``...`` / ``raise NotImplementedError`` body is legitimate inside
#: these abstraction contexts (the empty body IS the contract). We detect the
#: context via base classes and decorators rather than banning the tokens.
_ABSTRACT_BASES: frozenset[str] = frozenset({"ABC", "ABCMeta", "Protocol"})
_ABSTRACT_DECORATORS: frozenset[str] = frozenset(
    {"abstractmethod", "abstractproperty", "abstractclassmethod", "abstractstaticmethod"}
)


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


def _comment_and_string_spans(source: str) -> list[tuple[int, str]]:
    """Return ``(line_number, text)`` for every comment and string literal.

    Vaporware markers are only meaningful inside human-readable text (comments,
    docstrings, string literals). Restricting the search to these spans means an
    ordinary identifier like ``todo_list`` or ``placeholder_sql`` is never
    flagged — only actual prose is. Falls back to a whole-line scan if the source
    cannot be tokenised (a syntax error is reported separately as a BLOCKER).
    """
    import io
    import tokenize

    spans: list[tuple[int, str]] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                spans.append((tok.start[0], tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Degrade gracefully: scan raw lines so markers are still caught even
        # when tokenisation fails. The syntax error itself is a separate BLOCKER.
        for idx, line in enumerate(source.splitlines(), start=1):
            spans.append((idx, line))
    return spans


def _marker_issues(source: str, filename: str, is_test: bool) -> list[GuardrailIssue]:
    """Flag vaporware / false-claim text markers in comments and string literals.

    Skipped entirely for test files (fixtures legitimately contain the words)
    and for this guardrails module, whose rule table lists the markers as data.
    """
    if is_test or Path(filename).name == "guardrails.py":
        return []
    issues: list[GuardrailIssue] = []
    for line_no, text in _comment_and_string_spans(source):
        low = text.lower()
        for needle, message in _VAPORWARE_MARKERS:
            if needle in low:
                issues.append(
                    GuardrailIssue(
                        rule="no-vaporware-marker",
                        severity=Severity.HIGH,
                        file=filename,
                        line=line_no,
                        message=message,
                    )
                )
                # One finding per span is enough; avoid stacking on one comment.
                break
    return issues


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name):
            names.add(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.add(dec.attr)
    return names


def _class_base_names(node: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    for kw in node.keywords:
        # metaclass=ABCMeta
        if isinstance(kw.value, ast.Name):
            names.add(kw.value.id)
        elif isinstance(kw.value, ast.Attribute):
            names.add(kw.value.attr)
    return names


def _is_abstract_class(node: ast.ClassDef) -> bool:
    return bool(_class_base_names(node) & _ABSTRACT_BASES)


def _body_is_stub(body: list[ast.stmt]) -> tuple[str, int] | None:
    """Classify a function/method body as a placeholder stub, or ``None``.

    Returns ``(reason, line)`` — where ``line`` is the offending statement's line
    so a ``# noqa: guardrail`` placed on it suppresses the finding — when the body
    is *only* one of:
    * ``pass``
    * ``...`` (an ``Expr`` wrapping the ``Ellipsis`` constant)
    * ``raise NotImplementedError`` (with or without a message/call)

    An optional leading docstring is allowed and ignored, so a documented but
    unimplemented function is still correctly flagged as a stub.
    """
    stmts = list(body)
    if (
        stmts
        and isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        stmts = stmts[1:]  # drop the docstring
    if len(stmts) != 1:
        return None
    only = stmts[0]
    line = getattr(only, "lineno", 1)
    if isinstance(only, ast.Pass):
        return "empty 'pass' body — implement the function fully.", line
    if (
        isinstance(only, ast.Expr)
        and isinstance(only.value, ast.Constant)
        and only.value.value is Ellipsis
    ):
        return "'...' placeholder body — implement the function fully.", line
    if isinstance(only, ast.Raise):
        exc = only.exc
        name = ""
        if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
            name = exc.func.id
        elif isinstance(exc, ast.Name):
            name = exc.id
        if name == "NotImplementedError":
            return "raises NotImplementedError — implement it or declare it abstract.", line
    return None


def _stub_issues(source: str, filename: str, is_test: bool) -> list[GuardrailIssue]:
    """Flag placeholder function/method bodies, exempting real abstractions.

    A body of ``pass`` / ``...`` / ``raise NotImplementedError`` is a vaporware
    stub UNLESS it is an interface contract: an ``@abstractmethod``, a method on
    an ``abc.ABC`` subclass, or a member of a ``typing.Protocol``. Test files are
    exempt (fixtures deliberately use stubs).
    """
    if is_test:
        return []
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []  # reported as a BLOCKER by _ast_issues
    issues: list[GuardrailIssue] = []
    # Track abstract-class context so methods inside them are exempt.
    abstract_class_stack: list[bool] = [False]

    class _Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            abstract_class_stack.append(_is_abstract_class(node))
            self.generic_visit(node)
            abstract_class_stack.pop()

        def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            stub = _body_is_stub(node.body)
            if stub is not None:
                reason, line = stub
                is_abstract = abstract_class_stack[-1] or bool(
                    _decorator_names(node) & _ABSTRACT_DECORATORS
                )
                if not is_abstract:
                    issues.append(
                        GuardrailIssue(
                            rule="no-vaporware-stub",
                            severity=Severity.HIGH,
                            file=filename,
                            line=line,
                            message=f"Stub '{node.name}': {reason}",
                        )
                    )
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_func(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_func(node)

    _Visitor().visit(tree)
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
    issues.extend(i for i in _stub_issues(source, filename, is_test) if i.line not in suppressed)
    issues.extend(i for i in _marker_issues(source, filename, is_test) if i.line not in suppressed)
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
