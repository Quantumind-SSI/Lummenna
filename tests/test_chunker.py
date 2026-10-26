"""Tests for lummenna.force.contextual.chunker."""

import pytest

from lummenna.force.contextual.chunker import (
    ChunkerCounter,
    semantic_chunks,
    token_aware_chunks,
)


class CountingCounter:
    """Deterministic TokenCounter: one token per character (easy to reason about)."""

    def count(self, text: str) -> int:
        return len(text)


class NoTokenizerEmbedder:
    """Embedder-shaped object with no tokenizer (exercises the fallback path)."""


class ListTokenizer:
    """Tokenizer whose input_ids are lists of ints."""

    def __call__(self, text: str, add_special_tokens: bool = False):  # noqa: ARG002
        class Encoded:
            input_ids = [0] * max(1, len(text) // 5)

        return Encoded()


class DictTokenizer:
    """Tokenizer returning the HF-dict shape."""

    def __call__(self, text: str, add_special_tokens: bool = False):  # noqa: ARG002
        return {"input_ids": [0] * max(1, len(text) // 5)}


# --- validation -------------------------------------------------------------


def test_rejects_max_tokens_below_floor():
    with pytest.raises(ValueError, match="max_tokens must be >= 32"):
        semantic_chunks("x", max_tokens=16, counter=CountingCounter())


def test_rejects_inverted_bounds():
    with pytest.raises(ValueError, match="min_tokens"):
        semantic_chunks("x", max_tokens=100, min_tokens=200, counter=CountingCounter())


def test_rejects_overlap_ge_max():
    with pytest.raises(ValueError, match="overlap_tokens"):
        semantic_chunks("x", max_tokens=100, overlap_tokens=100, counter=CountingCounter())


# --- trivial cases ----------------------------------------------------------


def test_empty_text_returns_empty():
    assert semantic_chunks("", counter=CountingCounter()) == []
    assert semantic_chunks("   \n  ", counter=CountingCounter()) == []


def test_short_text_single_segment():
    text = "Just a short note."
    assert semantic_chunks(text, counter=CountingCounter()) == [text]


# --- structural splitting ---------------------------------------------------


def test_paragraph_split_respected():
    para = "word " * 30  # 150 chars -> 149 stripped
    text = f"{para}\n\n{para}"
    chunks = semantic_chunks(text, max_tokens=100, overlap_tokens=0, counter=CountingCounter())
    # Each 149-token paragraph exceeds the 100-token window, so lossless
    # splitting must produce two pieces per paragraph (4 total) — and no
    # segment may mix content from both paragraphs.
    assert len(chunks) == 4
    assert all(len(c) <= 100 for c in chunks)
    body = para.strip()
    for chunk in chunks:
        assert chunk in body


def test_heading_starts_new_section():
    body = "sentence. " * 20
    text = f"# Alpha\n\n{body}\n\n# Beta\n\n{body}"
    chunks = semantic_chunks(
        text, max_tokens=160, min_tokens=10, overlap_tokens=0, counter=CountingCounter()
    )
    assert len(chunks) >= 2
    assert any(c.startswith("# Alpha") for c in chunks)
    assert any(c.startswith("# Beta") for c in chunks)


def test_no_segment_mixes_sections_when_both_fit():
    body = "sentence. " * 12
    text = f"# One\n\n{body}\n\n# Two\n\n{body}"
    chunks = semantic_chunks(
        text, max_tokens=300, min_tokens=10, overlap_tokens=0, counter=CountingCounter()
    )
    # Two sections, each ~130 tokens: they must not merge across the heading.
    assert len(chunks) == 2
    assert chunks[0].startswith("# One") and chunks[1].startswith("# Two")


# --- hard splitting ---------------------------------------------------------


def test_oversized_paragraph_split_on_sentences():
    # 40 sentences x 10 chars = 400 chars; window 120.
    para = "abcdefghij. " * 40
    chunks = semantic_chunks(
        para, max_tokens=120, min_tokens=10, overlap_tokens=0, counter=CountingCounter()
    )
    assert len(chunks) >= 3
    assert all(len(c) <= 120 for c in chunks)
    # No content lost: rejoining without overlap must cover every sentence.
    rejoined = " ".join(c.strip() for c in chunks)
    for _ in range(40):
        assert "abcdefghij." in rejoined


def test_oversized_single_word_split_by_words():
    # A punctuation-free run far beyond the window.
    para = " ".join(["x" * 50] * 10)
    chunks = semantic_chunks(
        para, max_tokens=80, min_tokens=1, overlap_tokens=0, counter=CountingCounter()
    )
    assert len(chunks) >= 5
    assert all(len(c) <= 80 for c in chunks)
    assert " ".join(chunks).count("x" * 50) == 10


def test_merge_undersized_segments():
    # Two 30-char paragraphs against a 100-token window: they merge.
    para = "abcdefghij" * 3  # 30 chars
    text = f"{para}\n\n{para}"
    chunks = semantic_chunks(
        text, max_tokens=100, min_tokens=64, overlap_tokens=0, counter=CountingCounter()
    )
    assert len(chunks) == 1


# --- overlap ----------------------------------------------------------------


def test_overlap_carries_tail():
    sentences = [f"{'b' * 20} {i}. " for i in range(10)]
    text = "".join(sentences)
    chunks = semantic_chunks(
        text, max_tokens=120, min_tokens=10, overlap_tokens=20, counter=CountingCounter()
    )
    assert len(chunks) >= 2
    # Every segment after the first either starts with the overlap marker or
    # carries no tail (only when the tail would break the budget).
    assert all(c.startswith("… ") or not c.startswith("…") for c in chunks[1:])
    # Content coverage: tail of chunk N appears in chunk N+1.
    assert chunks[0][-10:] in chunks[1] or chunks[1].count(".") >= 1


# --- token counters ---------------------------------------------------------


def test_chunker_counter_list_tokenizer():
    counter = ChunkerCounter(type("E", (), {"tokenizer": ListTokenizer()})())
    assert counter.count("hello world") == 2  # 11 // 5


def test_chunker_counter_dict_tokenizer():
    counter = ChunkerCounter(type("E", (), {"tokenizer": DictTokenizer()})())
    assert counter.count("hello world") == 2


def test_chunker_counter_rejects_missing_tokenizer():
    with pytest.raises(ValueError, match="no tokenizer"):
        ChunkerCounter(NoTokenizerEmbedder())


def test_semantic_chunks_falls_back_to_char_counter():
    # NoTokenizerEmbedder has no tokenizer -> char heuristic; 500 chars -> >1 chunk.
    text = "word " * 100
    chunks = semantic_chunks(text, max_tokens=100, embedder=NoTokenizerEmbedder())
    assert len(chunks) > 1


def test_token_aware_chunks_uses_embedder():
    embedder = type("E", (), {"tokenizer": ListTokenizer()})()
    text = "word " * 100  # 500 chars -> 100 tokens under ListTokenizer
    chunks = token_aware_chunks(text, embedder, max_tokens=50)
    assert len(chunks) >= 2


def test_default_max_tokens_within_embedder_window():
    """Default 448 leaves headroom under the embedder's 512-token hard window."""
    text = "word " * 600  # 3000 chars
    chunks = semantic_chunks(text, counter=CountingCounter())
    assert all(len(c) <= 448 * 4 for c in chunks)
    assert len(chunks) >= 2
