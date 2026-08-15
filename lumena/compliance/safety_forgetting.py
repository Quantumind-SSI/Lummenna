"""A11: Safety-Triggered Forgetting (L4) & Compliance.

Input wire: Regex / spaCy NER patterns, user command channel
Output wire: A12 (audit log), A5 (provenance purge), D17 (log rotation)
Secret sauce: Provenance-chain deletion (GateMem requirement)
"""

import json
import re
import sqlite3

from lumena.logging import get_console_logger

logger = get_console_logger(__name__)

PII_PATTERNS = {
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b\d{3}-\d{3}-\d{4}\b"),
    "api_key": re.compile(r"[a-zA-Z0-9_-]{32,}"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}


def _compile_custom_patterns(custom_patterns: str) -> dict[str, re.Pattern]:
    """Compile comma-separated custom regex patterns.

    A malformed pattern is skipped with a WARNING — silently disabling a
    security control would leave the operator believing PII is filtered
    when it is not.
    """
    compiled: dict[str, re.Pattern] = {}
    if not custom_patterns:
        return compiled
    for idx, pat in enumerate(custom_patterns.split(",")):
        pat = pat.strip()
        if not pat:
            continue
        try:
            compiled[f"custom_{idx}"] = re.compile(pat)
        except re.error as exc:
            logger.warning("pii_custom_pattern_invalid", index=idx, pattern=pat, error=str(exc))
    return compiled


def safety_scan_chunk(content: str, custom_patterns: str = "") -> list[str]:
    """Return list of triggered safety rule names."""
    hits = []
    patterns = dict(PII_PATTERNS)
    patterns.update(_compile_custom_patterns(custom_patterns))
    for rule_name, rx in patterns.items():
        if rx.search(content):
            hits.append(rule_name)
    return hits


def _resolve_pattern(pattern_name: str, custom_patterns: str) -> re.Pattern | None:
    """Resolve a hit name to its compiled regex (built-in or custom)."""
    rx = PII_PATTERNS.get(pattern_name)
    if rx is not None:
        return rx
    if pattern_name.startswith("custom_"):
        return _compile_custom_patterns(custom_patterns).get(pattern_name)
    return None


def _hash_pattern(text: str, pattern_name: str, custom_patterns: str = "") -> str:
    """Replace matches with a stable short hash."""
    import hashlib

    rx = _resolve_pattern(pattern_name, custom_patterns)
    if rx is None:
        return text

    def _repl(m: re.Match) -> str:
        digest = hashlib.sha256(m.group(0).encode()).hexdigest()[:8]
        return f"[HASH:{digest}]"

    result: str = rx.sub(_repl, text)
    return result


def apply_pii_strategy(
    content: str, hits: list[str], mode: str, custom_patterns: str = ""
) -> str | None:
    """Apply the configured PII strategy.

    Args:
        content: Original chunk content.
        hits: List of triggered PII pattern names (built-in or ``custom_N``).
        mode: One of ``block``, ``redact``, ``hash``.
        custom_patterns: Comma-separated custom regexes; required so that
            ``custom_N`` hits are actually redacted/hashed rather than only
            detected.

    Returns:
        Modified content string, or ``None`` if the store should be blocked.
    """
    if mode == "block":
        return None
    if mode == "hash":
        for hit in hits:
            content = _hash_pattern(content, hit, custom_patterns)
        return content
    # Default: redact
    for hit in hits:
        rx = _resolve_pattern(hit, custom_patterns)
        if rx:
            content = rx.sub(lambda m: "[REDACTED]", content)
    return content


def safety_forget_chunk(conn: sqlite3.Connection, chunk_id: int, reason: str) -> None:
    """
    Immediate deletion with provenance-chain clearance.
    Write-ahead audit BEFORE mutation.
    """
    from lumena.audit import log_audit_event

    log_audit_event(
        conn=conn,
        event_type="compliance",
        actor="system",
        action="safety_forget",
        resource_id=chunk_id,
        resource_type="chunk",
        metadata_json=json.dumps({"reason": reason}),
        client_ip=None,
        request_id=None,
    )

    # Remove from FTS5
    conn.execute("DELETE FROM chunk_fts WHERE rowid = ?", (chunk_id,))
    # Remove the embedding vectors: leaving them behind would keep
    # recoverable derived data on disk after a right-to-be-forgotten request.
    for vec_table in ("vec_fallback", "vec_chunks"):
        try:
            conn.execute(
                f"DELETE FROM {vec_table} WHERE chunk_id = ?",  # nosec B608 — fixed table names
                (chunk_id,),
            )
        except sqlite3.OperationalError:
            # Table may not exist depending on the configured vector backend.
            logger.debug("safety_forget_vec_table_missing", table=vec_table)
    # Break FK from chunk -> provenance before clearing provenance
    conn.execute("UPDATE chunk SET provenance_root = NULL WHERE chunk_id = ?", (chunk_id,))
    # Clear provenance tree recursively
    _clear_provenance_tree(conn, chunk_id)
    # Logical delete in chunk table
    conn.execute(
        """UPDATE chunk
           SET valid_to = unixepoch(), content = '[REDACTED]', optical_level = 2
           WHERE chunk_id = ?""",
        (chunk_id,),
    )

    logger.warning("safety_forget_executed", chunk_id=chunk_id, reason=reason)


def _clear_provenance_tree(conn: sqlite3.Connection, chunk_id: int) -> None:
    """Walk provenance parent/child links and anonymise.

    Deletes provenance records recursively: first all child records
    that reference this chunk as their ``parent_provenance``, then the
    chunk's own provenance record.
    """
    # Walk children: find records whose parent_provenance references
    # any provenance row of this chunk.
    child_rows = conn.execute(
        """SELECT provenance_id FROM provenance
           WHERE parent_provenance IN (
               SELECT provenance_id FROM provenance WHERE chunk_id = ?
           )""",
        (chunk_id,),
    ).fetchall()
    for row in child_rows:
        conn.execute("DELETE FROM provenance WHERE provenance_id = ?", (row[0],))
    conn.execute("DELETE FROM provenance WHERE chunk_id = ?", (chunk_id,))


def get_recent_audit_events(n: int = 10) -> list[dict]:
    """Read last N compliance audit events from SQLite audit_log."""
    from lumena.config import LumenaConfig
    from lumena.data.schema import get_connection

    try:
        conn = get_connection(LumenaConfig())
        rows = conn.execute(
            """SELECT audit_id, event_type, actor, resource_type, resource_id,
                      action, metadata_json, client_ip, request_id, created_at
               FROM audit_log ORDER BY created_at DESC LIMIT ?""",
            (n,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []
