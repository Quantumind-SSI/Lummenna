"""Tests for the Gumroad batch fulfillment tool (scripts/gumroad_fulfillment.py).

Covers: the CSV -> minted-keys -> register happy path across all three tiers,
refund/chargeback skipping, idempotent re-runs against the register, atomic
abort on invalid rows, Gumroad header/variant/date parsing, tier overrides,
and the macOS Keychain private-key source (mocked).
"""

from __future__ import annotations

import csv
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lummenna import licensing

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gumroad_fulfillment.py"
_ISSUE_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "issue_license.py"

GUMROAD_HEADERS = (
    "Purchase ID,Item Name,Buyer Name,Purchase Email,Purchase Date,Variants,Refunded?,Chargedback?"
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves string annotations (PEP 563)
    # through sys.modules[cls.__module__] at class-creation time.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load("gumroad_fulfillment", _SCRIPT)


@pytest.fixture()
def signing_key(tmp_path, monkeypatch):
    """Ephemeral Ed25519 keypair: embeds the pubkey, returns the privkey file."""
    issue_mod = _load("issue_license", _ISSUE_SCRIPT)
    priv_b64, pub_b64 = issue_mod.generate_keypair()
    monkeypatch.setattr(licensing, "_QUANTUMINDSSI_PUBLIC_KEY_B64", pub_b64)
    licensing.clear_license_cache()
    key_file = tmp_path / "signing.b64"
    key_file.write_text(priv_b64 + "\n")
    return key_file


def _write_sales_csv(path: Path, rows: list[str], headers: str = GUMROAD_HEADERS) -> Path:
    path.write_text(headers + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _run(mod, tmp_path: Path, sales: Path, key_file: Path, *extra: str) -> tuple[Path, Path]:
    out = tmp_path / "fulfillment.csv"
    register = tmp_path / "register.csv"
    mod.main(
        [
            "--sales-csv",
            str(sales),
            "--out",
            str(out),
            "--register",
            str(register),
            "--private-key-file",
            str(key_file),
            *extra,
        ]
    )
    return out, register


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_happy_path_all_three_tiers(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        [
            'S1,Lummenna Pro,Ada Lovelace,ada@example.com,2026-08-20 14:03:11 UTC,"(Tier: Solo)",false,false',
            'S2,Lummenna Pro,Grace Hopper,grace@example.com,2026-08-20 15:00:00 UTC,"(Tier: Team)",false,false',
            'S3,Lummenna Pro,,biz@example.com,2026-08-21 09:30:00 UTC,"(Tier: Business)",false,false',
        ],
    )
    out, register = _run(mod, tmp_path, sales, signing_key)

    out_rows = _read_rows(out)
    assert [r["tier"] for r in out_rows] == ["solo", "team", "business"]
    assert [r["seats"] for r in out_rows] == ["3", "10", "25"]
    assert [r["expires"] for r in out_rows] == ["2027-08-20", "2027-08-20", "2027-08-21"]

    # Every key is a real, verifiable Pro license with the planned entitlements.
    for row in out_rows:
        lic = licensing.parse_license_key(row["license_key"])
        assert lic.is_pro
        assert lic.grants("compliance") and lic.grants("multi_tenant")
        assert lic.seats == int(row["seats"])
        assert str(lic.expires) == row["expires"]
        assert lic.license_id == row["license_id"]
    # Named buyer -> "Name <email>" subject; anonymous buyer -> bare email.
    assert licensing.parse_license_key(out_rows[0]["license_key"]).subject == (
        "Ada Lovelace <ada@example.com>"
    )
    assert licensing.parse_license_key(out_rows[2]["license_key"]).subject == "biz@example.com"

    reg_rows = _read_rows(register)
    assert [r["sale_id"] for r in reg_rows] == ["S1", "S2", "S3"]
    assert all(r["features"] == "*" for r in reg_rows)
    assert all(r["license_id"] and r["issued_at"] for r in reg_rows)


def test_refunded_and_chargedback_rows_are_skipped(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        [
            'S1,Lummenna Pro,A,a@example.com,2026-08-20,"(Tier: Solo)",true,false',
            'S2,Lummenna Pro,B,b@example.com,2026-08-20,"(Tier: Team)",false,true',
            'S3,Lummenna Pro,C,c@example.com,2026-08-20,"(Tier: Team)",false,false',
        ],
    )
    out, register = _run(mod, tmp_path, sales, signing_key)
    assert [r["sale_id"] for r in _read_rows(out)] == ["S3"]
    assert [r["sale_id"] for r in _read_rows(register)] == ["S3"]


def test_rerun_is_idempotent_via_register(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        ['S1,Lummenna Pro,A,a@example.com,2026-08-20,"(Tier: Solo)",false,false'],
    )
    out, register = _run(mod, tmp_path, sales, signing_key)
    assert len(_read_rows(register)) == 1

    out2 = tmp_path / "fulfillment-2.csv"
    mod.main(
        [
            "--sales-csv",
            str(sales),
            "--out",
            str(out2),
            "--register",
            str(register),
            "--private-key-file",
            str(signing_key),
        ]
    )
    # Nothing new: no second fulfillment file, register unchanged.
    assert not out2.exists()
    assert len(_read_rows(register)) == 1


def test_duplicate_sale_id_within_one_csv_minted_once(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        [
            'S1,Lummenna Pro,A,a@example.com,2026-08-20,"(Tier: Solo)",false,false',
            'S1,Lummenna Pro,A,a@example.com,2026-08-20,"(Tier: Solo)",false,false',
        ],
    )
    out, register = _run(mod, tmp_path, sales, signing_key)
    assert len(_read_rows(out)) == 1
    assert len(_read_rows(register)) == 1


def test_unknown_variant_aborts_atomically(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        [
            'S1,Lummenna Pro,A,a@example.com,2026-08-20,"(Tier: Solo)",false,false',
            'S2,Lummenna Pro,B,b@example.com,2026-08-20,"(Tier: Galactic)",false,false',
        ],
    )
    out = tmp_path / "fulfillment.csv"
    register = tmp_path / "register.csv"
    with pytest.raises(SystemExit) as excinfo:
        mod.main(
            [
                "--sales-csv",
                str(sales),
                "--out",
                str(out),
                "--register",
                str(register),
                "--private-key-file",
                str(signing_key),
            ]
        )
    assert excinfo.value.code == 2
    # Atomic: the valid S1 row must NOT have been minted either.
    assert not out.exists()
    assert not register.exists()


def test_unparseable_date_aborts_and_suggests_anchor_today(mod, signing_key, tmp_path, capsys):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        ['S1,Lummenna Pro,A,a@example.com,someday,"(Tier: Solo)",false,false'],
    )
    with pytest.raises(SystemExit) as excinfo:
        _run(mod, tmp_path, sales, signing_key)
    assert excinfo.value.code == 2
    assert "--anchor-today" in capsys.readouterr().err


def test_anchor_today_with_deterministic_today(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        ['S1,Lummenna Pro,A,a@example.com,,"(Tier: Team)",false,false'],
    )
    out, _ = _run(mod, tmp_path, sales, signing_key, "--anchor-today", "--today", "2026-08-20")
    assert _read_rows(out)[0]["expires"] == "2027-08-20"


def test_missing_email_column_fails_with_guidance(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        ["S1,Lummenna Pro"],
        headers="Purchase ID,Item Name",
    )
    with pytest.raises(SystemExit) as excinfo:
        _run(mod, tmp_path, sales, signing_key)
    assert "--col-email" in str(excinfo.value)


def test_default_tier_for_variantless_product(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        ["S1,Lummenna Pro,A,a@example.com,2026-08-20"],
        headers="Purchase ID,Item Name,Buyer Name,Purchase Email,Purchase Date",
    )
    out, _ = _run(mod, tmp_path, sales, signing_key, "--default-tier", "team")
    row = _read_rows(out)[0]
    assert row["tier"] == "team"
    assert row["seats"] == "10"


def test_custom_tier_specs_replace_defaults(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        ['S1,Lummenna Pro,A,a@example.com,2026-08-20,"(Tier: Studio)",false,false'],
    )
    out, _ = _run(mod, tmp_path, sales, signing_key, "--tier", "studio=7")
    row = _read_rows(out)[0]
    assert row["tier"] == "studio"
    assert licensing.parse_license_key(row["license_key"]).seats == 7


def test_malformed_tier_spec_rejected(mod):
    with pytest.raises(SystemExit):
        mod._parse_tier_specs(["team"])
    with pytest.raises(SystemExit):
        mod._parse_tier_specs(["team=lots"])
    with pytest.raises(SystemExit):
        mod._parse_tier_specs(["team=-1"])


def test_features_subset_grant(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        ['S1,Lummenna Pro,A,a@example.com,2026-08-20,"(Tier: Solo)",false,false'],
    )
    out, _ = _run(mod, tmp_path, sales, signing_key, "--features", "compliance,multi_tenant")
    lic = licensing.parse_license_key(_read_rows(out)[0]["license_key"])
    assert lic.grants("compliance") and lic.grants("multi_tenant")
    assert not lic.grants("advanced_p2p")


def test_dry_run_mints_and_writes_nothing(mod, signing_key, tmp_path, capsys):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        ['S1,Lummenna Pro,A,a@example.com,2026-08-20,"(Tier: Solo)",false,false'],
    )
    out = tmp_path / "fulfillment.csv"
    register = tmp_path / "register.csv"
    mod.main(
        [
            "--sales-csv",
            str(sales),
            "--out",
            str(out),
            "--register",
            str(register),
            "--private-key-file",
            str(signing_key),
            "--dry-run",
        ]
    )
    assert "DRY RUN" in capsys.readouterr().err
    assert not out.exists() and not register.exists()


def test_keychain_source_reads_security_output(mod, signing_key, monkeypatch, tmp_path):
    priv_b64 = signing_key.read_text().strip()

    def fake_run(cmd, capture_output, text, check):
        assert cmd[:2] == ["security", "find-generic-password"]
        assert "-s" in cmd and "custom-service" in cmd
        return SimpleNamespace(returncode=0, stdout=priv_b64 + "\n", stderr="")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", fake_run)
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        ['S1,Lummenna Pro,A,a@example.com,2026-08-20,"(Tier: Solo)",false,false'],
    )
    out = tmp_path / "fulfillment.csv"
    register = tmp_path / "register.csv"
    mod.main(
        [
            "--sales-csv",
            str(sales),
            "--out",
            str(out),
            "--register",
            str(register),
            "--keychain",
            "--keychain-service",
            "custom-service",
        ]
    )
    lic = licensing.parse_license_key(_read_rows(out)[0]["license_key"])
    assert lic.is_pro


def test_keychain_missing_item_exits_with_pointer(mod, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=44, stdout="", stderr="not found"),
    )
    with pytest.raises(SystemExit) as excinfo:
        mod._read_keychain_secret("nope")
    assert "LummennaSigningVault" in str(excinfo.value)


def test_wrong_signing_key_fails_before_writing_files(mod, signing_key, tmp_path, monkeypatch):
    """A key that does not match the embedded pubkey aborts with no output files."""
    issue_mod = _load("issue_license", _ISSUE_SCRIPT)
    other_priv, _other_pub = issue_mod.generate_keypair()
    wrong_key_file = tmp_path / "wrong.b64"
    wrong_key_file.write_text(other_priv + "\n")

    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        ['S1,Lummenna Pro,A,a@example.com,2026-08-20,"(Tier: Solo)",false,false'],
    )
    out = tmp_path / "fulfillment.csv"
    register = tmp_path / "register.csv"
    with pytest.raises(SystemExit) as excinfo:
        mod.main(
            [
                "--sales-csv",
                str(sales),
                "--out",
                str(out),
                "--register",
                str(register),
                "--private-key-file",
                str(wrong_key_file),
            ]
        )
    assert "verification" in str(excinfo.value)
    assert not out.exists() and not register.exists()


def test_register_preserved_rows_and_appends(mod, signing_key, tmp_path):
    """Appending to an existing register keeps prior rows byte-identical in data."""
    sales1 = _write_sales_csv(
        tmp_path / "s1.csv",
        ['S1,Lummenna Pro,A,a@example.com,2026-08-20,"(Tier: Solo)",false,false'],
    )
    out, register = _run(mod, tmp_path, sales1, signing_key)
    first = _read_rows(register)[0]

    sales2 = _write_sales_csv(
        tmp_path / "s2.csv",
        ['S2,Lummenna Pro,B,b@example.com,2026-08-21,"(Tier: Team)",false,false'],
    )
    out2 = tmp_path / "fulfillment-2.csv"
    mod.main(
        [
            "--sales-csv",
            str(sales2),
            "--out",
            str(out2),
            "--register",
            str(register),
            "--private-key-file",
            str(signing_key),
        ]
    )
    rows = _read_rows(register)
    assert len(rows) == 2
    assert rows[0] == first
    assert rows[1]["sale_id"] == "S2"


# --- Processors that report a status enum instead of a boolean refund flag ---
#
# Stripe exports "Status: Paid|Refunded|Failed" and an "Amount Refunded" figure,
# never a true/false. Reading either through _truthy yields False, so a refunded
# sale used to mint a license. These licenses verify offline and cannot be
# revoked, so that failure mode had no remedy.

STRIPE_HEADERS = (
    "id,Created date (UTC),Amount,Amount Refunded,Currency,Status,"
    "Description,Customer Email,Customer Name,tier"
)

_STRIPE_COLS = (
    "--col-sale-id",
    "id",
    "--col-date",
    "Created date (UTC)",
    "--col-email",
    "Customer Email",
    "--col-name",
    "Customer Name",
    "--col-variant",
    "tier",
    "--col-product",
    "Description",
)


def _stripe_row(sale_id: str, email: str, status: str, tier: str, refunded: str = "0.00") -> str:
    return (
        f"{sale_id},2026-08-20 09:14:02,199.00,{refunded},usd,{status},"
        f"Lummenna Pro,{email},Buyer {sale_id},{tier}"
    )


def test_paid_status_allowlist_mints_paid_and_skips_refunded(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "stripe.csv",
        [
            _stripe_row("pi_1", "ada@example.com", "Paid", "Solo"),
            _stripe_row("pi_2", "alan@example.com", "Refunded", "Solo", refunded="199.00"),
            _stripe_row("pi_3", "grace@example.com", "Paid", "Team"),
        ],
        headers=STRIPE_HEADERS,
    )
    out, _ = _run(
        mod,
        tmp_path,
        sales,
        signing_key,
        *_STRIPE_COLS,
        "--col-status",
        "Status",
        "--paid-status",
        "Paid",
    )
    minted = {row["sale_id"] for row in _read_rows(out)}
    assert minted == {"pi_1", "pi_3"}, "a refunded sale must never be minted a license"


def test_status_column_used_as_refund_flag_aborts_instead_of_minting(mod, signing_key, tmp_path):
    """The original defect: --col-refunded pointed at a status enum minted everything."""
    sales = _write_sales_csv(
        tmp_path / "stripe.csv",
        [
            _stripe_row("pi_1", "ada@example.com", "Paid", "Solo"),
            _stripe_row("pi_2", "alan@example.com", "Refunded", "Solo", refunded="199.00"),
        ],
        headers=STRIPE_HEADERS,
    )
    with pytest.raises(SystemExit) as excinfo:
        _run(mod, tmp_path, sales, signing_key, *_STRIPE_COLS, "--col-refunded", "Status")
    assert excinfo.value.code == 2
    assert not (tmp_path / "fulfillment.csv").exists(), "aborted run must mint nothing"


def test_amount_refunded_as_refund_flag_aborts(mod, signing_key, tmp_path):
    """'0.00' is not a boolean either; a money column must not be read as one."""
    sales = _write_sales_csv(
        tmp_path / "stripe.csv",
        [_stripe_row("pi_1", "ada@example.com", "Paid", "Solo")],
        headers=STRIPE_HEADERS,
    )
    with pytest.raises(SystemExit) as excinfo:
        _run(mod, tmp_path, sales, signing_key, *_STRIPE_COLS, "--col-refunded", "Amount Refunded")
    assert excinfo.value.code == 2


def test_unrecognised_status_is_excluded_not_minted(mod, signing_key, tmp_path):
    """The allowlist fails closed: an unfamiliar status skips rather than mints."""
    sales = _write_sales_csv(
        tmp_path / "stripe.csv",
        [
            _stripe_row("pi_1", "ada@example.com", "Paid", "Solo"),
            _stripe_row("pi_2", "alan@example.com", "disputed", "Solo"),
            _stripe_row("pi_3", "grace@example.com", "incomplete_expired", "Team"),
        ],
        headers=STRIPE_HEADERS,
    )
    out, _ = _run(
        mod,
        tmp_path,
        sales,
        signing_key,
        *_STRIPE_COLS,
        "--col-status",
        "Status",
        "--paid-status",
        "Paid",
    )
    assert {row["sale_id"] for row in _read_rows(out)} == {"pi_1"}


def test_paid_status_matching_is_case_insensitive(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "stripe.csv",
        [_stripe_row("pi_1", "ada@example.com", "PAID", "Solo")],
        headers=STRIPE_HEADERS,
    )
    out, _ = _run(
        mod,
        tmp_path,
        sales,
        signing_key,
        *_STRIPE_COLS,
        "--col-status",
        "Status",
        "--paid-status",
        "paid",
    )
    assert len(_read_rows(out)) == 1


def test_paid_status_without_a_status_column_is_rejected(mod, signing_key, tmp_path):
    sales = _write_sales_csv(
        tmp_path / "sales.csv",
        ["S1,Lummenna Pro,Ada,ada@example.com,2026-08-20,(Tier: Solo),,"],
    )
    with pytest.raises(SystemExit) as excinfo:
        _run(mod, tmp_path, sales, signing_key, "--paid-status", "Paid")
    assert "--col-status" in str(excinfo.value)
