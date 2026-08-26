"""Tests for scripts/pro_features_report.py — the AST Pro-features report.

The tool statically parses a repo (never importing it) and cross-references the
``PRO_FEATURES`` / ``PLANNED_PRO_FEATURES`` registries against every
``assert_licensed`` / ``is_licensed`` call site. These tests build tiny
synthetic repos so the detector is proven to *catch* the three violation classes
it exists to catch (an honest pass is necessary but not sufficient).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pro_features_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("pro_features_report", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclass/field resolution looks the module up in
    # sys.modules by name while the module body is executing.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _make_repo(tmp_path: Path, licensing_src: str, gate_files: dict[str, str]) -> Path:
    """Materialise a minimal repo: lummenna/licensing.py plus gate-site modules."""
    pkg = tmp_path / "lummenna"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "licensing.py").write_text(licensing_src)
    for rel, src in gate_files.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src)
    return tmp_path


_HONEST_LICENSING = """
PRO_FEATURES: dict[str, str] = {
    "compliance": "Compliance (P1)",
    "priority_support": "Priority support (V5)",
    "lts": "LTS (V8)",
}
PLANNED_PRO_FEATURES: dict[str, str] = {
    "scale_out": "Scale-out (P5)",
}
"""


class TestExtraction:
    def test_registries_extracted(self, tmp_path):
        root = _make_repo(tmp_path, _HONEST_LICENSING, {})
        report = mod.build_report(root)
        assert set(report.pro_features) == {"compliance", "priority_support", "lts"}
        assert set(report.planned_features) == {"scale_out"}

    def test_literal_gate_site_captured(self, tmp_path):
        root = _make_repo(
            tmp_path,
            _HONEST_LICENSING,
            {"a.py": 'def f(c):\n    c.assert_licensed("compliance")\n'},
        )
        report = mod.build_report(root)
        keys = {s.key for s in report.assert_sites}
        assert "compliance" in keys

    def test_variable_gate_site_ignored(self, tmp_path):
        # A non-literal first arg (a variable) is unresolvable statically and
        # must not be captured as a key — mirrors cli/main.py's wrapper.
        root = _make_repo(
            tmp_path,
            _HONEST_LICENSING,
            {"a.py": "def f(c, feature):\n    c.assert_licensed(feature)\n"},
        )
        report = mod.build_report(root)
        assert report.assert_sites == []


class TestVerdict:
    def test_honest_repo_passes(self, tmp_path):
        root = _make_repo(
            tmp_path,
            _HONEST_LICENSING,
            {"a.py": 'def f(c):\n    c.assert_licensed("compliance")\n'},
        )
        out = mod.render(mod.build_report(root))
        assert "RESULT: PASS" in out
        # compliance is gated; priority_support/lts are non-code entitlements.
        # The leak marker (not the section header "must be UNGATED") must be absent.
        assert "revenue leak" not in out
        assert "VIOLATION" not in out

    def test_ungated_pro_feature_flagged(self, tmp_path):
        # compliance is a real (non-entitlement) PRO feature with NO gate site.
        root = _make_repo(tmp_path, _HONEST_LICENSING, {})
        out = mod.render(mod.build_report(root))
        assert "revenue leak" in out
        assert "RESULT: REVIEW" in out

    def test_planned_feature_gated_is_violation(self, tmp_path):
        # Gating a PLANNED feature is a forbidden false-availability claim.
        root = _make_repo(
            tmp_path,
            _HONEST_LICENSING,
            {
                "a.py": (
                    "def f(c):\n"
                    '    c.assert_licensed("compliance")\n'
                    '    c.assert_licensed("scale_out")\n'
                )
            },
        )
        out = mod.render(mod.build_report(root))
        assert "VIOLATION: planned feature is gated" in out
        assert "RESULT: REVIEW" in out

    def test_unknown_key_gate_flagged(self, tmp_path):
        root = _make_repo(
            tmp_path,
            _HONEST_LICENSING,
            {
                "a.py": (
                    "def f(c):\n"
                    '    c.assert_licensed("compliance")\n'
                    '    c.assert_licensed("typo_feature")\n'
                )
            },
        )
        out = mod.render(mod.build_report(root))
        assert "UNKNOWN KEY 'typo_feature'" in out
        assert "RESULT: REVIEW" in out


class TestRealRepo:
    def test_lummenna_repo_is_honest(self):
        """The actual Lummenna repo must pass: every PRO feature gated, no leaks."""
        root = Path(__file__).resolve().parents[1]
        if not (root / "lummenna" / "licensing.py").exists():
            pytest.skip("licensing module not present")
        report = mod.build_report(root)
        out = mod.render(report)
        assert "RESULT: PASS" in out
        assert set(report.pro_features)  # non-empty
