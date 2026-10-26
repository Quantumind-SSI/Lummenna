"""Build and audit the direct-download Lummenna Community distribution."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_PAGES_FILE_BYTES = 25 * 1024 * 1024
SOURCE_DATE_EPOCH = "1788480000"  # 2026-09-04T00:00:00Z — the 1.0.1rc1 preparation date
SIDECAR_NAME = "lummenna-community-1.0.1rc1.Dockerfile"
STAGED_PATHS = (
    "lummenna",
    "benchmarks/retrieval/results/retrieval_results.json",
    "benchmarks/beir/results/beir_results.json",
    "benchmarks/e2e/results/e2e_results.json",
    "PACKAGE-README.md",
    "pyproject.toml",
    "LICENSE",
    "LICENSES",
    "NOTICE",
    "COPYRIGHT",
)
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
FORBIDDEN_PATH_PARTS = (
    "/.github/",
    "/functions/",
    "/scripts/",
    "/tests/",
    "/website/",
    "/AGENTS.md",
)


def copy_release_sources(stage: Path) -> None:
    """Copy the bounded set of files required to build the Community package."""
    for relative in STAGED_PATHS:
        source = ROOT / relative
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    (stage / ".gitignore").write_text("dist/\n", encoding="utf-8")


def build_distributions(stage: Path, output: Path) -> tuple[Path, Path]:
    """Build the source archive and wheel from the sanitized staging tree."""
    # `python -m build` assembles the wheel from the sdist, so zip entry
    # timestamps come from extraction wall-clock time. Without pinning
    # SOURCE_DATE_EPOCH, two builds of the identical tree produce different
    # bytes, which breaks the immutable-artifact contract: CI rebuilds the
    # wheel and must reproduce the committed one byte-for-byte.
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(output)],
        cwd=stage,
        check=True,
        env={**os.environ, "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH},
    )
    wheels = sorted(output.glob("lummenna-*.whl"))
    sdists = sorted(output.glob("lummenna-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError("build must produce exactly one wheel and one source archive")
    return wheels[0], sdists[0]


def archive_files(archive: Path) -> list[tuple[str, bytes]]:
    """Read regular files from a wheel or gzip-compressed source archive."""
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as package:
            return [
                (name, package.read(name)) for name in package.namelist() if not name.endswith("/")
            ]
    with tarfile.open(archive, "r:gz") as package:
        return [
            (member.name, extracted.read())
            for member in package.getmembers()
            if member.isfile() and (extracted := package.extractfile(member)) is not None
        ]


def audit_distribution(archive: Path) -> None:
    """Reject oversized artifacts, internal paths, and private references."""
    if archive.stat().st_size > MAX_PAGES_FILE_BYTES:
        raise RuntimeError(f"{archive.name} exceeds the Cloudflare Pages file-size limit")
    for name, content in archive_files(archive):
        normalized = f"/{name}"
        if any(part in normalized for part in FORBIDDEN_PATH_PARTS):
            raise RuntimeError(f"{archive.name} contains forbidden path: {name}")
        for marker in FORBIDDEN_CONTENT:
            if marker.lower() in content.lower():
                raise RuntimeError(f"{archive.name}:{name} contains private release data")


def verify_sidecar(output: Path, wheel: Path, *, replace_unpublished: bool = False) -> Path:
    """Ensure the published sidecar recipe pins the wheel produced by this build.

    The ``LUMMENNA_WHEEL_SHA256`` pin is a function of the wheel bytes, so it
    changes with the wheel. ``--replace-unpublished`` rewrites a stale pin the
    same way it replaces unpublished artifact bytes; without it a stale pin is
    a hard error, keeping released pins immutable.
    """
    sidecar = output / SIDECAR_NAME
    content = sidecar.read_text(encoding="utf-8")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if digest not in content:
        stale_pin = re.search(r"^ARG LUMMENNA_WHEEL_SHA256=([0-9a-f]{64})$", content, re.M)
        if stale_pin is None or not replace_unpublished:
            raise RuntimeError(f"{SIDECAR_NAME} does not pin {wheel.name}")
        content = content.replace(stale_pin.group(1), digest)
        sidecar.write_text(content, encoding="utf-8")
    if f"https://lummenna.com/downloads/{wheel.name}" not in content:
        raise RuntimeError(f"{SIDECAR_NAME} does not download {wheel.name}")
    return sidecar


def checksum_manifest(artifacts: tuple[Path, ...]) -> str:
    """Return deterministic SHA-256 entries for the published artifacts."""
    lines = []
    for artifact in sorted(artifacts, key=lambda path: path.name):
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lines.append(f"{digest}  {artifact.name}")
    return "\n".join(lines) + "\n"


def publish_artifacts(
    output: Path, artifacts: tuple[Path, Path], *, replace_unpublished: bool = False
) -> tuple[Path, Path]:
    """Publish new artifacts without replacing bytes under an existing version."""
    output.mkdir(parents=True, exist_ok=True)
    published = (output / artifacts[0].name, output / artifacts[1].name)
    for source, target in zip(artifacts, published, strict=True):
        changed = (
            target.exists()
            and hashlib.sha256(target.read_bytes()).digest()
            != hashlib.sha256(source.read_bytes()).digest()
        )
        if changed and not replace_unpublished:
            raise RuntimeError(f"refusing to replace immutable artifact: {target.name}")
        shutil.copy2(source, target)
    return published


def write_checksums(
    output: Path, artifacts: tuple[Path, ...], *, replace_unpublished: bool = False
) -> Path:
    """Write the manifest, refusing to alter an existing release manifest."""
    checksum_path = output / "SHA256SUMS"
    content = checksum_manifest(artifacts)
    changed = checksum_path.exists() and checksum_path.read_text(encoding="ascii") != content
    if changed and not replace_unpublished:
        raise RuntimeError("refusing to replace immutable SHA256SUMS")
    checksum_path.write_text(content, encoding="ascii")
    return checksum_path


def main() -> int:
    """Build, audit, and checksum the direct-download release artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "website" / "downloads",
        help="Directory that receives the audited release artifacts",
    )
    parser.add_argument(
        "--replace-unpublished",
        action="store_true",
        help="Replace same-version artifacts only before their first production deployment",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    with tempfile.TemporaryDirectory(prefix="lummenna-community-") as temporary:
        workspace = Path(temporary)
        stage = workspace / "source"
        stage.mkdir()
        copy_release_sources(stage)
        artifacts = build_distributions(stage, workspace / "dist")
        for artifact in artifacts:
            audit_distribution(artifact)
        published = publish_artifacts(
            output, artifacts, replace_unpublished=args.replace_unpublished
        )
    sidecar = verify_sidecar(output, published[0], replace_unpublished=args.replace_unpublished)
    checksum_path = write_checksums(
        output, (*published, sidecar), replace_unpublished=args.replace_unpublished
    )
    for artifact in (*published, sidecar, checksum_path):
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
