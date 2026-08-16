"""D1: Configuration Schema & Runtime Loader.

Input wire: pydantic-settings, env vars
Output wire: EVERY other task
Secret sauce: Device-specific defaults (RPi5 vs Jetson vs generic)
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

from pydantic import PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_config_path() -> Path:
    return Path.home() / ".lumena" / "config.toml"


# Supported device profiles — the single source of truth for the closed set,
# reused by LumenaConfig.device and validated at the CLI boundary.
DeviceProfile = Literal["rpi5", "jetson-orin", "orange-pi", "generic"]


DEVICE_PROFILES: dict[str, dict[str, object]] = {
    "rpi5": {"context_budget": 1024, "memory_limit_mb": 256, "vector_index": "sqlite-vec"},
    "jetson-orin": {"context_budget": 2048, "memory_limit_mb": 512, "vector_index": "usearch"},
    "orange-pi": {"context_budget": 1024, "memory_limit_mb": 256, "vector_index": "sqlite-vec"},
    "generic": {"context_budget": 2048, "memory_limit_mb": 512, "vector_index": "sqlite-vec"},
}


class LumenaConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LUMENA_",
        toml_file=_default_config_path(),
    )

    device: DeviceProfile = "generic"
    context_budget: int = 2048
    memory_limit_mb: int = 300
    embedding_model: str = "bge-small-en-v1.5"
    embedding_dims: int = 384
    # Testing escape hatch ONLY (LUMENA_ALLOW_MOCK_EMBEDDER=1): when the real
    # ONNX model is missing, substitute hash-based mock embeddings instead of
    # failing. Mock vectors make semantic search meaningless, so production
    # paths refuse to start without the real model unless this is set.
    allow_mock_embedder: bool = False
    # First-run convenience (LUMENA_AUTO_PROVISION_MODEL=1): when the real ONNX
    # embedding model is missing, download and export it automatically instead
    # of failing at startup. This performs a ONE-TIME network fetch from the
    # model hub and therefore crosses the sovereign boundary; it is opt-in and
    # off by default. After the first launch the model is cached locally and
    # every subsequent startup is fully offline/sovereign.
    auto_provision_model: bool = False
    vector_index: Literal["sqlite-vec", "usearch"] = "sqlite-vec"
    enable_frqad: bool = True
    enable_local_llm: bool = False
    local_llm_model: str = "Qwen3-1.7B-Q4_K_M.gguf"
    consolidation_cpu_percent: float = 5.0
    scheduler_granularity: int = 300
    consolidation_battery_threshold: int = 50
    device_name: str = "lumena-device"
    # P2P Beam bind address. All-interfaces is intentional here: Beam exists
    # solely to share memories across a trusted LAN, is opt-in (a Beam
    # listener must be explicitly started), and every frame is
    # AES-256-GCM-encrypted and HMAC-signed.
    local_ip: str = "0.0.0.0"  # nosec B104
    beam_port: int = 8847
    store_path: Path = Path.home() / ".lumena" / "store"
    model_path: Path = Path.home() / ".lumena" / "models"
    memory_budget_mb: int = 64
    sovereign: bool = True
    log_level: str = "info"
    # Loopback by default: the memory API must not listen on the LAN unless
    # explicitly configured (LUMENA_API_HOST=0.0.0.0 — Docker does this).
    api_host: str = "127.0.0.1"
    api_port: int = 8848
    api_key: str | None = None
    api_rate_limit: str = "60/minute"
    allowed_origins: str = "http://localhost:8000,http://localhost:8848"
    request_max_size_bytes: int = 1_048_576
    pii_detection_enabled: bool = True
    pii_redaction_mode: Literal["block", "redact", "hash"] = "redact"
    pii_custom_patterns: str = ""
    tfc_novelty_threshold: float = 0.7
    tfc_repetition_threshold: float = 0.7
    tfc_context_pressure_threshold: float = 0.9
    tfc_satisfaction_threshold: float = -0.3
    tfc_step_conservation: float = 0.1
    tfc_step_attention: float = 0.1
    tfc_step_attention_repair: float = 0.2
    tfc_default_tau: float = 7.0
    tfc_default_resolution: int = 3
    encryption_key: str | None = None
    database_encryption_mode: Literal["sqlcipher", "fernet", "none"] = "none"
    p2p_encryption_key: str | None = None

    # Licensing / entitlement (dual-license Pro edition). A signed license key
    # (LUMENA_LICENSE_KEY) unlocks Pro features; absent/invalid → Community.
    # Validation is fully offline (no phone-home) to preserve sovereignty.
    license_key: str | None = None

    _resolved: bool = PrivateAttr(default=False)

    def assert_sovereign(self, attempted_call: str) -> None:
        """Raise SovereignViolationError if sovereign mode is enabled.

        Call this before any operation that would touch an external network API.
        """
        if self.sovereign:
            from lumena.brand.errors import SovereignViolationError

            raise SovereignViolationError(attempted_call)

    @property
    def edition(self) -> str:
        """Active edition: 'pro' if a valid Pro license is present, else 'community'."""
        from lumena.licensing import current_edition

        return current_edition(self)

    def assert_licensed(self, feature: str) -> None:
        """Raise LicenseRequiredError unless a valid license entitles ``feature``.

        Call this before any Pro-only code path. Mirrors ``assert_sovereign``.
        See ``lumena.licensing.PRO_FEATURES`` for valid feature keys.
        """
        from lumena.licensing import assert_licensed

        assert_licensed(feature, self)

    def model_post_init(self, __context: object) -> None:
        """After field population, apply device-profile defaults."""
        self.resolve_device_defaults()

    @property
    def db_uri(self) -> str:
        return f"{self.store_path}/lumena.db"

    @property
    def db_path(self) -> Path:
        return self.store_path / "lumena.db"

    def resolve_device_defaults(self) -> None:
        """Apply device-profile defaults for fields still at their generic defaults."""
        if self._resolved:
            return
        profile = DEVICE_PROFILES.get(self.device, DEVICE_PROFILES["generic"])
        if self.context_budget == 2048:
            self.context_budget = int(cast(int, profile.get("context_budget", self.context_budget)))
        if self.memory_limit_mb == 300:
            self.memory_limit_mb = int(
                cast(int, profile.get("memory_limit_mb", self.memory_limit_mb))
            )
        if self.vector_index == "sqlite-vec":
            vector_index = str(profile.get("vector_index", self.vector_index))
            if vector_index in ("sqlite-vec", "usearch"):
                self.vector_index = cast(Literal["sqlite-vec", "usearch"], vector_index)
        self._resolved = True


# Backward-compatible alias
LumenaConfig = LumenaConfig
