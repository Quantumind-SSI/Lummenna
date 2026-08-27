# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-QuantumindSSI-Commercial
# Copyright (c) 2025-2026 QuantumindSSI. All rights reserved.
"""Gumroad sales fulfillment: batch-mint Lummenna Pro license keys.

Reads a Gumroad **sales CSV export**, mints one signed ``LUMMENNA-...`` key per
un-refunded, not-yet-fulfilled sale, and writes:

1. a **fulfillment CSV** (``--out``) with one row per newly issued license —
   ready for a mail-merge that sends each buyer their key; and
2. a **license register** (``--register``, append-only) — the authoritative
   custody record (license id, subject, tier, seats, expiry) required by
   ``docs/LICENSE_SIGNING_KEY_CUSTODY.md`` §5. On refund/chargeback, the
   register row's ``license_id`` is what goes into ``REVOKED_LICENSE_IDS``.

The run is **atomic per invocation**: every data row is validated first, and if
any row is invalid (unknown tier, missing email/sale id, unparseable date) the
tool exits with code 2 and mints nothing. Refunded/chargedback rows and sale
ids already present in the register are *skipped*, not errors, so re-running
on a cumulative Gumroad export is idempotent.

Private-key sources (checked in this order):

* ``--private-key-file`` — raw-32 base64url Ed25519 key file;
* ``--keychain`` — macOS login Keychain item (default service
  ``quantumindssi-license-signing-key``), the recommended custody location;
* ``LUMMENNA_SIGNING_PRIVATE_KEY_B64`` environment variable.

Example::

    python scripts/gumroad_fulfillment.py \\
        --sales-csv ~/Downloads/Sales_CSV.csv \\
        --keychain \\
        --register ~/LummennaSigningVault/license-register.csv \\
        --out ~/LummennaSigningVault/fulfillment-20260820.csv

License keys are written only to the output files, never to the console.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent

#: Default tier map for the Gumroad listing (variant name -> seats). Seats are
#: enforced by lummenna.licensing as max concurrent non-default tenants.
DEFAULT_TIERS: dict[str, int] = {"solo": 3, "team": 10, "business": 25}

#: Default macOS Keychain service holding the signing key (see the custody
#: runbook and ~/LummennaSigningVault/README.md).
DEFAULT_KEYCHAIN_SERVICE = "quantumindssi-license-signing-key"

#: Candidate Gumroad CSV headers per logical column, normalised via
#: :func:`_norm_header`. Gumroad has renamed columns over time; explicit
#: ``--col-*`` overrides always win.
_HEADER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "email": ("purchaseemail", "email", "buyeremail", "purchaseremail"),
    "name": ("buyername", "fullname", "purchasername", "name"),
    "sale_id": ("purchaseid", "saleid", "ordernumber", "orderid", "transactionid"),
    "product": ("itemname", "productname", "product"),
    "variant": ("variants", "variant", "version", "tier"),
    "date": ("purchasedate", "createdat", "saletimestamp", "timestamp", "date"),
    "refunded": ("refunded", "fullyrefunded", "refundissued"),
    "chargedback": ("chargedback", "chargeback", "disputed"),
    "status": ("status", "paymentstatus", "state"),
}

_REQUIRED_COLUMNS = ("email", "sale_id")

_TRUTHY = frozenset({"1", "true", "yes", "y"})

#: Values a boolean void-marker column (refunded / charged back) may take to
#: mean "no". Anything outside _TRUTHY | _FALSY is a configuration mistake and
#: aborts the run: see :func:`_void_markers` for why silence is not an option.
_FALSY = frozenset({"", "0", "false", "no", "n", "f"})

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)

_REGISTER_FIELDS = (
    "license_id",
    "sale_id",
    "email",
    "buyer_name",
    "subject",
    "tier",
    "seats",
    "features",
    "expires",
    "issued_at",
    "product",
    "notes",
)

_OUT_FIELDS = (
    "email",
    "buyer_name",
    "tier",
    "seats",
    "expires",
    "license_key",
    "license_id",
    "sale_id",
    "product",
)


def _load_issue_module() -> ModuleType:
    """Load scripts/issue_license.py (the offline signer) as a module."""
    path = _SCRIPTS_DIR / "issue_license.py"
    spec = importlib.util.spec_from_file_location("issue_license", path)
    if spec is None or spec.loader is None:
        sys.exit(f"error: cannot load signer module at {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _licensing() -> ModuleType:
    """Import lummenna.licensing with the repo root on sys.path."""
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from lummenna import licensing

    return licensing


def _norm_header(header: str) -> str:
    """Normalise a CSV header for tolerant matching: 'Purchase Email ' -> 'purchaseemail'."""
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _resolve_columns(
    headers: list[str], overrides: dict[str, str | None]
) -> tuple[dict[str, str | None], list[str]]:
    """Map logical column names to actual CSV headers.

    Explicit overrides are validated against the header row; otherwise the
    first candidate (by normalised name) wins. Returns (mapping, errors);
    missing *required* columns are errors, missing optional ones map to None.
    """
    normalised = {_norm_header(h): h for h in headers}
    resolved: dict[str, str | None] = {}
    errors: list[str] = []
    for logical, candidates in _HEADER_CANDIDATES.items():
        override = overrides.get(logical)
        if override is not None:
            if override in headers:
                resolved[logical] = override
            else:
                errors.append(f"--col-{logical.replace('_', '-')} {override!r} not in CSV headers")
                resolved[logical] = None
            continue
        resolved[logical] = next(
            (normalised[c] for c in candidates if c in normalised),
            None,
        )
    for logical in _REQUIRED_COLUMNS:
        if resolved.get(logical) is None and not any(
            e.startswith(f"--col-{logical.replace('_', '-')}") for e in errors
        ):
            errors.append(
                f"could not find a {logical!r} column (headers: {headers}); "
                f"use --col-{logical.replace('_', '-')} to name it explicitly"
            )
    return resolved, errors


def _truthy(cell: str | None) -> bool:
    return (cell or "").strip().lower() in _TRUTHY


def _parse_purchase_date(raw: str) -> date | None:
    """Parse a Gumroad purchase timestamp; returns None if unrecognised."""
    text = raw.strip()
    if not text:
        return None
    # strptime's %Z is unreliable across platforms; trailing zone names carry
    # no information we need for a date, so strip them explicitly.
    text = re.sub(r"\s+(UTC|GMT)$", "", text, flags=re.IGNORECASE)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _match_tier(variant_cell: str, tiers: dict[str, int]) -> str | None:
    """Match a Gumroad variant cell (e.g. '(Tier: Team)') to a tier name."""
    hay = variant_cell.lower()
    matches = [name for name in tiers if name.lower() in hay]
    if len(matches) == 1:
        return matches[0]
    return None


@dataclass(frozen=True)
class PlannedIssue:
    """One validated, not-yet-minted license issuance."""

    sale_id: str
    email: str
    buyer_name: str
    tier: str
    seats: int
    expires: str
    product: str

    @property
    def subject(self) -> str:
        return f"{self.buyer_name} <{self.email}>" if self.buyer_name else self.email


@dataclass(frozen=True)
class IssuedLicense:
    """A minted license bound to its sale."""

    planned: PlannedIssue
    license_key: str
    license_id: str
    issued_at: str


def _read_keychain_secret(service: str) -> str:
    """Read a generic password from the macOS login Keychain (never logged)."""
    if sys.platform != "darwin":
        sys.exit("error: --keychain is only available on macOS; use --private-key-file instead.")
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.exit(
            f"error: Keychain item {service!r} not found or not readable "
            f"(security exit {proc.returncode}). See ~/LummennaSigningVault/README.md."
        )
    return proc.stdout.strip()


def _load_private_key_bytes(args: argparse.Namespace, signer: ModuleType) -> bytes:
    """Resolve the Ed25519 private key from file, Keychain, or environment."""
    raw_b64: str | None = None
    if args.private_key_file:
        raw_b64 = Path(args.private_key_file).read_text(encoding="utf-8").strip()
    elif args.keychain:
        raw_b64 = _read_keychain_secret(args.keychain_service)
    elif os.environ.get("LUMMENNA_SIGNING_PRIVATE_KEY_B64"):
        raw_b64 = os.environ["LUMMENNA_SIGNING_PRIVATE_KEY_B64"].strip()
    if not raw_b64:
        sys.exit(
            "error: no signing key. Use --private-key-file, --keychain, or set "
            "LUMMENNA_SIGNING_PRIVATE_KEY_B64."
        )
    try:
        key_bytes: bytes = signer._b64url_decode(raw_b64)
    except Exception as exc:  # noqa: BLE001 - decode failure must abort with a clear message
        sys.exit(f"error: could not base64url-decode the private key: {exc}")
    if len(key_bytes) != 32:
        sys.exit(f"error: expected a raw 32-byte Ed25519 private key, got {len(key_bytes)} bytes.")
    return key_bytes


def _load_register_ids(path: Path) -> set[str]:
    """Sale ids already fulfilled (from the append-only register)."""
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "sale_id" not in reader.fieldnames:
            sys.exit(
                f"error: register {path} exists but has no 'sale_id' column; "
                f"refusing to guess (headers: {reader.fieldnames})."
            )
        return {row["sale_id"] for row in reader if row.get("sale_id")}


def _read_sales_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read the Gumroad sales CSV into (headers, rows)."""
    if not path.exists():
        sys.exit(f"error: sales CSV not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            sys.exit(f"error: {path} has no header row.")
        return list(reader.fieldnames), list(reader)


def _cell(row: dict[str, str], col: str | None) -> str:
    return (row.get(col) or "").strip() if col else ""


def _void_markers(
    row: dict[str, str],
    rownum: int,
    cols: dict[str, str | None],
    paid_status: frozenset[str],
) -> tuple[str | None, str | None]:
    """Decide whether a sale is void: refunded, disputed, or never actually paid.

    Two column shapes exist in the wild. Gumroad emits boolean markers
    (``refunded`` is true/false); Stripe emits a status enum (``Status`` is
    Paid/Refunded/Failed/...). Booleans are read here. Enums go through
    ``--paid-status``, which is an allowlist, so an unfamiliar status excludes
    the row instead of admitting it.

    An unreadable boolean marker is an error rather than a False. These
    licenses verify offline against a public key compiled into the client, so a
    wrongly-minted key cannot be revoked without reissuing every license in
    circulation. Pointing ``--col-refunded`` at a status column used to read
    "Refunded" as not-refunded and mint anyway; that silence is now fatal.

    Args:
        row: One CSV row keyed by header.
        rownum: 1-based row number, for error messages.
        cols: Logical column name to actual header, from :func:`_resolve_columns`.
        paid_status: Lowercased allowlist. Empty disables the status check.

    Returns:
        ``(skip_reason, error)``. At most one is non-None; both None means the
        sale looks payable.
    """
    for marker in ("refunded", "chargedback"):
        column = cols[marker]
        if column is None:
            continue
        cell = _cell(row, column)
        text = cell.strip().lower()
        if text in _TRUTHY:
            return marker, None
        if text not in _FALSY:
            return None, (
                f"row {rownum}: column {column!r} holds {cell!r}, which is neither a "
                f"yes nor a no. If it is a status column, drop --col-{marker} and use "
                f"--col-status with --paid-status instead. An unreadable refund marker "
                f"stops the run because a minted license cannot be revoked."
            )
    if paid_status:
        status_column = cols["status"]
        if status_column is None:
            # main() rejects this combination up front; treating it as an error
            # here too keeps the check fail-closed if that guard is ever moved.
            return None, f"row {rownum}: --paid-status given but no status column resolved"
        text = _cell(row, status_column).strip().lower()
        if text not in paid_status:
            return f"status {text!r} not in --paid-status", None
    return None, None


def _plan_row(
    row: dict[str, str],
    rownum: int,
    cols: dict[str, str | None],
    tiers: dict[str, int],
    args: argparse.Namespace,
    today: date,
) -> tuple[PlannedIssue | None, str | None, str | None]:
    """Validate one CSV row. Returns (planned, skip_reason, error)."""
    skip_reason, void_error = _void_markers(row, rownum, cols, args.paid_status)
    if skip_reason is not None or void_error is not None:
        return None, skip_reason, void_error
    sale_id = _cell(row, cols["sale_id"])
    email = _cell(row, cols["email"])
    if not sale_id:
        return None, None, f"row {rownum}: empty sale id"
    if not email or "@" not in email:
        return None, None, f"row {rownum} (sale {sale_id}): missing/invalid email {email!r}"
    variant = _cell(row, cols["variant"])
    tier = _match_tier(variant, tiers) if variant else None
    if tier is None:
        if args.default_tier:
            tier = args.default_tier
        else:
            return (
                None,
                None,
                (
                    f"row {rownum} (sale {sale_id}): cannot map variant {variant!r} to a tier "
                    f"{sorted(tiers)}; fix the CSV or pass --default-tier"
                ),
            )
    anchor: date | None
    if args.anchor_today:
        anchor = today
    else:
        raw_date = _cell(row, cols["date"])
        anchor = _parse_purchase_date(raw_date)
        if anchor is None:
            return (
                None,
                None,
                (
                    f"row {rownum} (sale {sale_id}): unparseable purchase date {raw_date!r}; "
                    f"pass --anchor-today to date licenses from today instead"
                ),
            )
    expires = (anchor + timedelta(days=args.duration_days)).isoformat()
    return (
        PlannedIssue(
            sale_id=sale_id,
            email=email,
            buyer_name=_cell(row, cols["name"]),
            tier=tier,
            seats=tiers[tier],
            expires=expires,
            product=_cell(row, cols["product"]),
        ),
        None,
        None,
    )


def _plan_all(
    rows: list[dict[str, str]],
    cols: dict[str, str | None],
    tiers: dict[str, int],
    fulfilled: set[str],
    args: argparse.Namespace,
    today: date,
) -> tuple[list[PlannedIssue], list[str], list[str]]:
    """Validate every row; collect planned issues, skips, and errors."""
    planned: list[PlannedIssue] = []
    skips: list[str] = []
    errors: list[str] = []
    seen = set(fulfilled)
    for rownum, row in enumerate(rows, start=2):  # header is line 1
        issue, skip_reason, error = _plan_row(row, rownum, cols, tiers, args, today)
        if error:
            errors.append(error)
            continue
        if skip_reason:
            skips.append(f"row {rownum}: skipped ({skip_reason})")
            continue
        assert issue is not None  # _plan_row returns exactly one of the three
        if issue.sale_id in seen:
            skips.append(f"row {rownum}: skipped (sale {issue.sale_id} already fulfilled)")
            continue
        seen.add(issue.sale_id)
        planned.append(issue)
    return planned, skips, errors


def _mint(
    planned: list[PlannedIssue],
    private_key: bytes,
    features: str,
    signer: ModuleType,
) -> list[IssuedLicense]:
    """Sign one license per planned issue and self-verify each key."""
    licensing = _licensing()
    licensing.clear_license_cache()
    issued: list[IssuedLicense] = []
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for item in planned:
        ns = argparse.Namespace(
            subject=item.subject,
            features=features,
            seats=item.seats,
            expires=item.expires,
            license_id="",
        )
        payload = signer.build_payload(ns)
        key = signer.sign_license(payload, private_key)
        try:
            lic = licensing.parse_license_key(key)
        except licensing.LicenseInvalidError as exc:
            sys.exit(
                f"error: minted key for sale {item.sale_id} failed verification against "
                f"the embedded public key ({exc}); is the signing key the production one?"
            )
        if lic.seats != item.seats or str(lic.expires) != item.expires:
            sys.exit(
                f"error: verified license for sale {item.sale_id} does not match the plan "
                f"(seats {lic.seats} vs {item.seats}, expires {lic.expires} vs {item.expires})."
            )
        issued.append(
            IssuedLicense(
                planned=item,
                license_key=key,
                license_id=str(payload["id"]),
                issued_at=now_iso,
            )
        )
    return issued


def _write_out_csv(path: Path, issued: list[IssuedLicense]) -> None:
    """Write the mail-merge fulfillment CSV (created fresh each run)."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(_OUT_FIELDS))
        writer.writeheader()
        for lic in issued:
            writer.writerow(
                {
                    "email": lic.planned.email,
                    "buyer_name": lic.planned.buyer_name,
                    "tier": lic.planned.tier,
                    "seats": lic.planned.seats,
                    "expires": lic.planned.expires,
                    "license_key": lic.license_key,
                    "license_id": lic.license_id,
                    "sale_id": lic.planned.sale_id,
                    "product": lic.planned.product,
                }
            )


def _append_register(path: Path, issued: list[IssuedLicense], features: str) -> None:
    """Append issued licenses to the register atomically (write-temp + rename)."""
    existing: list[dict[str, str]] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            existing = list(csv.DictReader(fh))
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".register-", suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(_REGISTER_FIELDS), extrasaction="ignore")
            writer.writeheader()
            for old in existing:
                writer.writerow({field: old.get(field, "") for field in _REGISTER_FIELDS})
            for lic in issued:
                writer.writerow(
                    {
                        "license_id": lic.license_id,
                        "sale_id": lic.planned.sale_id,
                        "email": lic.planned.email,
                        "buyer_name": lic.planned.buyer_name,
                        "subject": lic.planned.subject,
                        "tier": lic.planned.tier,
                        "seats": lic.planned.seats,
                        "features": features,
                        "expires": lic.planned.expires,
                        "issued_at": lic.issued_at,
                        "product": lic.planned.product,
                        "notes": "",
                    }
                )
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def _parse_tier_specs(specs: list[str]) -> dict[str, int]:
    """Parse repeated --tier NAME=SEATS specs into a tier map."""
    tiers: dict[str, int] = {}
    for spec in specs:
        name, sep, seats_text = spec.partition("=")
        name = name.strip()
        if not sep or not name:
            sys.exit(f"error: --tier must be NAME=SEATS, got {spec!r}")
        try:
            seats = int(seats_text)
        except ValueError:
            sys.exit(f"error: --tier seats must be an integer, got {spec!r}")
        if seats < 0:
            sys.exit(f"error: --tier seats must be >= 0, got {spec!r}")
        tiers[name] = seats
    return tiers


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gumroad_fulfillment.py",
        description="Batch-mint Lummenna Pro licenses from a Gumroad sales CSV export.",
    )
    parser.add_argument("--sales-csv", required=True, help="Gumroad sales CSV export.")
    parser.add_argument("--out", required=True, help="Fulfillment CSV to write (mail-merge input).")
    parser.add_argument(
        "--register",
        required=True,
        help="Append-only license register CSV (custody record; created if missing).",
    )
    parser.add_argument(
        "--tier",
        action="append",
        default=[],
        metavar="NAME=SEATS",
        help="Tier definition (repeatable). Default: solo=3 team=10 business=25.",
    )
    parser.add_argument(
        "--default-tier",
        default=None,
        help="Tier to use when a row has no recognisable variant (e.g. single-tier products).",
    )
    parser.add_argument(
        "--features",
        default="*",
        help="Feature grant for every minted license (default '*': all Pro features).",
    )
    parser.add_argument(
        "--duration-days",
        type=int,
        default=365,
        help="License validity from the purchase date (default 365).",
    )
    parser.add_argument(
        "--anchor-today",
        action="store_true",
        help="Date licenses from today instead of each row's purchase date.",
    )
    parser.add_argument(
        "--today",
        default=None,
        metavar="YYYY-MM-DD",
        help="Override 'today' (deterministic runs / testing).",
    )
    parser.add_argument("--private-key-file", default=None, help="Raw-32 base64url Ed25519 key.")
    parser.add_argument(
        "--keychain",
        action="store_true",
        help=f"Read the key from the macOS Keychain (service {DEFAULT_KEYCHAIN_SERVICE!r}).",
    )
    parser.add_argument(
        "--keychain-service",
        default=DEFAULT_KEYCHAIN_SERVICE,
        help="Keychain service name to read with --keychain.",
    )
    parser.add_argument(
        "--paid-status",
        action="append",
        default=[],
        metavar="VALUE",
        help=(
            "Status value that counts as a completed sale (repeatable, "
            "case-insensitive). Use with --col-status for processors that report a "
            "status enum rather than a boolean refund flag, e.g. Stripe: "
            "--col-status Status --paid-status Paid. Rows with any other status are "
            "skipped, so unfamiliar statuses never mint."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report; mint nothing, write nothing.",
    )
    for logical in _HEADER_CANDIDATES:
        flag = f"--col-{logical.replace('_', '-')}"
        parser.add_argument(
            flag,
            dest=f"col_{logical}",
            default=None,
            metavar="HEADER",
            help=f"Exact CSV header to use for the {logical.replace('_', ' ')} column.",
        )
    return parser


def _resolve_today(args: argparse.Namespace) -> date:
    if args.today is None:
        return date.today()
    try:
        return datetime.strptime(args.today, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"error: --today must be YYYY-MM-DD, got {args.today!r}")


def main(argv: list[str] | None = None) -> None:
    """Entry point. See module docstring for the fulfillment contract."""
    args = _build_parser().parse_args(argv)
    if args.duration_days <= 0:
        sys.exit(f"error: --duration-days must be positive, got {args.duration_days}.")
    tiers = _parse_tier_specs(args.tier) if args.tier else dict(DEFAULT_TIERS)
    if args.default_tier is not None and args.default_tier not in tiers:
        sys.exit(f"error: --default-tier {args.default_tier!r} is not one of {sorted(tiers)}.")
    today = _resolve_today(args)

    headers, rows = _read_sales_rows(Path(args.sales_csv))
    overrides = {logical: getattr(args, f"col_{logical}") for logical in _HEADER_CANDIDATES}
    cols, column_errors = _resolve_columns(headers, overrides)
    if column_errors:
        sys.exit("error: sales CSV columns:\n  " + "\n  ".join(column_errors))
    args.paid_status = frozenset(value.strip().lower() for value in args.paid_status)
    if args.paid_status and cols["status"] is None:
        sys.exit(
            "error: --paid-status needs a status column; none was detected in "
            f"{headers}. Name it with --col-status."
        )

    fulfilled = _load_register_ids(Path(args.register))
    planned, skips, errors = _plan_all(rows, cols, tiers, fulfilled, args, today)
    for line in skips:
        print(line, file=sys.stderr)
    if errors:
        print("Aborting — no keys minted. Fix these rows first:", file=sys.stderr)
        for line in errors:
            print(f"  {line}", file=sys.stderr)
        raise SystemExit(2)
    if not planned:
        print("Nothing to fulfil: no new, un-refunded sales.", file=sys.stderr)
        return
    if args.dry_run:
        for item in planned:
            print(
                f"DRY RUN: would mint {item.tier} ({item.seats} seats, expires "
                f"{item.expires}) for {item.subject} [sale {item.sale_id}]",
                file=sys.stderr,
            )
        return

    signer = _load_issue_module()
    private_key = _load_private_key_bytes(args, signer)
    issued = _mint(planned, private_key, args.features, signer)
    _write_out_csv(Path(args.out), issued)
    _append_register(Path(args.register), issued, args.features)
    print(
        f"Issued {len(issued)} license(s) -> {args.out}; register updated at {args.register}.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
