"""Reference baselines every result is reported against.

A leaderboard of memory frameworks without baselines is unreadable: a recall of
0.61 means nothing until you know that plain BM25 over the same transcript gets
0.58. Most of the value a memory framework claims is the delta over trivial
retrieval, so the trivial retrievers are published in the same table.

Both baselines are dependency-light, fully deterministic, and implemented here
in the open, so any reader can check that they are not strawmen.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter

from benchmarks.leaderboard.adapter import (
    AdapterInfo,
    MemoryItem,
    Question,
    RetrievalResult,
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Okapi BM25 constants at their standard values (Robertson & Zaragoza, 2009).
_BM25_K1 = 1.5
_BM25_B = 0.75


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokenization. Deterministic and dependency-free."""
    return _TOKEN_RE.findall(text.lower())


class BM25Baseline:
    """Okapi BM25 over the flattened transcript.

    The floor any memory framework must clear. It has no notion of sessions,
    time, consolidation or forgetting; it just ranks by term overlap.
    """

    def __init__(self) -> None:
        self._item_ids: list[str] = []
        self._docs: list[list[str]] = []
        self._doc_freq: Counter[str] = Counter()
        self._avg_len: float = 0.0

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            framework="BM25 (baseline)",
            version="okapi-k1.5-b0.75",
            authored_by="lummenna",
            verified_by=None,
            config_summary="Okapi BM25, k1=1.5, b=0.75, flat transcript, no memory model",
            homepage=None,
        )

    def available(self) -> tuple[bool, str]:
        """Always runnable: pure standard library plus math."""
        return True, ""

    def reset(self) -> None:
        self._item_ids = []
        self._docs = []
        self._doc_freq = Counter()
        self._avg_len = 0.0

    def ingest(self, items: list[MemoryItem]) -> None:
        for item in items:
            tokens = _tokenize(item.text)
            self._item_ids.append(item.item_id)
            self._docs.append(tokens)
            self._doc_freq.update(set(tokens))
        total_len = sum(len(doc) for doc in self._docs)
        self._avg_len = total_len / len(self._docs) if self._docs else 0.0

    def retrieve(self, question: Question, top_k: int) -> RetrievalResult:
        """Score every document and return the top ``top_k``.

        Complexity:
            O(N * q) for N documents and q query terms. Exhaustive by design:
            an approximate baseline would understate the floor.
        """
        started = time.perf_counter()
        query_tokens = _tokenize(question.text)
        num_docs = len(self._docs)
        if num_docs == 0 or not query_tokens:
            return RetrievalResult([], (time.perf_counter() - started) * 1000.0)

        scored: list[tuple[float, str]] = []
        for index, doc in enumerate(self._docs):
            score = self._score(doc, query_tokens, num_docs)
            if score > 0.0:
                scored.append((score, self._item_ids[index]))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return RetrievalResult([item_id for _, item_id in scored[:top_k]], elapsed_ms)

    def _score(self, doc: list[str], query_tokens: list[str], num_docs: int) -> float:
        """Okapi BM25 score for one document."""
        if not doc:
            return 0.0
        counts = Counter(doc)
        doc_len = len(doc)
        score = 0.0
        for term in query_tokens:
            term_freq = counts.get(term)
            if not term_freq:
                continue
            doc_freq = self._doc_freq.get(term, 0)
            # Standard BM25 IDF with the +0.5 smoothing that keeps it positive.
            idf = math.log((num_docs - doc_freq + 0.5) / (doc_freq + 0.5) + 1.0)
            denominator = term_freq + _BM25_K1 * (
                1 - _BM25_B + _BM25_B * doc_len / (self._avg_len or 1.0)
            )
            score += idf * (term_freq * (_BM25_K1 + 1)) / denominator
        return score

    def teardown(self) -> None:
        self.reset()


class RecencyBaseline:
    """Return the most recent turns, ignoring the question entirely.

    This is the sliding-context-window strategy: keep the last N turns and hope
    the answer is in there. It exists to quantify how much of a framework's
    score is retrieval skill versus the benchmark simply favouring recent
    content, which several conversational datasets do.
    """

    def __init__(self) -> None:
        self._item_ids: list[str] = []

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            framework="Recency window (baseline)",
            version="last-k",
            authored_by="lummenna",
            verified_by=None,
            config_summary="Returns the last k ingested turns; ignores the query",
            homepage=None,
        )

    def available(self) -> tuple[bool, str]:
        return True, ""

    def reset(self) -> None:
        self._item_ids = []

    def ingest(self, items: list[MemoryItem]) -> None:
        self._item_ids.extend(item.item_id for item in items)

    def retrieve(self, question: Question, top_k: int) -> RetrievalResult:
        started = time.perf_counter()
        # Most recent first, mirroring how a context window is filled.
        recent = list(reversed(self._item_ids[-top_k:]))
        return RetrievalResult(recent, (time.perf_counter() - started) * 1000.0)

    def teardown(self) -> None:
        self.reset()
