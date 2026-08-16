# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-QuantumindSSI-Commercial
# Copyright (c) 2025-2026 QuantumindSSI. All rights reserved.
"""CLI for the Lumena code guardrail gate.

Usage::

    # scan explicit files
    python -m lumena.dev PATH [PATH ...]

    # scan files listed on stdin (e.g. from `git diff --name-only`)
    git diff --name-only origin/main... | python -m lumena.dev --stdin

    # emit JSON instead of text
    python -m lumena.dev --stdin --json

Exit code is non-zero when any issue meets/exceeds the fail threshold
(default HIGH), so it can gate CI.
"""

from __future__ import annotations

import argparse
import sys

from lumena.dev.guardrails import DEFAULT_FAIL_AT, Severity, scan_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumena.dev", description="Lumena code guardrail gate.")
    parser.add_argument("paths", nargs="*", help="Python files to scan.")
    parser.add_argument(
        "--stdin", action="store_true", help="Read newline-separated paths from stdin."
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    parser.add_argument(
        "--fail-at",
        choices=[s.label for s in Severity],
        default=DEFAULT_FAIL_AT.label,
        help="Minimum severity that fails the gate (default: HIGH).",
    )
    args = parser.parse_args(argv)

    paths: list[str] = list(args.paths)
    if args.stdin:
        paths.extend(line.strip() for line in sys.stdin if line.strip())

    fail_at = Severity[args.fail_at]
    report = scan_paths(paths, fail_at=fail_at)

    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(report.render())

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
