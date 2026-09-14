"""Tests for configuration source precedence and device-profile resolution.

``lummenna init`` writes ``~/.lummenna/config.toml``. pydantic-settings ignores
``model_config.toml_file`` unless a ``TomlConfigSettingsSource`` is registered,
so without the ``settings_customise_sources`` override that file is inert and
the CLI advertises a setting it never applies.
"""

import pytest

from lummenna.config import DEVICE_PROFILES, LummennaConfig


@pytest.fixture
def toml_config(tmp_path, monkeypatch):
    """Point LummennaConfig at a throwaway config.toml.

    ``toml_file`` is bound at class-definition time, so the path is patched on
    the already-built model_config rather than via the home directory.
    """
    path = tmp_path / "config.toml"
    original = LummennaConfig.model_config.get("toml_file")
    LummennaConfig.model_config["toml_file"] = path
    for key in list(LummennaConfig.model_fields):
        monkeypatch.delenv(f"LUMMENNA_{key.upper()}", raising=False)
    yield path
    LummennaConfig.model_config["toml_file"] = original


class TestTomlLoading:
    def test_toml_values_are_applied(self, toml_config):
        toml_config.write_text("context_budget = 9999\n")

        assert LummennaConfig().context_budget == 9999

    def test_missing_toml_falls_back_to_defaults(self, toml_config):
        assert LummennaConfig().context_budget == 2048

    def test_environment_overrides_toml(self, toml_config, monkeypatch):
        toml_config.write_text("context_budget = 9999\n")
        monkeypatch.setenv("LUMMENNA_CONTEXT_BUDGET", "512")

        assert LummennaConfig().context_budget == 512

    def test_constructor_overrides_toml(self, toml_config):
        toml_config.write_text("context_budget = 9999\n")

        assert LummennaConfig(context_budget=256).context_budget == 256

    def test_device_can_be_set_from_toml(self, toml_config):
        toml_config.write_text('device = "rpi5"\n')

        assert LummennaConfig().device == "rpi5"

    def test_recency_knobs_are_loadable_from_toml(self, toml_config):
        toml_config.write_text("recency_floor = 0.5\nrecency_tau_hours = 48.0\n")

        config = LummennaConfig()
        assert config.recency_floor == 0.5
        assert config.recency_tau_hours == 48.0


class TestDeviceProfileResolution:
    def test_profile_applies_when_field_is_unset(self, toml_config):
        toml_config.write_text('device = "rpi5"\n')

        config = LummennaConfig()
        assert config.context_budget == DEVICE_PROFILES["rpi5"]["context_budget"]
        assert config.memory_limit_mb == DEVICE_PROFILES["rpi5"]["memory_limit_mb"]

    def test_explicit_value_equal_to_generic_default_is_preserved(self, toml_config):
        """A value-comparison sentinel cannot tell 2048 from "unset".

        On an rpi5 the old sentinel silently rewrote an explicit 2048 to 1024.
        """
        toml_config.write_text('device = "rpi5"\ncontext_budget = 2048\n')

        assert LummennaConfig().context_budget == 2048

    def test_explicit_memory_limit_equal_to_default_is_preserved(self, toml_config):
        toml_config.write_text('device = "rpi5"\nmemory_limit_mb = 300\n')

        assert LummennaConfig().memory_limit_mb == 300

    def test_explicit_vector_index_is_preserved(self, toml_config):
        toml_config.write_text('device = "jetson-orin"\nvector_index = "sqlite-vec"\n')

        assert LummennaConfig().vector_index == "sqlite-vec"

    def test_jetson_profile_still_selects_usearch_when_unset(self, toml_config):
        toml_config.write_text('device = "jetson-orin"\n')

        assert LummennaConfig().vector_index == "usearch"

    def test_env_set_value_is_treated_as_explicit(self, toml_config, monkeypatch):
        monkeypatch.setenv("LUMMENNA_DEVICE", "rpi5")
        monkeypatch.setenv("LUMMENNA_CONTEXT_BUDGET", "2048")

        assert LummennaConfig().context_budget == 2048

    def test_generic_device_keeps_generic_defaults(self, toml_config):
        config = LummennaConfig()

        assert config.device == "generic"
        assert config.context_budget == DEVICE_PROFILES["generic"]["context_budget"]
