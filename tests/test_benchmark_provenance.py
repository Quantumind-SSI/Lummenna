"""Tests for benchmark artifact provenance capture.

A benchmark number without its environment is not evidence. These tests pin the
two properties that matter: provenance is always recorded, and it is never
fabricated when unavailable.
"""

from __future__ import annotations

import json

import pytest

from benchmarks.provenance import (
    SCHEMA_VERSION,
    capture_environment,
    describe_platform,
    sha256_file,
    write_artifact,
)


class TestCaptureEnvironment:
    def test_records_the_fields_a_reader_needs(self):
        environment = capture_environment()

        assert environment["platform"]["machine"]
        assert environment["python"]["version"]
        assert "packages" in environment
        assert "repository" in environment
        assert environment["captured_at_utc"].endswith("+00:00")

    def test_is_json_serialisable(self):
        json.dumps(capture_environment())

    def test_records_lummenna_version(self):
        assert capture_environment()["packages"]["lummenna"] is not None

    def test_absent_packages_are_none_not_omitted(self):
        packages = capture_environment()["packages"]

        assert "datasets" in packages  # key present whether installed or not

    def test_never_raises(self):
        for _ in range(3):
            capture_environment()

    def test_clean_tree_is_false_not_unknown(self, monkeypatch):
        """A verified-clean tree must report False, never None.

        ``git status --porcelain`` prints nothing for a clean tree. Collapsing
        that empty output to None makes "verified clean" indistinguishable from
        "could not determine", and clean is the claim a citable artifact rests
        on.
        """
        import benchmarks.provenance as provenance

        monkeypatch.setattr(
            provenance,
            "_git",
            lambda *args: "" if args[0] == "status" else "a" * 40,
        )

        repository = provenance.capture_environment()["repository"]
        assert repository["dirty"] is False
        assert repository["commit"] == "a" * 40

    def test_dirty_tree_is_true(self, monkeypatch):
        import benchmarks.provenance as provenance

        monkeypatch.setattr(
            provenance,
            "_git",
            lambda *args: " M file.py" if args[0] == "status" else "b" * 40,
        )

        assert provenance.capture_environment()["repository"]["dirty"] is True

    def test_unavailable_git_is_none_not_false(self, monkeypatch):
        """Absent git means unknown, which must not be reported as clean."""
        import benchmarks.provenance as provenance

        monkeypatch.setattr(provenance, "_git", lambda *args: None)

        repository = provenance.capture_environment()["repository"]
        assert repository["dirty"] is None
        assert repository["commit"] is None


class TestDescribePlatform:
    def test_renders_system_and_machine(self):
        label = describe_platform(capture_environment())

        assert "/" in label

    @pytest.mark.parametrize("value", [None, {}, {"platform": {}}, {"platform": "nonsense"}])
    def test_never_fabricates_a_platform(self, value):
        """An artifact without provenance must say so, not guess a platform."""
        assert describe_platform(value) == "platform not recorded"


class TestWriteArtifact:
    def test_writes_payload_with_envelope(self, tmp_path):
        path = tmp_path / "result.json"

        digest = write_artifact(path, {"benchmark": "demo", "score": 0.5})

        written = json.loads(path.read_text())
        assert written["benchmark"] == "demo"
        assert written["score"] == 0.5
        assert written["schema_version"] == SCHEMA_VERSION
        assert written["environment"]["platform"]["machine"]
        assert digest == sha256_file(path)

    def test_does_not_mutate_the_callers_payload(self, tmp_path):
        payload = {"benchmark": "demo"}

        write_artifact(tmp_path / "r.json", payload)

        assert payload == {"benchmark": "demo"}

    def test_preserves_a_precaptured_environment(self, tmp_path):
        """A caller that captured once must not have it silently replaced.

        The report and the artifact have to describe the same run.
        """
        environment = capture_environment()
        path = tmp_path / "r.json"

        write_artifact(path, {"benchmark": "demo", "environment": environment})

        assert json.loads(path.read_text())["environment"] == environment

    def test_creates_missing_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "r.json"

        write_artifact(path, {"benchmark": "demo"})

        assert path.is_file()

    def test_ends_with_a_newline(self, tmp_path):
        path = tmp_path / "r.json"

        write_artifact(path, {"benchmark": "demo"})

        assert path.read_text().endswith("\n")

    def test_non_serialisable_payload_raises(self, tmp_path):
        with pytest.raises(TypeError):
            write_artifact(tmp_path / "r.json", {"bad": object()})


class TestSha256File:
    def test_digest_is_stable(self, tmp_path):
        path = tmp_path / "f.bin"
        path.write_bytes(b"lummenna")

        assert sha256_file(path) == sha256_file(path)
        assert len(sha256_file(path)) == 64

    def test_missing_file_returns_none(self, tmp_path):
        assert sha256_file(tmp_path / "absent") is None
