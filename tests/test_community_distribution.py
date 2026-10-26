"""Checks for the direct-download Community distribution."""

import hashlib
import re
import tarfile
import time
import zipfile
from pathlib import Path

import pytest

from scripts import build_community_dist

ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS = ROOT / "website" / "downloads"
VERSION = "1.0.1rc1"
WHEEL = DOWNLOADS / f"lummenna-{VERSION}-py3-none-any.whl"
SDIST = DOWNLOADS / f"lummenna-{VERSION}.tar.gz"
SIDECAR = DOWNLOADS / f"lummenna-community-{VERSION}.Dockerfile"
CHECKSUMS = DOWNLOADS / "SHA256SUMS"
MAX_PAGES_FILE_BYTES = 25 * 1024 * 1024
FORBIDDEN_CONTENT = (
    b"github.com/QuantmindSSI/lummenna",
    b"licensing@quantumindssi.com",
    b"admin@quantumindssi.com",
    b"lummenna@outlook.com",
    b"incident-2026",
    b"LICENSE_SIGNING_KEY_CUSTODY",
    b"ownership-attestation",
    b"WEB-DEPLOYMENT.md",
    b"pypi-AgEI",
)


def sha256(path: Path) -> str:
    """Return a file's SHA-256 digest."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checksum_entries() -> dict[str, str]:
    """Parse the published SHA256SUMS file."""
    entries = {}
    for line in CHECKSUMS.read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ", maxsplit=1)
        entries[name] = digest
    return entries


def test_download_artifacts_exist_and_match_checksums():
    expected = (WHEEL, SDIST, SIDECAR)
    entries = checksum_entries()
    assert set(entries) == {path.name for path in expected}
    for artifact in expected:
        assert artifact.is_file()
        assert 0 < artifact.stat().st_size < MAX_PAGES_FILE_BYTES
        assert entries[artifact.name] == sha256(artifact)


def test_wheel_metadata_describes_the_public_prerelease():
    with zipfile.ZipFile(WHEEL) as package:
        metadata_name = next(name for name in package.namelist() if name.endswith("/METADATA"))
        metadata = package.read(metadata_name).decode("utf-8")
    assert "Metadata-Version: 2.4" in metadata
    assert "Version: 1.0.1rc1" in metadata
    assert "License-Expression: AGPL-3.0-or-later" in metadata
    assert "Development Status :: 4 - Beta" in metadata
    assert "Project-URL: Homepage, https://lummenna.com" in metadata
    assert "Requires-Python: <3.13,>=3.10" in metadata
    assert "distributed through PyPI" in metadata


def test_archives_contain_no_private_distribution_data():
    with zipfile.ZipFile(WHEEL) as package:
        wheel_files = [package.read(name) for name in package.namelist() if not name.endswith("/")]
    with tarfile.open(SDIST, "r:gz") as package:
        source_files = [
            extracted.read()
            for member in package.getmembers()
            if member.isfile() and (extracted := package.extractfile(member)) is not None
        ]
    for content in (*wheel_files, *source_files):
        for marker in FORBIDDEN_CONTENT:
            assert marker.lower() not in content.lower()


def test_source_archive_has_a_bounded_release_surface():
    with tarfile.open(SDIST, "r:gz") as package:
        names = package.getnames()
        ignore_name = next(name for name in names if name.endswith("/.gitignore"))
        ignore_file = package.extractfile(ignore_name)
        assert ignore_file is not None
        assert ignore_file.read() == b"dist/\n"
    for forbidden_path in ("/.github/", "/functions/", "/scripts/", "/tests/", "/website/"):
        assert not any(forbidden_path in f"/{name}" for name in names)


def test_sidecar_pins_the_published_wheel_and_requires_authentication():
    content = SIDECAR.read_text(encoding="utf-8")
    assert checksum_entries()[WHEEL.name] in content
    assert f"https://lummenna.com/downloads/{WHEEL.name}" in content
    assert "LUMMENNA_API_KEY is required" in content
    assert "USER 10001:10001" in content
    assert "github.com" not in content.casefold()


def test_community_page_links_every_release_artifact():
    page = (ROOT / "website" / "community.html").read_text(encoding="utf-8")
    for artifact in (WHEEL, SDIST, SIDECAR, CHECKSUMS):
        assert f"/downloads/{artifact.name}" in page
    entries = checksum_entries()
    assert entries[WHEEL.name] in page
    assert entries[SDIST.name] in page
    assert 'python -m pip install --pre "lummenna[export]==1.0.1rc1"' in page
    assert "curl --fail --show-error --silent --remote-name" in page
    assert re.search(r"docker run --detach.*lummenna-community:1\.0\.1rc1", page, re.DOTALL)


@pytest.mark.parametrize(
    ("member", "content"),
    [
        ("package/.github/workflows/release.yml", b"safe"),
        ("package/module.py", b"https://github.com/QuantmindSSI/lummenna"),
    ],
)
def test_distribution_audit_rejects_private_material(tmp_path, member, content):
    archive = tmp_path / "candidate.whl"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(member, content)
    with pytest.raises(RuntimeError, match="forbidden path|private release data"):
        build_community_dist.audit_distribution(archive)


def test_distribution_audit_rejects_oversized_artifact(tmp_path, monkeypatch):
    archive = tmp_path / "candidate.whl"
    archive.write_bytes(b"large")
    monkeypatch.setattr(build_community_dist, "MAX_PAGES_FILE_BYTES", 1)
    with pytest.raises(RuntimeError, match="file-size limit"):
        build_community_dist.audit_distribution(archive)


def test_publisher_refuses_to_replace_versioned_artifacts(tmp_path):
    source_wheel = tmp_path / "source.whl"
    source_archive = tmp_path / "source.tar.gz"
    output = tmp_path / "published"
    output.mkdir()
    source_wheel.write_bytes(b"new wheel")
    source_archive.write_bytes(b"new source")
    (output / source_wheel.name).write_bytes(b"published wheel")

    with pytest.raises(RuntimeError, match="refusing to replace immutable artifact"):
        build_community_dist.publish_artifacts(output, (source_wheel, source_archive))


def test_checksum_writer_refuses_to_change_a_release_manifest(tmp_path):
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"content")
    (tmp_path / "SHA256SUMS").write_text("different\n", encoding="ascii")

    with pytest.raises(RuntimeError, match="refusing to replace immutable SHA256SUMS"):
        build_community_dist.write_checksums(tmp_path, (artifact,))


def test_unpublished_override_replaces_artifact_and_manifest(tmp_path):
    source_wheel = tmp_path / "source.whl"
    source_archive = tmp_path / "source.tar.gz"
    output = tmp_path / "published"
    output.mkdir()
    source_wheel.write_bytes(b"new wheel")
    source_archive.write_bytes(b"new source")
    (output / source_wheel.name).write_bytes(b"old wheel")
    (output / "SHA256SUMS").write_text("old\n", encoding="ascii")

    published = build_community_dist.publish_artifacts(
        output, (source_wheel, source_archive), replace_unpublished=True
    )
    checksum = build_community_dist.write_checksums(output, published, replace_unpublished=True)

    assert published[0].read_bytes() == b"new wheel"
    assert checksum.read_text(encoding="ascii") == build_community_dist.checksum_manifest(published)


def test_release_workflow_uses_oidc_and_the_sanitized_builder():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "python scripts/build_community_dist.py" in workflow
    assert "Tag must match package version" in workflow
    assert "python -m twine check" in workflow
    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "PYPI_API_TOKEN" not in workflow
    assert "password:" not in workflow


def test_community_wheel_build_is_byte_reproducible():
    """Two builds of the same tree must produce identical release bytes.

    The release workflow rebuilds the wheel in CI and relies on the
    immutability guard refusing to replace the committed artifact. That
    only holds if the builder is deterministic: `python -m build` assembles
    the wheel from the sdist, so without a pinned SOURCE_DATE_EPOCH the zip
    entry timestamps are extraction wall-clock time and every rebuild of an
    unchanged tree would differ. This test perturbs source mtimes the way
    a fresh checkout does and asserts the digests still match.
    """
    import os
    import subprocess
    import sys
    import tempfile

    def build_once(workspace: Path) -> str:
        stage = workspace / "source"
        stage.mkdir(parents=True)
        build_community_dist.copy_release_sources(stage)
        subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(workspace / "dist")],
            cwd=stage,
            check=True,
            capture_output=True,
        )
        wheel = next((workspace / "dist").glob("lummenna-*.whl"))
        return sha256(wheel)

    with tempfile.TemporaryDirectory(prefix="lummenna-repro-") as temporary:
        first = build_once(Path(temporary) / "first")
    # Perturb every source mtime, as a fresh git checkout would. Content is
    # untouched, so git status is unaffected; restoring mtimes is unnecessary.
    now = time.time()
    for module in (ROOT / "lummenna").rglob("*.py"):
        os.utime(module, (now + 11, now + 11))
    with tempfile.TemporaryDirectory(prefix="lummenna-repro-") as temporary:
        second = build_once(Path(temporary) / "second")
    assert first == second, (
        "wheel digest changed across rebuilds of an unchanged tree; "
        "SOURCE_DATE_EPOCH must be pinned in build_distributions"
    )
