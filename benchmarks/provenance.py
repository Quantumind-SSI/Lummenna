"""Reproducibility provenance for benchmark artifacts.

A benchmark number without its environment is not evidence. "20k chunks at
79 chunks/s" means nothing unless a reader can tell whether that was a Raspberry
Pi or a 64-core workstation, which embedder build produced it, and whether the
working tree was dirty at the time.

This module is the single writer of that metadata, so every suite records the
same fields in the same shape and no surface has to assert provenance the
artifact does not contain.

Design constraints:
    * Never raise. A provenance failure must degrade to ``None`` rather than
      destroy a completed benchmark run.
    * Never call the network. Version detection reads installed metadata only.
    * Record absence explicitly. A missing value is ``None``, never omitted and
      never guessed.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess  # nosec B404
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

#: Bumped when the artifact envelope changes shape. Loaders compare against it
#: so an old artifact is reported as unreadable rather than silently misparsed.
SCHEMA_VERSION = 1

#: Packages whose versions materially affect benchmark numbers. Kept explicit
#: rather than dumping the whole environment: a 300-package freeze buries the
#: five that matter and churns the artifact on every unrelated upgrade.
_TRACKED_PACKAGES = (
    "lummenna",
    "numpy",
    "scipy",
    "sentence-transformers",
    "torch",
    "transformers",
    "datasets",
    "sqlite-vec",
    "usearch",
    "rank-bm25",
)

_GIT_TIMEOUT_SECONDS = 5


def _git(*args: str) -> str | None:
    """Run a read-only git command, returning ``None`` on any failure.

    Args:
        *args: Arguments after ``git``.

    Returns:
        Stripped stdout, or ``None`` if git is absent, this is not a
        repository, or the command fails or times out.
    """
    try:
        completed = subprocess.run(  # nosec B603 B607
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            cwd=Path(__file__).resolve().parent.parent,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _package_versions() -> dict[str, str | None]:
    """Return installed versions for the tracked packages.

    Returns:
        Mapping of package name to version string, or ``None`` when the package
        is not installed in this environment.
    """
    versions: dict[str, str | None] = {}
    for name in _TRACKED_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _total_memory_gb() -> float | None:
    """Return total physical RAM in GiB, or ``None`` if it cannot be read."""
    try:
        import psutil

        return round(float(psutil.virtual_memory().total) / (1024**3), 2)
    except Exception:
        return None


def capture_environment() -> dict[str, Any]:
    """Capture the execution environment for an artifact.

    Every field is best-effort and independently nullable, so a locked-down or
    unusual host still produces a usable envelope.

    Returns:
        A JSON-serialisable mapping describing the host, interpreter, relevant
        package versions, and repository state.
    """
    commit = _git("rev-parse", "HEAD")
    dirty_output = _git("status", "--porcelain")
    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": {
            # `machine` is the field that settles "was this x86_64 or ARM".
            "system": platform.system() or None,
            "release": platform.release() or None,
            "machine": platform.machine() or None,
            "processor": platform.processor() or None,
            "platform_string": platform.platform() or None,
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "hardware": {
            "cpu_count_logical": os.cpu_count(),
            "total_memory_gb": _total_memory_gb(),
        },
        "packages": _package_versions(),
        "repository": {
            "commit": commit,
            # A dirty tree means the artifact is not reproducible from any
            # commit. Stating that is the whole point of recording it.
            "dirty": None if dirty_output is None else bool(dirty_output),
        },
    }


def sha256_file(path: Path) -> str | None:
    """Return the SHA-256 hex digest of a file, or ``None`` if unreadable.

    Args:
        path: File to digest.

    Returns:
        64-character lowercase hex digest, or ``None``.

    Complexity:
        O(file size), streamed in 64 KiB blocks so large artifacts do not have
        to fit in memory.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def write_artifact(path: Path, payload: dict[str, Any]) -> str | None:
    """Write a benchmark artifact with a provenance envelope.

    The envelope is added under reserved keys rather than merged into the
    payload, so a suite can never accidentally shadow provenance with a metric
    of the same name.

    Args:
        path: Destination ``.json`` path. Parent directories are created.
        payload: The suite's own result mapping. Must be JSON-serialisable.

    Returns:
        The SHA-256 of the bytes written, or ``None`` if the digest could not
        be computed afterwards.

    Raises:
        OSError: If the artifact cannot be written. A benchmark that cannot
            persist its result has failed and must not report success.
        TypeError: If ``payload`` is not JSON-serialisable.
    """
    enveloped = dict(payload)
    enveloped["schema_version"] = SCHEMA_VERSION
    # setdefault, not assignment: a caller that captured the environment up
    # front (so its report and its artifact describe the same run) must not
    # have it silently replaced by a second, later capture.
    enveloped.setdefault("environment", capture_environment())

    path.parent.mkdir(parents=True, exist_ok=True)
    # Trailing newline so the file is diff-friendly and POSIX-clean.
    text = json.dumps(enveloped, indent=2, sort_keys=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return sha256_file(path)


def describe_platform(environment: dict[str, Any] | None) -> str:
    """Render a short human-readable platform label from an environment block.

    Args:
        environment: An ``environment`` mapping produced by
            :func:`capture_environment`, or ``None``.

    Returns:
        A label such as ``"Darwin/arm64"``, or ``"platform not recorded"`` when
        the artifact predates provenance capture. Never fabricates a platform.
    """
    if not isinstance(environment, dict):
        return "platform not recorded"
    platform_block = environment.get("platform")
    if not isinstance(platform_block, dict):
        return "platform not recorded"
    system = platform_block.get("system")
    machine = platform_block.get("machine")
    if not system and not machine:
        return "platform not recorded"
    return "/".join(part for part in (system, machine) if part)
