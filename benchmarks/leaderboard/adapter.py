"""The contract a memory framework must satisfy to appear on the leaderboard.

Why a contract instead of in-tree implementations of every competitor:

The standard failure of cross-framework benchmarks is that the publisher
implements everyone else's system and gets it subtly wrong, usually in the
direction that flatters the publisher. The resulting table is unfalsifiable
noise, and the first response from any ranked vendor is "you configured mine
incorrectly", which is usually correct.

This harness inverts that. The protocol below is narrow, fully specified, and
framework-agnostic. Lummenna ships an adapter for itself and two baselines that
anyone can verify. Every other adapter is expected to be contributed and
verified by people who actually know that system, and each result records who
authored and who verified its adapter, so a reader can weigh the provenance
themselves.

Fairness rules enforced by the runner, not by trust:
    * Ingestion and query are timed separately. A framework that pre-computes
      aggressively pays for it in ingest, not silently in nobody's column.
    * Every adapter sees identical sessions in identical order with the same
      seed. The runner, never the adapter, controls iteration.
    * ``retrieve`` returns evidence ids only. No adapter is asked to generate
      an answer, so no result is contaminated by a differing generator LLM.
    * An adapter that cannot run declares that itself via :meth:`available`,
      and the runner records ``NOT_RUN`` rather than inventing a zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class MemoryItem:
    """One unit of evidence a framework must be able to store and return.

    Attributes:
        item_id: Stable identifier. Ground truth references these, so an
            adapter MUST return this exact string, not a paraphrase or an
            internal id.
        text: The content to remember.
        session_id: Conversation/session this belongs to. Memory benchmarks
            test cross-session recall, so this is never collapsed.
        speaker: Who produced the text, when the dataset distinguishes.
        timestamp: Dataset-provided ordering key. Some questions are explicitly
            temporal, so adapters that model time may use it. ``None`` when the
            dataset does not supply one.
    """

    item_id: str
    text: str
    session_id: str
    speaker: str | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class Question:
    """A query with its ground-truth evidence.

    Attributes:
        question_id: Stable identifier for reporting.
        text: The question as posed to the memory system.
        answer_item_ids: Item ids that constitute correct evidence. Empty means
            the question is unanswerable from the transcript and the correct
            behaviour is abstention.
        category: Dataset-provided question type (for example
            ``"multi-session"``, ``"temporal-reasoning"``,
            ``"knowledge-update"``). Used for per-category breakdown, which is
            where memory systems actually differentiate.
    """

    question_id: str
    text: str
    answer_item_ids: frozenset[str]
    category: str = "unknown"

    @property
    def is_abstention(self) -> bool:
        """True when the correct behaviour is to return nothing."""
        return not self.answer_item_ids


@dataclass(frozen=True)
class AdapterInfo:
    """Provenance for an adapter implementation.

    This is published beside every score. An adapter written by the leaderboard
    author for a competing framework is a weaker claim than one written by that
    framework's maintainers, and the table must say so rather than hide it.

    Attributes:
        framework: Framework name as its authors spell it.
        version: Installed version of the framework, or ``None`` if unknown.
        authored_by: Who wrote this adapter.
        verified_by: Who reviewed it against the framework's documented API.
            ``None`` means unverified, which the report renders explicitly.
        config_summary: Human-readable description of the configuration used,
            including any non-default settings.
        homepage: Project URL, for readers who want to check the adapter.
    """

    framework: str
    version: str | None
    authored_by: str
    verified_by: str | None = None
    config_summary: str = ""
    homepage: str | None = None

    @property
    def is_self_authored(self) -> bool:
        """True when the adapter was written by the leaderboard maintainer.

        Results from self-authored adapters for *other* frameworks carry a
        conflict-of-interest marker in the report.
        """
        return self.authored_by.strip().lower() in _MAINTAINER_ALIASES


#: Names that identify the leaderboard maintainer for conflict-of-interest
#: marking. Lowercase, compared after stripping.
_MAINTAINER_ALIASES = frozenset({"lummenna", "quantumindssi", "lummenna-maintainers"})


@dataclass
class RetrievalResult:
    """What an adapter returns for one question.

    Attributes:
        item_ids: Retrieved evidence ids, most relevant first. An empty list is
            a valid and sometimes correct answer (abstention).
        latency_ms: Wall-clock time for this retrieval, measured by the
            adapter around its own call so framework-internal batching is
            attributed correctly.
    """

    item_ids: list[str] = field(default_factory=list)
    latency_ms: float = 0.0


@runtime_checkable
class MemoryAdapter(Protocol):
    """Minimal interface a framework must expose to be measured.

    Lifecycle, driven entirely by the runner:
        1. :meth:`available` is consulted. False means the run records
           ``NOT_RUN`` with the stated reason.
        2. :meth:`reset` establishes a clean store.
        3. :meth:`ingest` is called once with every item, in dataset order.
        4. :meth:`retrieve` is called once per question.
        5. :meth:`teardown` releases resources.
    """

    def info(self) -> AdapterInfo:
        """Return provenance for this adapter. Must not raise."""
        ...

    def available(self) -> tuple[bool, str]:
        """Report whether this adapter can run here.

        Returns:
            ``(True, "")`` when runnable, otherwise ``(False, reason)`` where
            reason names the missing dependency, credential or model. The
            reason is published, so "not installed" is more useful than "no".
        """
        ...

    def reset(self) -> None:
        """Discard all state so the next ingest starts from empty."""
        ...

    def ingest(self, items: list[MemoryItem]) -> None:
        """Store every item. Called once, timed as a whole."""
        ...

    def retrieve(self, question: Question, top_k: int) -> RetrievalResult:
        """Return up to ``top_k`` evidence ids for ``question``.

        Implementations MUST NOT consult ``question.answer_item_ids``. The
        runner passes the full :class:`Question` for category-aware systems,
        and verifies non-cheating by re-running a sample with the ground truth
        stripped.
        """
        ...

    def teardown(self) -> None:
        """Release resources. Must be safe to call after a failed ingest."""
        ...
