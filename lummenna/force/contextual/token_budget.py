"""B1a: Token budget enforcement with real tokenizer.

Uses the same model tokenizer as the embedder for exact token counting.
Falls back to a character-heuristic only when the tokenizer is unavailable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from lummenna.logging import get_console_logger

logger = get_console_logger(__name__)


class TokenCounter(Protocol):
    """Protocol for anything that can count tokens in a string."""

    def count(self, text: str) -> int: ...


class CharHeuristicCounter:
    """Fallback counter: ~4 chars per token (crude but zero-dep)."""

    def count(self, text: str) -> int:
        return max(1, len(text) // 4)


class TransformersTokenizerCounter:
    """Exact token counter using a HuggingFace tokenizer."""

    def __init__(self, model_path: Path) -> None:
        from transformers import AutoTokenizer

        # Loads from a local model directory, never the Hub — no download
        # occurs, so revision pinning is moot.
        self.tokenizer = AutoTokenizer.from_pretrained(str(model_path))  # nosec B615

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False))


_HEURISTIC_COUNTER = CharHeuristicCounter()

# Loading a HuggingFace tokenizer reads and parses vocabulary files from disk.
# Context assembly runs on every turn, so an uncached load would put that cost
# on the critical path of every single request. Keyed by path and mtime so a
# re-provisioned model is picked up without a restart.
_COUNTER_CACHE: dict[tuple[str, int], TokenCounter] = {}
_COUNTER_CACHE_MAX = 8

# Paths already reported as unusable, so the warning is emitted once rather
# than on every assemble call. Bounded for the same reason as the cache.
_WARNED_PATHS: set[str] = set()
_WARNED_PATHS_MAX = 64


def _warn_once(event: str, path: str, **fields: object) -> None:
    """Emit a warning the first time a given path fails, then stay quiet."""
    if path in _WARNED_PATHS:
        return
    if len(_WARNED_PATHS) >= _WARNED_PATHS_MAX:
        _WARNED_PATHS.clear()
    _WARNED_PATHS.add(path)
    logger.warning(event, path=path, **fields)


def get_token_counter(model_path: Path | None = None) -> TokenCounter:
    """Return the best available token counter, caching successful loads.

    Falling back to the character heuristic is a correctness event, not a
    detail: budget enforcement silently becomes an estimate that can be wrong by
    up to 4x on non-Latin or dense text. Every fallback is therefore logged once
    per path, including the previously silent "directory does not exist" case.

    Args:
        model_path: Directory containing a HuggingFace tokenizer. If ``None``,
            missing, or unloadable, the character heuristic is returned.

    Returns:
        A :class:`TokenCounter`. Never raises.
    """
    if model_path is None:
        return _HEURISTIC_COUNTER

    path_str = str(model_path)
    try:
        mtime_ns = model_path.stat().st_mtime_ns
    except OSError:
        _warn_once(
            "tokenizer_path_missing_using_char_heuristic",
            path_str,
            impact="token budget is approximate (~4 chars/token)",
        )
        return _HEURISTIC_COUNTER

    key = (path_str, mtime_ns)
    cached = _COUNTER_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        counter: TokenCounter = TransformersTokenizerCounter(model_path)
    except Exception as exc:
        _warn_once("tokenizer_load_failed", path_str, error=str(exc))
        return _HEURISTIC_COUNTER

    if len(_COUNTER_CACHE) >= _COUNTER_CACHE_MAX:
        _COUNTER_CACHE.clear()
    _COUNTER_CACHE[key] = counter
    return counter
