#!/usr/bin/env python3
"""AST current-state report for Lumena Pro features.

Statically parses (never imports/executes) the Lumena package to extract:

  * the ``PRO_FEATURES`` registry (built + enforceable),
  * the ``PLANNED_PRO_FEATURES`` registry (design-only),
  * every ``assert_licensed(<key>)`` call site with its literal key argument,
  * every ``is_licensed(<key>)`` call site,

then cross-references them to answer, per AGENTS.md Rule 2:

  1. Which PRO_FEATURES have a real gate call site (enforced) vs none (revenue
     leak / false claim)?
  2. Are any PLANNED_PRO_FEATURES gated (a forbidden false-availability claim)?
  3. Are any gate call sites referencing an unknown key (a typo / dead gate)?

The analysed code is never imported, so this runs with optional extras absent
and triggers no module-level side effects.

Usage:
    python3 scripts/pro_features_report.py [REPO_ROOT]

REPO_ROOT defaults to the current working directory, so from the repo root a
bare ``python3 scripts/pro_features_report.py`` analyses this checkout.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Non-code entitlements: granted purely by license flag, legitimately without a
# code gate (they are support/release commitments, not a code path). Sourced
# from the PRO_FEATURES comments in lumena/licensing.py.
_NON_CODE_ENTITLEMENTS: frozenset[str] = frozenset({"priority_support", "lts"})

_LICENSING_REL = Path("lumena") / "licensing.py"
_GATE_FUNCS: frozenset[str] = frozenset({"assert_licensed", "is_licensed"})


@dataclass
class CallSite:
    key: str
    file: str
    line: int


@dataclass
class Report:
    pro_features: dict[str, str] = field(default_factory=dict)
    planned_features: dict[str, str] = field(default_factory=dict)
    assert_sites: list[CallSite] = field(default_factory=list)
    is_licensed_sites: list[CallSite] = field(default_factory=list)


def _iter_python_files(root: Path) -> list[Path]:
    """Every tracked .py file under lumena/ (skip tests, caches, venvs)."""
    files: list[Path] = []
    for path in sorted((root / "lumena").rglob("*.py")):
        parts = set(path.parts)
        if parts & {".venv", "__pycache__", ".mypy_cache", ".ruff_cache"}:
            continue
        files.append(path)
    return files


def _string_dict_from_assign(node: ast.AST) -> dict[str, str] | None:
    """Extract a {str: str} literal from an annotated/plain assignment target."""
    value: ast.AST | None = None
    if (isinstance(node, ast.AnnAssign) and node.value is not None) or isinstance(node, ast.Assign):
        value = node.value
    if not isinstance(value, ast.Dict):
        return None
    result: dict[str, str] = {}
    for k, v in zip(value.keys, value.values, strict=False):
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            desc = v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else ""
            result[k.value] = str(desc)
    return result


def _assigned_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    return []


def _extract_registries(licensing_src: str, report: Report) -> None:
    tree = ast.parse(licensing_src)
    for node in ast.walk(tree):
        names = _assigned_names(node)
        if "PRO_FEATURES" in names:
            d = _string_dict_from_assign(node)
            if d is not None:
                report.pro_features = d
        elif "PLANNED_PRO_FEATURES" in names:
            d = _string_dict_from_assign(node)
            if d is not None:
                report.planned_features = d


def _call_func_name(call: ast.Call) -> str | None:
    """Resolve the simple name of a call target (``f()`` or ``obj.f()``)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _first_str_arg(call: ast.Call) -> str | None:
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        # Non-literal first positional (e.g. a variable): unresolvable statically.
        return None
    return None


def _extract_call_sites(path: Path, root: Path, report: Report) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return
    rel = str(path.relative_to(root))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_func_name(node)
        if name not in _GATE_FUNCS:
            continue
        key = _first_str_arg(node)
        if key is None:
            continue
        site = CallSite(key=key, file=rel, line=node.lineno)
        if name == "assert_licensed":
            report.assert_sites.append(site)
        else:
            report.is_licensed_sites.append(site)


def build_report(root: Path) -> Report:
    report = Report()
    licensing = root / _LICENSING_REL
    if not licensing.exists():
        raise SystemExit(f"licensing module not found: {licensing}")
    _extract_registries(licensing.read_text(encoding="utf-8"), report)
    for path in _iter_python_files(root):
        # Skip the licensing module's own definition/guard sites: assert_licensed
        # is *defined* there and references PRO_FEATURES/PLANNED for validation,
        # not as a real feature gate.
        if path == licensing:
            continue
        _extract_call_sites(path, root, report)
    return report


def _sites_by_key(sites: list[CallSite]) -> dict[str, list[CallSite]]:
    out: dict[str, list[CallSite]] = {}
    for s in sites:
        out.setdefault(s.key, []).append(s)
    return out


@dataclass
class _Findings:
    """The three violation classes, collected while rendering."""

    leaks: list[str] = field(default_factory=list)
    wrongly_gated: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.leaks and not self.wrongly_gated and not self.unknown


def _render_header(report: Report) -> list[str]:
    return [
        "=" * 72,
        "LUMENA PRO FEATURES — AST CURRENT-STATE REPORT",
        "(static parse; source never imported/executed)",
        "=" * 72,
        "",
        f"PRO_FEATURES (built + enforceable): {len(report.pro_features)}",
        f"PLANNED_PRO_FEATURES (design-only): {len(report.planned_features)}",
        f"assert_licensed() call sites: {len(report.assert_sites)}",
        f"is_licensed() call sites: {len(report.is_licensed_sites)}",
        "",
    ]


def _render_pro_status(
    report: Report,
    assert_by_key: dict[str, list[CallSite]],
    islic_by_key: dict[str, list[CallSite]],
    findings: _Findings,
) -> list[str]:
    out = ["-" * 72, "1. PRO_FEATURES — enforcement status", "-" * 72]
    for key, desc in report.pro_features.items():
        asserts = assert_by_key.get(key, [])
        checks = islic_by_key.get(key, [])
        if asserts:
            status = f"GATED ({len(asserts)} assert_licensed)"
        elif key in _NON_CODE_ENTITLEMENTS:
            status = "NON-CODE ENTITLEMENT (flag-only, no gate required)"
        else:
            status = "UNGATED  <-- revenue leak / false claim"
            findings.leaks.append(key)
        out.append(f"  [{status}]")
        out.append(f"    key   : {key}")
        out.append(f"    desc  : {desc}")
        out.extend(f"    gate  : {s.file}:{s.line}" for s in asserts)
        out.extend(f"    check : is_licensed @ {s.file}:{s.line}" for s in checks)
        out.append("")
    return out


def _render_planned(
    report: Report,
    assert_by_key: dict[str, list[CallSite]],
    islic_by_key: dict[str, list[CallSite]],
    findings: _Findings,
) -> list[str]:
    out = ["-" * 72, "2. PLANNED_PRO_FEATURES — must be UNGATED (Rule 3)", "-" * 72]
    for key, desc in report.planned_features.items():
        sites = assert_by_key.get(key, []) + islic_by_key.get(key, [])
        if sites:
            findings.wrongly_gated.append(key)
            out.append(f"  [VIOLATION: planned feature is gated]  {key}")
            out.extend(f"    at {s.file}:{s.line}" for s in sites)
        else:
            out.append(f"  [OK ungated]  {key}  — {desc}")
    out.append("")
    return out


def _render_unknown(
    report: Report,
    assert_by_key: dict[str, list[CallSite]],
    islic_by_key: dict[str, list[CallSite]],
    gated_keys: set[str],
    findings: _Findings,
) -> list[str]:
    out = [
        "-" * 72,
        "3. Gate call sites referencing UNKNOWN keys (typos / dead gates)",
        "-" * 72,
    ]
    known = set(report.pro_features) | set(report.planned_features) | {"*"}
    findings.unknown = sorted(k for k in gated_keys if k not in known)
    if findings.unknown:
        for key in findings.unknown:
            for s in assert_by_key.get(key, []) + islic_by_key.get(key, []):
                out.append(f"  [UNKNOWN KEY '{key}'] {s.file}:{s.line}")
    else:
        out.append("  none — every gate references a known registry key.")
    out.append("")
    return out


def _render_call_sites(assert_by_key: dict[str, list[CallSite]]) -> list[str]:
    out = ["-" * 72, "4. All assert_licensed() call sites (grouped by key)", "-" * 72]
    for key in sorted(assert_by_key):
        out.append(f"  {key}:")
        out.extend(f"    {s.file}:{s.line}" for s in assert_by_key[key])
    out.append("")
    return out


def _render_verdict(findings: _Findings) -> list[str]:
    verdict = "PASS — registries honest & enforced" if findings.ok else "REVIEW"
    return [
        "=" * 72,
        "VERDICT",
        "=" * 72,
        f"  Ungated PRO_FEATURES (excl. non-code): {findings.leaks or 'none'}",
        f"  Planned features wrongly gated       : {findings.wrongly_gated or 'none'}",
        f"  Unknown-key gate sites               : {findings.unknown or 'none'}",
        f"  RESULT: {verdict}",
    ]


def render(report: Report) -> str:
    """Render the full report as text, adjudicating the three violation classes."""
    assert_by_key = _sites_by_key(report.assert_sites)
    islic_by_key = _sites_by_key(report.is_licensed_sites)
    gated_keys = set(assert_by_key) | set(islic_by_key)
    findings = _Findings()

    lines: list[str] = []
    lines += _render_header(report)
    lines += _render_pro_status(report, assert_by_key, islic_by_key, findings)
    lines += _render_planned(report, assert_by_key, islic_by_key, findings)
    lines += _render_unknown(report, assert_by_key, islic_by_key, gated_keys, findings)
    lines += _render_call_sites(assert_by_key)
    lines += _render_verdict(findings)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    report = build_report(root)
    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
