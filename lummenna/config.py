"""D1: Configuration Schema & Runtime Loader.

Input wire: pydantic-settings, env vars
Output wire: EVERY other task
Secret sauce: Device-specific defaults (RPi5 vs Jetson vs generic)
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

from pydantic import PrivateAttr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


def _default_config_path() -> Path:
    return Path.home() / ".lummenna" / "config.toml"


# Supported device profiles — the single source of truth for the closed set,
# reused by LummennaConfig.device and validated at the CLI boundary.
DeviceProfile = Literal["rpi5", "jetson-orin", "orange-pi", "generic"]


DEVICE_PROFILES: dict[str, dict[str, object]] = {
    "rpi5": {"context_budget": 1024, "memory_limit_mb": 256, "vector_index": "sqlite-vec"},
    "jetson-orin": {"context_budget": 2048, "memory_limit_mb": 512, "vector_index": "usearch"},
    "orange-pi": {"context_budget": 1024, "memory_limit_mb": 256, "vector_index": "sqlite-vec"},
    "generic": {"context_budget": 2048, "memory_limit_mb": 512, "vector_index": "sqlite-vec"},
}


class LummennaConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LUMMENNA_",
        toml_file=_default_config_path(),
    )

    device: DeviceProfile = "generic"
    context_budget: int = 2048
    memory_limit_mb: int = 300
    # Maximum characters of chunk content shown per result by the
    # `lummenna_search` MCP tool (a ranked preview, not a full-content reader).
    # When a chunk's content exceeds this length it is cut and an ellipsis
    # marker is appended so the caller can tell a preview from a full chunk.
    # Raise it (e.g. LUMMENNA_SEARCH_PREVIEW_CHARS=2000) to see more per hit, or
    # read the chunk in full from the store via its chunk_id.
    search_preview_chars: int = 300
    embedding_model: str = "bge-small-en-v1.5"
    embedding_dims: int = 384
    # Testing escape hatch ONLY (LUMMENNA_ALLOW_MOCK_EMBEDDER=1): when the real
    # ONNX model is missing, substitute hash-based mock embeddings instead of
    # failing. Mock vectors make semantic search meaningless, so production
    # paths refuse to start without the real model unless this is set.
    allow_mock_embedder: bool = False
    # First-run convenience (LUMMENNA_AUTO_PROVISION_MODEL=1): when the real ONNX
    # embedding model is missing, download and export it automatically instead
    # of failing at startup. This performs a ONE-TIME network fetch from the
    # model hub and therefore crosses the sovereign boundary; it is opt-in and
    # off by default. After the first launch the model is cached locally and
    # every subsequent startup is fully offline/sovereign.
    auto_provision_model: bool = False
    vector_index: Literal["sqlite-vec", "usearch"] = "sqlite-vec"
    enable_frqad: bool = True
    # Recency shaping for the fusion rerank (lummenna.fusion.fuse_and_rerank).
    # The final score is a product, so the recency term is bounded to
    # [recency_floor, 1.0]: it expresses a preference for recently-rehearsed
    # memories without letting age annihilate relevance. Lower the floor to
    # weight recency harder; set it to 1.0 to disable recency shaping entirely.
    recency_tau_hours: float = 168.0
    recency_floor: float = 0.35
    enable_local_llm: bool = False
    local_llm_model: str = "Qwen3-1.7B-Q4_K_M.gguf"
    consolidation_cpu_percent: float = 5.0
    scheduler_granularity: int = 300
    consolidation_battery_threshold: int = 50
    device_name: str = "lummenna-device"
    # P2P Beam bind address. All-interfaces is intentional here: Beam exists
    # solely to share memories across a trusted LAN, is opt-in (a Beam
    # listener must be explicitly started), and every frame is
    # AES-256-GCM-encrypted and HMAC-signed.
    local_ip: str = "0.0.0.0"  # nosec B104
    beam_port: int = 8847
    store_path: Path = Path.home() / ".lummenna" / "store"
    model_path: Path = Path.home() / ".lummenna" / "models"
    memory_budget_mb: int = 64
    sovereign: bool = True
    log_level: str = "info"
    # Loopback by default: the memory API must not listen on the LAN unless
    # explicitly configured (LUMMENNA_API_HOST=0.0.0.0 — Docker does this).
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
    # Where Beam remembers the frame nonces it has already accepted
    # (LUMMENNA_P2P_REPLAY_STORE). "memory" keeps the set in the process, so a
    # restart empties it and a frame captured just beforehand can be replayed
    # once, for whatever is left of its window. "persistent" keeps the set in the
    # Lummenna database, closing that gap and sharing it across processes; it is
    # a Pro capability (advanced_p2p) and is validated at BeamNode construction.
    p2p_replay_store: Literal["memory", "persistent"] = "memory"
    # Clock skew tolerated between a Beam frame's timestamp and local time
    # (LUMMENNA_P2P_REPLAY_WINDOW_SECONDS). It is also the exact nonce retention
    # horizon: past this many seconds a frame fails the timestamp check outright,
    # so its nonce can be discarded. Widen it only for genuinely skewed clocks —
    # every extra second is another second a captured frame stays replayable.
    p2p_replay_window_seconds: int = 30
    # Ceiling on retained Beam nonces (LUMMENNA_P2P_REPLAY_MAX_ENTRIES). Expiry
    # bounds the set under normal traffic; this bounds it under flood, when
    # frames arrive faster than the window retires them. Overflow is evicted
    # nearest-expiry first and logged at WARNING.
    p2p_replay_max_entries: int = 100_000

    # Off-device database replication (lummenna.ops.backup). This exists for
    # hosted deployments whose local disk is ephemeral; it ships a consistent
    # snapshot of the SQLite store to Azure Blob Storage on a timer and restores
    # it on a cold start. It is an outbound network call by definition, so it is
    # gated on assert_sovereign() and can never run while sovereign is true.
    # Leaving this off (the default) is the correct choice for local installs.
    backup_enabled: bool = False
    # Storage account NAME, not a connection string and not a key. Auth is
    # workload identity (DefaultAzureCredential); no secret is ever configured.
    backup_account: str | None = None
    backup_container: str = "db-backups"
    # Snapshot cadence. The floor is enforced in ops.backup rather than here so
    # that a hand-edited config.toml cannot schedule a hot loop against blob.
    backup_interval_seconds: int = 3600
    # Number of snapshots retained; older blobs are pruned after each upload.
    backup_retention: int = 24

    # Licensing / entitlement (dual-license Pro edition). A signed license key
    # (LUMMENNA_LICENSE_KEY) unlocks Pro features; absent/invalid → Community.
    # Validation is fully offline (no phone-home) to preserve sovereignty.
    license_key: str | None = None

    _resolved: bool = PrivateAttr(default=False)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Register the TOML source declared by ``model_config.toml_file``.

        pydantic-settings does not read ``toml_file`` unless a
        :class:`TomlConfigSettingsSource` is added here; without this override it
        emits ``Config key `toml_file` ... will be ignored`` and the file written
        by ``lummenna init`` is silently inert.

        Precedence, highest first: constructor kwargs, environment
        (``LUMMENNA_*``), ``.env``, ``~/.lummenna/config.toml``, then secrets.
        Environment beats the file so a container can override a baked-in config.

        Returns:
            The ordered settings sources for this model.
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    def assert_sovereign(self, attempted_call: str) -> None:
        """Raise SovereignViolationError if sovereign mode is enabled.

        Call this before any operation that would touch an external network API.
        """
        if self.sovereign:
            from lummenna.brand.errors import SovereignViolationError

            raise SovereignViolationError(attempted_call)

    @property
    def edition(self) -> str:
        """Active edition: 'pro' if a valid Pro license is present, else 'community'."""
        from lummenna.licensing import current_edition

        return current_edition(self)

    def assert_licensed(self, feature: str) -> None:
        """Raise LicenseRequiredError unless a valid license entitles ``feature``.

        Call this before any Pro-only code path. Mirrors ``assert_sovereign``.
        See ``lummenna.licensing.PRO_FEATURES`` for valid feature keys.
        """
        from lummenna.licensing import assert_licensed

        assert_licensed(feature, self)

    def model_post_init(self, __context: object) -> None:
        """After field population, apply device-profile defaults."""
        self.resolve_device_defaults()

    @property
    def db_uri(self) -> str:
        return f"{self.store_path}/lummenna.db"

    @property
    def db_path(self) -> Path:
        return self.store_path / "lummenna.db"

    def resolve_device_defaults(self) -> None:
        """Apply device-profile defaults to fields the user did not set.

        "Did not set" is decided by ``model_fields_set`` (populated by every
        source: constructor, env, ``.env``, TOML), not by comparing against the
        generic default. A value-comparison sentinel cannot distinguish
        ``LUMMENNA_CONTEXT_BUDGET=2048`` from an unset field, so on an ``rpi5``
        it would silently rewrite the user's explicit 2048 to 1024.
        """
        if self._resolved:
            return
        profile = DEVICE_PROFILES.get(self.device, DEVICE_PROFILES["generic"])
        explicit = self.model_fields_set
        if "context_budget" not in explicit:
            self.context_budget = int(cast(int, profile.get("context_budget", self.context_budget)))
        if "memory_limit_mb" not in explicit:
            self.memory_limit_mb = int(
                cast(int, profile.get("memory_limit_mb", self.memory_limit_mb))
            )
        if "vector_index" not in explicit:
            vector_index = str(profile.get("vector_index", self.vector_index))
            if vector_index in ("sqlite-vec", "usearch"):
                self.vector_index = cast(Literal["sqlite-vec", "usearch"], vector_index)
        self._resolved = True


# Backward-compatible alias
LummennaConfig = LummennaConfig
