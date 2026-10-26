"""Tests for the `lummenna mcp` CLI command and its `--check` preflight.

The command exists so MCP clients can spawn `["lummenna", "mcp"]` instead of
`["python", "-m", "lummenna.integrations.mcp_server"]`. Two properties matter
enough to pin:

1. `--check` must fail loudly for every condition that would leave a client
   with no tools, including a *successfully started* server backed by a mock
   embedder.
2. The bare command must delegate to the server entry point and put nothing on
   stdout, which the JSON-RPC framing owns.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from lummenna._init import initialize_palace
from lummenna.brand.errors import ModelNotAvailableError
from lummenna.cli.main import app
from lummenna.config import LummennaConfig

pytestmark = pytest.mark.skipif(
    pytest.importorskip("mcp", reason="mcp package not installed") is None,
    reason="mcp package not installed",
)

runner = CliRunner()


@pytest.fixture
def palace_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Lummenna at an empty store/model dir with no ONNX model present."""
    store = tmp_path / "store"
    models = tmp_path / "models"
    store.mkdir()
    models.mkdir()
    monkeypatch.setenv("LUMMENNA_STORE_PATH", str(store))
    monkeypatch.setenv("LUMMENNA_MODEL_PATH", str(models))
    monkeypatch.delenv("LUMMENNA_ALLOW_MOCK_EMBEDDER", raising=False)
    # Rich wraps table cells to the terminal width (80 when not a tty) and
    # re-reads COLUMNS on every render. Without this, assertions on long
    # details like "pip install 'lummenna[mcp]'" fail on a line break rather
    # than on the message being absent.
    monkeypatch.setenv("COLUMNS", "200")
    return tmp_path


class _FakeConn:
    """Stands in for the sqlite3 connection the preflight must always close."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TestCommandRegistration:
    def test_mcp_is_a_top_level_command(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "mcp" in result.output

    def test_bare_mcp_is_not_a_subcommand_group(self) -> None:
        """`lummenna mcp` must serve, not print help.

        It is pasted verbatim into client configs as the spawn command. A
        Typer sub-app would print help to stdout and corrupt the handshake.
        """
        result = runner.invoke(app, ["mcp", "--help"])
        assert result.exit_code == 0
        assert "--check" in result.output
        # A command group would advertise sub-commands here.
        assert "Commands" not in result.output


class TestPreflightFailures:
    def test_missing_model_fails_and_names_the_alias(self, palace_env: Path) -> None:
        result = runner.invoke(app, ["mcp", "--check"])
        assert result.exit_code == 1
        assert "FAIL" in result.output
        assert "bge-small-en" in result.output
        assert "not ready" in result.output

    def test_mock_embedder_is_a_failure_not_a_pass(
        self, palace_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The server *starts* with a mock embedder, so it must be caught here.

        Reporting this as ready is the trap the preflight exists to close:
        every tool call would succeed and every result would be noise.
        """
        monkeypatch.setenv("LUMMENNA_ALLOW_MOCK_EMBEDDER", "1")
        result = runner.invoke(app, ["mcp", "--check"])
        assert result.exit_code == 1
        assert "mock embedder active" in result.output

    def test_missing_mcp_package_short_circuits_with_install_hint(self, palace_env: Path) -> None:
        with (
            patch("lummenna.integrations.mcp_server._MCP_AVAILABLE", False),
            patch(
                "lummenna.integrations.mcp_server._MCP_ERROR",
                ModuleNotFoundError("No module named 'mcp'"),
            ),
        ):
            result = runner.invoke(app, ["mcp", "--check"])
        assert result.exit_code == 1
        assert "lummenna[mcp]" in result.output
        # Nothing past the dependency check is meaningful, so it must not run.
        assert "palace database" not in result.output


class TestPreflightSuccess:
    def test_healthy_palace_reports_full_surface_and_exits_zero(self, palace_env: Path) -> None:
        conn = _FakeConn()
        healthy = {
            "conn": conn,
            "embedder": object(),
            "pipeline": object(),
            "conversation": object(),
            "tfc": object(),
            "embedding_model_available": True,
        }
        with patch("lummenna._init.initialize_palace", return_value=healthy):
            result = runner.invoke(app, ["mcp", "--check"])

        assert result.exit_code == 0
        assert "ready to serve" in result.output
        assert "FAIL" not in result.output
        # The live registry is queried, so the counts are not hardcoded here.
        assert "lummenna_search" in result.output
        assert "dashboard://lummenna/effectiveness" in result.output
        assert conn.closed, "preflight leaked the database connection"

    def test_connection_is_closed_even_when_a_later_check_fails(self, palace_env: Path) -> None:
        conn = _FakeConn()
        degraded = {
            "conn": conn,
            "embedder": object(),
            "pipeline": object(),
            "conversation": object(),
            "tfc": object(),
            "embedding_model_available": False,
        }
        with patch("lummenna._init.initialize_palace", return_value=degraded):
            result = runner.invoke(app, ["mcp", "--check"])

        assert result.exit_code == 1
        assert conn.closed, "preflight leaked the database connection on the failure path"


class TestServeDelegation:
    def test_bare_command_runs_the_stdio_server(self, palace_env: Path) -> None:
        with patch("lummenna.integrations.mcp_server.main") as run_server:
            result = runner.invoke(app, ["mcp"])
        assert result.exit_code == 0
        run_server.assert_called_once_with(transport="stdio", host=None, port=None)

    def test_check_never_starts_the_protocol_loop(self, palace_env: Path) -> None:
        with patch("lummenna.integrations.mcp_server.main") as run_server:
            runner.invoke(app, ["mcp", "--check"])
        run_server.assert_not_called()


class TestModelNotAvailableMessage:
    """Regression cover for the doubly-wrapped provisioning error.

    `_init.py` used to re-raise with `str(exc)` as `model_path`, nesting the
    whole previous sentence inside "Embedding model not available at '...'"
    and printing the provisioning advice twice.
    """

    def test_hint_is_appended_and_path_is_retained(self) -> None:
        err = ModelNotAvailableError("/models/bge", hint="Extra advice.")
        assert err.model_path == "/models/bge"
        assert str(err).startswith("Embedding model not available at '/models/bge'.")
        assert str(err).endswith("Extra advice.")

    def test_omitted_hint_leaves_the_base_message_untouched(self) -> None:
        assert str(ModelNotAvailableError("/models/bge")) == (
            "Embedding model not available at '/models/bge'. "
            "Run 'lummenna model download <alias>' or 'lummenna init "
            "--download-model' to provision it."
        )

    def test_initialize_palace_does_not_nest_the_message(self, palace_env: Path) -> None:
        config = LummennaConfig()
        with pytest.raises(ModelNotAvailableError) as excinfo:
            initialize_palace(config)

        message = str(excinfo.value)
        assert message.count("Embedding model not available at") == 1
        assert message.count("lummenna init --download-model") == 1
        # The real path survives, rather than a sentence standing in for it.
        assert str(config.model_path / config.embedding_model) in message
