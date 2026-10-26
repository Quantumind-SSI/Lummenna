"""B3: Semantic Chunker — heading/paragraph-aware text splitting.

Input wire: raw attachment text / long store payloads
Output wire: A6 (store pipeline) — one chunk row per segment

Why this exists: the embedding model (bge-small-en-v1.5) encodes at most 512
tokens. ``store_memory`` embeds arbitrarily long content whole, so the dense
vector would represent only the first ~512 tokens and the rest of the document
would be invisible to semantic retrieval (the lexical index still sees it).
This module splits oversized content into retrieval-sized segments *before* the
write path, so every segment gets a faithful embedding.

Splitting is structure-first, not blind:

1. Split on blank-line paragraphs; Markdown headings (``#``-lines) start new
   sections so a segment never mixes two sections when avoidable.
2. Hard-split any paragraph still over the budget on sentence boundaries, then
   on words, then (for an unbreakable single token blob like base64) truncate.
3. Pack sections into segments greedily; merge undersized segments into a
   neighbour when the result fits.
4. Carry an overlap tail (default 48 tokens) from each segment into the next so
   a fact spanning a boundary remains retrievable from both sides.

Every size decision consults the caller's :class:`TokenCounter` — never a fixed
chars-per-token ratio. A 4:1 assumption understates CJK, base64 and minified
code by up to 4x (the same trap ``assembly._fit_chunk_block`` documents), which
would let an "under budget" segment overflow the embedder's 512-token window.
When an embedder is supplied, counting uses its exact tokenizer; otherwise a
char-heuristic counter (~4 chars/token) keeps the chunker usable before any
model is provisioned.
"""

from __future__ import annotations

import re

from lummenna.force.contextual.token_budget import (
    CharHeuristicCounter,
    TokenCounter,
)
from lummenna.logging import get_console_logger

__all__ = ["ChunkerCounter", "semantic_chunks", "token_aware_chunks"]

logger = get_console_logger(__name__)

#: ~4 chars/token, matching CharHeuristicCounter's documented approximation.
_CHARS_PER_TOKEN = 4

#: Hard floor: below this a "max window" would produce garbage segments.
_MIN_MAX_TOKENS = 32


class ChunkerCounter(TokenCounter):
    """TokenCounter over an embedder's exact tokenizer.

    Preferred when a :class:`~lummenna.force.contextual.embed.LocalEmbedder` is
    available: counts tokens with the vocabulary the embedder consumes, so a
    "448-token" segment really is 448 model tokens.

    Raises:
        ValueError: If the embedder exposes no usable tokenizer (e.g. the test
            MockEmbedder); :func:`semantic_chunks` then falls back to the char
            heuristic and logs a warning.
    """

    def __init__(self, embedder: object) -> None:
        tokenizer = getattr(embedder, "tokenizer", None)
        if tokenizer is None:
            raise ValueError("embedder exposes no tokenizer; use CharHeuristicCounter")
        self._tok = tokenizer

    def count(self, text: str) -> int:
        encoded = self._tok(text, add_special_tokens=False)
        ids = getattr(encoded, "input_ids", None)
        if ids is None and isinstance(encoded, dict):
            ids = encoded.get("input_ids")
        if ids is None:
            raise ValueError("tokenizer returned no input_ids")
        return len(ids)


#: Heading = a line of 1-6 ``#`` followed by text (ATX Markdown), allowing
#: trailing closing markers.
_HEADING_RE = re.compile(r"^#{1,6}\s+.+?#*\s*$")


def _is_heading(line: str) -> bool:
    return bool(_HEADING_RE.match(line.strip()))


def _split_paragraphs(text: str) -> list[str]:
    """Split on blank lines, keeping every non-empty paragraph verbatim."""
    paras: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "":
            if current:
                paras.append("\n".join(current).strip())
                current = []
        else:
            current.append(line)
    if current:
        paras.append("\n".join(current).strip())
    return paras


def _split_sentences(paragraph: str) -> list[str]:
    """Split on sentence enders followed by whitespace."""
    parts = re.split(r"(?<=[.!?])\s+", paragraph.strip())
    return [p for p in parts if p.strip()]


def _split_words(paragraph: str, max_tokens: int, counter: TokenCounter) -> list[str]:
    """Split on whitespace with counter-accurate budgets (no chars/token guess)."""
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for word in paragraph.split():
        word_tokens = counter.count(word)
        joiner = 1 if current else 0  # the space " ".join inserts
        if current and current_tokens + joiner + word_tokens > max_tokens:
            pieces.append(" ".join(current))
            current = [word]
            current_tokens = word_tokens
        else:
            current.append(word)
            current_tokens += joiner + word_tokens
    if current:
        pieces.append(" ".join(current))
    return pieces


def _truncate_to_budget(text: str, max_tokens: int, counter: TokenCounter) -> str:
    """Largest prefix of *text* fitting max_tokens (binary search on the prefix)."""
    if counter.count(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if counter.count(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip()


def _hard_split(paragraph: str, max_tokens: int, counter: TokenCounter) -> list[str]:
    """Split an oversized paragraph until every piece fits the token budget.

    Order: sentences → words → truncate. The final truncate handles a single
    unbreakable token blob (minified code, base64) that no boundary can split;
    truncation is honest (logged) where an oversized segment would silently
    shift the loss into the embedder's own truncation.
    """
    if counter.count(paragraph) <= max_tokens:
        return [paragraph]
    pieces: list[str] = []
    for sentence in _split_sentences(paragraph):
        if counter.count(sentence) <= max_tokens:
            pieces.append(sentence)
            continue
        for piece in _split_words(sentence, max_tokens, counter):
            if counter.count(piece) <= max_tokens:
                pieces.append(piece)
            else:
                logger.warning(
                    "chunker_unbreakable_token_truncated",
                    piece_chars=len(piece),
                    max_tokens=max_tokens,
                )
                pieces.append(_truncate_to_budget(piece, max_tokens, counter))
    return pieces


def _pack_units(units: list[str], max_tokens: int, counter: TokenCounter, joiner: str) -> list[str]:
    """Greedily pack units (paragraphs or split pieces) into segments.

    The joiner's own token cost is charged, or two units that fit individually
    would overflow once glued together.
    """
    joiner_tokens = counter.count(joiner)
    segments: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = counter.count(unit)
        cost = unit_tokens if not current else joiner_tokens + unit_tokens
        if current and current_tokens + cost > max_tokens:
            segments.append(joiner.join(current))
            current = [unit]
            current_tokens = unit_tokens
        else:
            current.append(unit)
            current_tokens += cost
    if current:
        segments.append(joiner.join(current))
    return segments


def _merge_undersized(
    segments: list[str], min_tokens: int, max_tokens: int, counter: TokenCounter
) -> list[str]:
    """Merge any segment below min_tokens into a neighbour when the result fits."""
    if len(segments) <= 1:
        return segments
    merged: list[str] = []
    pending: str | None = None
    joiner_tokens = counter.count("\n")
    for seg in segments:
        if pending is not None:
            candidate_tokens = counter.count(pending) + joiner_tokens + counter.count(seg)
            if candidate_tokens <= max_tokens:
                pending = f"{pending}\n{seg}"
                continue
            merged.append(pending)
            pending = seg
            continue
        if counter.count(seg) < min_tokens:
            pending = seg
        else:
            merged.append(seg)
    if pending is not None:
        if merged:
            tail_tokens = counter.count(merged[-1]) + joiner_tokens + counter.count(pending)
            if tail_tokens <= max_tokens:
                merged[-1] = f"{merged[-1]}\n{pending}"
                return merged
        merged.append(pending)
    return merged


def _overlap_tail(previous: str, overlap_tokens: int, counter: TokenCounter) -> str:
    """Largest word-aligned suffix of *previous* fitting overlap_tokens."""
    if overlap_tokens <= 0 or not previous:
        return ""
    tail_words: list[str] = []
    used = 0
    for word in reversed(previous.split()):
        word_tokens = counter.count(word)
        cost = word_tokens if not tail_words else 1 + word_tokens
        if used + cost > overlap_tokens:
            break
        tail_words.append(word)
        used += cost
    if not tail_words:
        return ""
    tail_words.reverse()
    return " ".join(tail_words)


def _resolve_counter(counter: TokenCounter | None, embedder: object | None) -> TokenCounter:
    """Prefer the embedder's tokenizer, fall back to an explicit counter, then chars."""
    if embedder is not None:
        try:
            return ChunkerCounter(embedder)
        except ValueError:
            logger.warning("chunker_no_tokenizer", effect="using char-heuristic counting")
    if counter is not None:
        return counter
    return CharHeuristicCounter()


def semantic_chunks(
    text: str,
    max_tokens: int = 448,
    min_tokens: int = 64,
    overlap_tokens: int = 48,
    counter: TokenCounter | None = None,
    embedder: object | None = None,
) -> list[str]:
    """Split *text* into semantically bounded segments of at most max_tokens.

    Headings and paragraph boundaries are respected where possible; any
    paragraph still exceeding the budget is hard-split on sentences, then words.
    Adjacent segments share up to *overlap_tokens* of trailing context so facts
    at a boundary stay retrievable.

    Args:
        text: The document to split.
        max_tokens: Upper bound on segment size in tokens. Default 448 leaves
            headroom inside the embedder's 512-token window for special tokens
            and tokenizer drift between the counter and the model.
        min_tokens: Segments smaller than this are merged into a neighbour.
        overlap_tokens: Trailing context carried into the next segment.
        counter: Explicit TokenCounter. Ignored when *embedder* is given.
        embedder: Preferred token source: its ``tokenizer`` counts with the
            embedder's exact vocabulary. Falls back to *counter*, then to a
            char-heuristic counter.

    Returns:
        Ordered list of segments; single-element for text that already fits.

    Raises:
        ValueError: If max_tokens < 32 or the bounds are inverted.
    """
    if max_tokens < _MIN_MAX_TOKENS:
        raise ValueError(f"max_tokens must be >= {_MIN_MAX_TOKENS}, got {max_tokens}")
    if min_tokens >= max_tokens:
        raise ValueError(f"min_tokens ({min_tokens}) must be < max_tokens ({max_tokens})")
    if overlap_tokens >= max_tokens:
        raise ValueError(f"overlap_tokens ({overlap_tokens}) must be < max_tokens ({max_tokens})")

    counter = _resolve_counter(counter, embedder)

    text = text.strip()
    if not text:
        return []
    # Fast path only when the text is ALSO structurally indivisible: a
    # document small enough to fit one segment but carrying two headings must
    # still split (the structural guarantee this module exists to make), so
    # the whole-text shortcut applies only when no internal heading boundary
    # exists. Paragraphs that fit are re-packed per section below, which
    # yields the same single segment in the plain case.
    if counter.count(text) <= max_tokens and len(_split_paragraphs(text)) == 1:
        return [text]

    # Phase 1: paragraphs grouped into (heading | None, blocks) sections. The
    # heading is tracked structurally during the split, never guessed back
    # from text shape afterwards.
    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_paras: list[str] = []
    for para in _split_paragraphs(text):
        first_line = para.split("\n", 1)[0]
        if _is_heading(first_line) and current_paras:
            sections.append((current_heading, current_paras))
            current_paras = []
        if _is_heading(first_line):
            current_heading = first_line.lstrip("#").strip()
            rest = para.split("\n", 1)[1] if "\n" in para else ""
            if rest.strip():
                current_paras.append(rest.strip())
            continue
        current_paras.append(para)
    if current_paras:
        sections.append((current_heading, current_paras))

    # Phase 2: pack each section into one or more segments. The section's
    # heading prefixes its first segment; the heading's token cost is charged
    # against the section budget, so a headed segment never exceeds max_tokens.
    segments: list[str] = []
    for heading, blocks in sections:
        heading_prefix = f"# {heading}\n\n" if heading else ""
        budget = max_tokens - (counter.count(heading_prefix) if heading_prefix else 0)
        budget = max(_MIN_MAX_TOKENS, budget)

        units: list[str] = []
        for para in blocks:
            if counter.count(para) <= budget:
                units.append(para)
            else:
                units.extend(
                    _pack_units(_hard_split(para, budget, counter), budget, counter, joiner=" ")
                )
        # Pack within THIS section only: a segment never spans two sections.
        # Cross-section boundaries are the structural guarantee this module
        # exists to make, so merging # One's tail into # Two's segment would
        # defeat the split in the common case where both sections fit alone.
        # (The old code deferred this pack to a single loop over all sections'
        # units, letting a full section bleed into the next one's segment.)
        packed_units = _pack_units(units, budget, counter, joiner="\n\n")
        if not packed_units:
            if heading_prefix:
                segments.append(heading_prefix.rstrip())
            continue
        for i, packed in enumerate(packed_units):
            segments.append(f"{heading_prefix}{packed}" if i == 0 else packed)

    # Phase 3: merge undersized segments into a neighbour when the result fits.
    merged = _merge_undersized(segments, min_tokens, max_tokens, counter)

    # Phase 4: overlap tails between consecutive segments.
    if overlap_tokens > 0 and len(merged) > 1:
        overlapped: list[str] = [merged[0]]
        for prev, cur in zip(merged, merged[1:], strict=False):
            tail = _overlap_tail(prev, overlap_tokens, counter)
            if not tail:
                overlapped.append(cur)
                continue
            candidate = f"… {tail}\n\n{cur}"
            if counter.count(candidate) <= max_tokens:
                overlapped.append(candidate)
            else:
                overlapped.append(cur)

        merged = overlapped

    return [s for s in merged if s]


def token_aware_chunks(
    text: str,
    embedder: object,
    max_tokens: int = 448,
    min_tokens: int = 64,
    overlap_tokens: int = 48,
) -> list[str]:
    """Convenience wrapper: :func:`semantic_chunks` with the embedder's tokenizer.

    Falls back to the char heuristic when the embedder exposes no tokenizer
    (logged at WARNING, never silently: counting units change).

    ``min_tokens``/``overlap_tokens`` are clamped to leave room below
    ``max_tokens`` rather than raising: callers of this wrapper pass target
    window sizes, and a hard error here would force every caller to repeat
    the same arithmetic. The validation error from :func:`semantic_chunks`
    still guards the explicit API.
    """
    return semantic_chunks(
        text,
        max_tokens=max_tokens,
        min_tokens=min(min_tokens, max_tokens // 2 - 1),
        overlap_tokens=min(overlap_tokens, max(0, max_tokens // 2 - 1)),
        embedder=embedder,
    )
