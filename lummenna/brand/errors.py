"""D15: Centralized Error/Exception Hierarchy.

Input wire: Brand bible error code spec
Output wire: All modules
Secret sauce: Unified error identity across CLI, logs, and API
"""


class LummennaError(Exception):
    """Base for all Lummenna exceptions."""

    code = "LME-0000"


class PalaceError(LummennaError):
    """Errors within the memory palace (Force A)."""

    pass


class RoomNotFoundError(PalaceError):
    """Requested room does not exist in the palace."""

    code = "LME-1001"

    def __init__(self, room: str) -> None:
        super().__init__(f"The room '{room}' has faded. Let me search nearby loci.")


class LocusConflictError(PalaceError):
    """Two loci collide or violate uniqueness."""

    code = "LME-1002"

    def __init__(self, room: str, name: str) -> None:
        super().__init__(
            f"The locus '{name}' already exists within '{room}'. "
            "Try a more specific name to avoid overlap."
        )


class ConsolidationFailedError(PalaceError):
    """Sleep-phase consolidation could not complete."""

    code = "LME-1003"

    def __init__(self, detail: str = "") -> None:
        msg = "Consolidation failed. The palace will retry on the next sleep cycle."
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


class ContextError(LummennaError):
    """Errors within the context window (Force B)."""

    pass


class BudgetExceededError(ContextError):
    """Context window budget exceeded."""

    code = "LCX-2001"


class RetrievalEmptyError(ContextError):
    """No results returned for a retrieval query."""

    code = "LCX-2002"

    def __init__(self, query: str = "") -> None:
        msg = "No memories matched your query."
        if query:
            msg += f" Try rephrasing '{query[:60]}'."
        super().__init__(msg)


class AssemblyTimeoutError(ContextError):
    """Context assembly exceeded its time budget."""

    code = "LCX-2003"

    def __init__(self, budget_ms: float = 0) -> None:
        msg = "Context assembly timed out."
        if budget_ms > 0:
            msg += f" Budget was {budget_ms:.0f} ms."
        super().__init__(msg)


class TFCError(LummennaError):
    """Twin-Force Controller state error."""

    code = "LLM-3001"


class TFCStuckError(TFCError):
    """TFC has entered an unrecoverable attractor state."""

    code = "LLM-3002"

    def __init__(self) -> None:
        super().__init__(
            "The Twin-Force Controller is stuck. Resetting attention to neutral. "
            "Run 'lummenna tfc set --a 0.5 --e 0.5' to recover."
        )


class ValueModelUncalibratedError(TFCError):
    """V(m) weights are not yet learned for this user."""

    code = "LLM-3004"

    def __init__(self, user_id: str = "default") -> None:
        super().__init__(
            f"V(m) weights for '{user_id}' have not been calibrated yet. "
            "Provide explicit feedback on at least 5 results to train the value model."
        )


class SovereignViolationError(LummennaError):
    """Attempted external API call when sovereign mode is enabled."""

    code = "LLM-3003"

    def __init__(self, attempted_call: str) -> None:
        super().__init__(f"Sovereign boundary: external API call blocked ({attempted_call}).")


class ModelNotAvailableError(LummennaError):
    """Required ONNX embedding model is missing or cannot be loaded.

    ``model_path`` is retained as an attribute so a caller that wants to add
    context can re-raise with the *same* path plus a ``hint``, instead of
    passing the previous message back in as ``model_path``. Doing the latter
    nests the whole sentence inside "Embedding model not available at '...'"
    and repeats the provisioning advice twice, which is what `_init.py` did
    until 2026-09-10.
    """

    code = "LME-2001"

    def __init__(self, model_path: str, hint: str = "") -> None:
        self.model_path = model_path
        msg = (
            f"Embedding model not available at '{model_path}'. "
            "Run 'lummenna model download <alias>' or 'lummenna init --download-model' to provision it."
        )
        if hint:
            msg += f" {hint}"
        super().__init__(msg)


class FeedbackLogError(LummennaError):
    """Could not write or read feedback log."""

    code = "LME-2002"

    def __init__(self, detail: str = "") -> None:
        msg = "Feedback could not be recorded. The palace will attempt recovery."
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


class LicenseError(LummennaError):
    """Base for licensing / entitlement errors."""

    code = "LLC-4000"


class LicenseRequiredError(LicenseError):
    """A Pro feature was used without a valid commercial license."""

    code = "LLC-4001"

    def __init__(self, feature: str) -> None:
        super().__init__(
            f"'{feature}' is a Lummenna Pro feature and requires a commercial license. "
            "Set LUMMENNA_LICENSE_KEY (or license_key in config), or contact "
            "license@lummenna.com. See https://lummenna.com/pricing."
        )
        self.feature = feature


class LicenseInvalidError(LicenseError):
    """A license key was supplied but failed validation."""

    code = "LLC-4002"

    def __init__(self, reason: str = "") -> None:
        msg = "The supplied Lummenna license key is invalid or expired."
        if reason:
            msg += f" ({reason})"
        msg += " Contact license@lummenna.com."
        super().__init__(msg)
        self.reason = reason


class SeatLimitError(LicenseError):
    """An operation would exceed the license's seat (concurrent-tenant) limit."""

    code = "LLC-4003"

    def __init__(self, seats: int, current: int) -> None:
        super().__init__(
            f"This Lummenna Pro license permits {seats} tenant seat(s); "
            f"{current} are already in use, so a new tenant cannot be created. "
            "Reduce active tenants or upgrade your seat count; contact "
            "license@lummenna.com. See https://lummenna.com/pricing."
        )
        self.seats = seats
        self.current = current



class TenantQuotaError(LummennaError):
    """A write would push a tenant past its configured active-chunk quota.

    Raised by the store write path when ``tenant_max_active_chunks > 0``.
    Unlike seat limits this is an operational quota, not a licensing gate.
    """

    code = "LME-6001"

    def __init__(self, tenant_id: str, limit: int, current: int) -> None:
        super().__init__(
            f"Tenant '{tenant_id}' already holds {current} active memories; "
            f"the configured quota is {limit}. The write was rejected so this "
            "tenant cannot evict other tenants' memories via storage pressure. "
            "Raise LUMMENNA_TENANT_MAX_ACTIVE_CHUNKS or forget memories first."
        )
        self.tenant_id = tenant_id
        self.limit = limit
        self.current = current


class BackupError(LummennaError):
    """Base for off-device database replication errors (lummenna.ops.backup)."""

    code = "LME-5000"


class BackupNotConfiguredError(BackupError):
    """Backup was requested but the configuration is incomplete."""

    code = "LME-5001"

    def __init__(self, missing: str) -> None:
        super().__init__(
            f"Off-device backup is enabled but '{missing}' is not set. "
            "Set LUMMENNA_BACKUP_ACCOUNT to the storage account name and ensure "
            "the workload has a managed identity with the Storage Blob Data "
            "Contributor role on that account."
        )
        self.missing = missing


class BackupDependencyMissingError(BackupError):
    """The optional Azure SDK extra is not installed."""

    code = "LME-5002"

    def __init__(self, package: str) -> None:
        super().__init__(
            f"Off-device backup requires '{package}', which is not installed. "
            "Install the optional extra: pip install 'lummenna[azure]'."
        )
        self.package = package


class BackupUploadError(BackupError):
    """A snapshot was produced but could not be replicated off-device."""

    code = "LME-5003"

    def __init__(self, blob_name: str, detail: str = "") -> None:
        msg = f"Backup snapshot '{blob_name}' could not be uploaded."
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)
        self.blob_name = blob_name
        self.detail = detail


class BackupRestoreError(BackupError):
    """A restore was attempted and failed, leaving the store untouched."""

    code = "LME-5004"

    def __init__(self, detail: str = "") -> None:
        msg = "Restore from off-device backup failed; the local store was left unchanged."
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)
        self.detail = detail


class BackupIntegrityError(BackupError):
    """A downloaded snapshot failed verification and was discarded."""

    code = "LME-5005"

    def __init__(self, blob_name: str, detail: str = "") -> None:
        msg = f"Backup snapshot '{blob_name}' failed integrity verification and was discarded."
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)
        self.blob_name = blob_name
        self.detail = detail
