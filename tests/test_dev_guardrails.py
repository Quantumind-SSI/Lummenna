"""Tests for the offline code guardrail scanner (lummenna.dev.guardrails).

The guardrail is the deterministic core of the "ground all LLM-generated code"
discipline: no network, no LLM, reproducible in CI. These tests pin its
detection surface and its false-positive suppressions.
"""

from __future__ import annotations

import pytest

from lummenna.dev.guardrails import (
    GuardrailIssue,
    GuardrailReport,
    Severity,
    scan_paths,
    scan_source,
)


def _rules(report: GuardrailReport) -> set[str]:
    return {i.rule for i in report.issues}


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_detects_dynamic_exec_blocker():
    r = scan_source("eval(user_input)\n", "x.py")
    assert not r.passed
    assert "no-dynamic-exec" in _rules(r)
    assert any(i.severity is Severity.BLOCKER for i in r.issues)


def test_detects_os_system():
    r = scan_source("import os\nos.system(cmd)\n", "x.py")
    assert "no-os-system" in _rules(r)


def test_detects_shell_true():
    r = scan_source("import subprocess\nsubprocess.run(cmd, shell=True)\n", "x.py")
    assert "no-shell-true" in _rules(r)


def test_detects_hardcoded_secret():
    # A deliberate fixture for the detector, not a real secret. Built at runtime
    # from parts so it is neither a provider-shaped literal (avoids upstream
    # secret-scanners) nor flagged by our own guardrail on this test file.
    fake = "API_KEY = " + '"' + ("A1b2C3d4" * 3) + '"'
    r = scan_source(fake + "\n", "x.py")
    assert "hardcoded-api_key" in _rules(r)


def test_detects_sql_string_interpolation():
    r = scan_source('db.execute(f"SELECT * FROM users WHERE id={uid}")\n', "x.py")
    assert "no-sql-string-format" in _rules(r)


def test_detects_sql_percent_concat():
    r = scan_source('db.execute("SELECT * FROM t WHERE id = %s" % uid)\n', "x.py")
    assert "no-sql-string-format" in _rules(r)


def test_detects_bare_except():
    r = scan_source("try:\n    f()\nexcept:\n    pass\n", "x.py")
    assert "no-bare-except" in _rules(r)


def test_detects_unsafe_yaml_load():
    r = scan_source("import yaml\nyaml.load(data)\n", "x.py")
    assert "no-yaml-unsafe-load" in _rules(r)


def test_syntax_error_is_blocker():
    r = scan_source("def broken(:\n", "x.py")
    assert not r.passed
    assert "syntax-error" in _rules(r)


# --------------------------------------------------------------------------- #
# False-positive suppression
# --------------------------------------------------------------------------- #


def test_re_compile_is_not_flagged():
    r = scan_source("import re\nrx = re.compile(r'[a-z]+')\n", "x.py")
    assert "no-dynamic-exec" not in _rules(r)


def test_safe_yaml_load_not_flagged():
    r = scan_source("import yaml\nyaml.load(data, Loader=yaml.SafeLoader)\n", "x.py")
    assert "no-yaml-unsafe-load" not in _rules(r)


def test_placeholder_sql_is_not_flagged():
    src = (
        "placeholders = '?,' * (n - 1) + '?'\n"
        'db.execute(f"SELECT id FROM chunk WHERE id IN ({placeholders})", ids)\n'
    )
    r = scan_source(src, "x.py")
    assert "no-sql-string-format" not in _rules(r)


def test_nosec_marker_suppresses():
    src = 'db.execute(f"SELECT * FROM t WHERE id={uid}")  # nosec B608\n'
    r = scan_source(src, "x.py")
    assert "no-sql-string-format" not in _rules(r)


def test_placeholder_secret_allowlist():
    r = scan_source('API_KEY = "your-api-key-here-placeholder"\n', "x.py")
    assert "hardcoded-api_key" not in _rules(r)


def test_assert_allowed_in_test_files():
    src = "def test_x():\n    assert 1 == 1\n"
    r = scan_source(src, "test_thing.py")
    assert "no-assert-validation" not in _rules(r)


def test_clean_source_passes():
    src = "def add(a: int, b: int) -> int:\n    return a + b\n"
    r = scan_source(src, "clean.py")
    assert r.passed
    assert r.issues == []


# --------------------------------------------------------------------------- #
# Report model behavior
# --------------------------------------------------------------------------- #


def test_report_fail_threshold_respected():
    # A lone MEDIUM passes at default HIGH threshold ...
    r = scan_source("try:\n    f()\nexcept:\n    pass\n", "x.py")
    assert r.passed
    # ... but fails when the threshold is lowered to MEDIUM.
    r2 = scan_source("try:\n    f()\nexcept:\n    pass\n", "x.py", fail_at=Severity.MEDIUM)
    assert not r2.passed


def test_issue_model_is_frozen_and_strict():
    from pydantic import ValidationError

    issue = GuardrailIssue(rule="r", severity=Severity.HIGH, message="m")
    with pytest.raises(ValidationError):
        issue.line = 5  # frozen
    with pytest.raises(ValidationError):
        GuardrailIssue(rule="r", severity=Severity.HIGH, message="m", bogus=1)  # extra forbid


def test_report_json_roundtrips():
    r = scan_source("eval(x)\n", "x.py")
    dumped = r.model_dump_json()
    restored = GuardrailReport.model_validate_json(dumped)
    assert restored.issues[0].rule == "no-dynamic-exec"
    assert not restored.passed


def test_scan_paths_skips_non_python(tmp_path):
    (tmp_path / "a.py").write_text("eval(x)\n")
    (tmp_path / "b.txt").write_text("eval(x)\n")
    report = scan_paths([tmp_path / "a.py", tmp_path / "b.txt"])
    assert report.files_scanned == 1
    assert not report.passed


# --------------------------------------------------------------------------- #
# Anti-vaporware: stub bodies
# --------------------------------------------------------------------------- #


def test_stub_pass_body_flagged():
    r = scan_source("def foo():\n    pass\n", "x.py")
    assert "no-vaporware-stub" in _rules(r)
    assert not r.passed


def test_stub_ellipsis_body_flagged():
    r = scan_source("def foo():\n    ...\n", "x.py")
    assert "no-vaporware-stub" in _rules(r)


def test_stub_not_implemented_error_flagged():
    r = scan_source("def foo():\n    raise NotImplementedError\n", "x.py")
    assert "no-vaporware-stub" in _rules(r)


def test_stub_not_implemented_error_with_message_flagged():
    r = scan_source("def foo():\n    raise NotImplementedError('later')\n", "x.py")
    assert "no-vaporware-stub" in _rules(r)


def test_documented_stub_still_flagged():
    # A docstring does not make an unimplemented function real.
    r = scan_source('def foo():\n    """Does a thing."""\n    pass\n', "x.py")
    assert "no-vaporware-stub" in _rules(r)


def test_async_stub_flagged():
    r = scan_source("async def foo():\n    ...\n", "x.py")
    assert "no-vaporware-stub" in _rules(r)


# --------------------------------------------------------------------------- #
# Anti-vaporware: legitimate abstractions are exempt
# --------------------------------------------------------------------------- #


def test_abstractmethod_not_flagged():
    src = "from abc import abstractmethod\nclass A:\n    @abstractmethod\n    def foo(self): ...\n"
    r = scan_source(src, "x.py")
    assert "no-vaporware-stub" not in _rules(r)


def test_abc_subclass_method_not_flagged():
    src = "from abc import ABC\nclass A(ABC):\n    def foo(self): ...\n"
    r = scan_source(src, "x.py")
    assert "no-vaporware-stub" not in _rules(r)


def test_protocol_method_not_flagged():
    src = "from typing import Protocol\nclass A(Protocol):\n    def foo(self) -> int: ...\n"
    r = scan_source(src, "x.py")
    assert "no-vaporware-stub" not in _rules(r)


def test_abcmeta_metaclass_not_flagged():
    src = "from abc import ABCMeta\nclass A(metaclass=ABCMeta):\n    def foo(self): ...\n"
    r = scan_source(src, "x.py")
    assert "no-vaporware-stub" not in _rules(r)


def test_stub_noqa_suppresses():
    r = scan_source("def foo():\n    pass  # noqa: guardrail\n", "x.py")
    assert "no-vaporware-stub" not in _rules(r)


def test_stubs_exempt_in_test_files():
    r = scan_source("def foo():\n    pass\n", "test_something.py")
    assert "no-vaporware-stub" not in _rules(r)


# --------------------------------------------------------------------------- #
# Anti-vaporware / no-false-claims: text markers
# --------------------------------------------------------------------------- #


def test_todo_marker_flagged():
    r = scan_source("def foo():\n    return 1  # TODO: fix later\n", "x.py")
    assert "no-vaporware-marker" in _rules(r)


def test_fixme_marker_flagged():
    r = scan_source("def foo():\n    return 1  # FIXME broken\n", "x.py")
    assert "no-vaporware-marker" in _rules(r)


def test_false_claim_marker_flagged():
    src = "def foo():\n    # in a real implementation we would call the API\n    return {}\n"
    r = scan_source(src, "x.py")
    assert "no-vaporware-marker" in _rules(r)


def test_left_as_exercise_marker_flagged():
    r = scan_source('def foo():\n    """Rest left as an exercise."""\n    return 1\n', "x.py")
    assert "no-vaporware-marker" in _rules(r)


def test_markers_exempt_in_test_files():
    r = scan_source("def foo():\n    return 1  # TODO: fix\n", "test_something.py")
    assert "no-vaporware-marker" not in _rules(r)


def test_marker_only_matches_prose_not_identifiers():
    # 'todo_items' is an identifier, not a marker; must not be flagged.
    r = scan_source("todo_items = []\ndef q():\n    return todo_items\n", "x.py")
    assert "no-vaporware-marker" not in _rules(r)


def test_sql_placeholder_comment_not_flagged():
    # 'placeholders' in the SQL bind-parameter sense is legitimate, not vaporware.
    src = "# placeholders is a generated '?,?,...' string\ndef q():\n    return 1\n"
    r = scan_source(src, "x.py")
    assert "no-vaporware-marker" not in _rules(r)


def test_marker_noqa_suppresses():
    r = scan_source("def foo():\n    return 1  # TODO fix  # noqa: guardrail\n", "x.py")
    assert "no-vaporware-marker" not in _rules(r)


def test_lummenna_package_passes_guardrails():
    """The shipped library must itself pass the HIGH-severity gate."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "lummenna"
    py_files = sorted(root.rglob("*.py"))
    report = scan_paths(py_files)
    blocking = report.blocking()
    assert not blocking, "lummenna/ has blocking guardrail issues:\n" + "\n".join(
        b.render() for b in blocking
    )
