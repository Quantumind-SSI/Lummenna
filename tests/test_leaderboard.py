"""Tests for the memory-framework leaderboard harness.

The harness makes public claims about other people's software, so its own
correctness properties are load-bearing:

* A framework that cannot run is never scored as zero.
* A leaderboard artifact always carries its conflict-of-interest disclosure.
* Ground-truth leakage is detected, and non-determinism is not mistaken for it.
* Abstention is scored separately from recall.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.leaderboard.adapter import (
    AdapterInfo,
    MemoryAdapter,
    MemoryItem,
    Question,
    RetrievalResult,
)
from benchmarks.leaderboard.adapters.baselines import BM25Baseline, RecencyBaseline
from benchmarks.leaderboard.datasets import (
    DatasetUnavailableError,
    MemoryDataset,
    compute_fingerprint,
    load_fixture,
)
from benchmarks.leaderboard.report import render_markdown
from benchmarks.leaderboard.runner import (
    STATUS_ERROR,
    STATUS_NOT_RUN,
    STATUS_OK,
    run_adapter,
    run_leaderboard,
)

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "benchmarks"
    / "leaderboard"
    / "fixtures"
    / "smoke.json"
)


@pytest.fixture
def dataset() -> MemoryDataset:
    return load_fixture(FIXTURE)


class _StubAdapter:
    """Configurable adapter used to exercise runner behaviour."""

    def __init__(
        self,
        *,
        name: str = "Stub",
        runnable: bool = True,
        reason: str = "",
        raise_on_ingest: bool = False,
        reads_ground_truth: bool = False,
        nondeterministic: bool = False,
        returns: list[str] | None = None,
        verified_by: str | None = None,
        authored_by: str = "third-party",
    ) -> None:
        self._name = name
        self._runnable = runnable
        self._reason = reason
        self._raise_on_ingest = raise_on_ingest
        self._reads_ground_truth = reads_ground_truth
        self._nondeterministic = nondeterministic
        self._returns = returns if returns is not None else []
        self._verified_by = verified_by
        self._authored_by = authored_by
        self._calls = 0

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            framework=self._name,
            version="0.0.1",
            authored_by=self._authored_by,
            verified_by=self._verified_by,
            config_summary="stub",
        )

    def available(self) -> tuple[bool, str]:
        return (True, "") if self._runnable else (False, self._reason)

    def reset(self) -> None:
        self._calls = 0

    def ingest(self, items: list[MemoryItem]) -> None:
        if self._raise_on_ingest:
            raise RuntimeError("ingest exploded")

    def retrieve(self, question: Question, top_k: int) -> RetrievalResult:
        self._calls += 1
        if self._reads_ground_truth and question.answer_item_ids:
            return RetrievalResult(sorted(question.answer_item_ids)[:top_k], 0.1)
        if self._nondeterministic:
            # Alternate output on every call, regardless of input.
            return RetrievalResult([f"item-{self._calls}"], 0.1)
        return RetrievalResult(list(self._returns[:top_k]), 0.1)

    def teardown(self) -> None:
        pass


class TestProtocolConformance:
    @pytest.mark.parametrize("factory", [BM25Baseline, RecencyBaseline])
    def test_baselines_satisfy_the_protocol(self, factory):
        assert isinstance(factory(), MemoryAdapter)

    def test_lummenna_adapter_satisfies_the_protocol(self):
        from benchmarks.leaderboard.adapters.lummenna_adapter import LummennaAdapter

        assert isinstance(LummennaAdapter(), MemoryAdapter)

    def test_baselines_are_always_available(self):
        assert BM25Baseline().available() == (True, "")
        assert RecencyBaseline().available() == (True, "")


class TestFailureIsNeverScored:
    def test_unavailable_adapter_is_not_run_not_zero(self, dataset):
        adapter = _StubAdapter(runnable=False, reason="mem0 is not installed")

        run = run_adapter(adapter, dataset)

        assert run.status == STATUS_NOT_RUN
        assert run.reason == "mem0 is not installed"
        assert run.metrics == {}

    def test_raising_adapter_is_error_not_zero(self, dataset):
        run = run_adapter(_StubAdapter(raise_on_ingest=True), dataset)

        assert run.status == STATUS_ERROR
        assert "ingest exploded" in run.reason
        assert run.metrics == {}

    def test_one_broken_adapter_does_not_sink_the_run(self, dataset):
        payload = run_leaderboard(
            dataset,
            [BM25Baseline(), _StubAdapter(raise_on_ingest=True, name="Broken")],
        )

        assert payload["summary"]["adapters_scored"] == 1
        assert payload["summary"]["adapters_errored"] == 1

    def test_adapter_returning_nothing_scores_zero_recall_not_error(self, dataset):
        run = run_adapter(_StubAdapter(returns=[]), dataset)

        assert run.status == STATUS_OK
        assert run.metrics["recall_at_10"]["mean"] == 0.0


class TestIntegrityProbe:
    def test_ground_truth_leakage_is_detected(self, dataset):
        run = run_adapter(_StubAdapter(reads_ground_truth=True), dataset)

        assert run.integrity["leakage_detected"] is True
        assert run.integrity["leakage_mismatches"] > 0

    def test_honest_adapter_is_not_flagged(self, dataset):
        run = run_adapter(_StubAdapter(returns=["s1-t1", "s2-t1"]), dataset)

        assert run.integrity["leakage_detected"] is False
        assert run.integrity["deterministic"] is True

    def test_nondeterminism_is_not_reported_as_leakage(self, dataset):
        """A stateful system must not be accused of cheating.

        Lummenna reinforces items on access and legitimately returns different
        results for a repeated query; a naive two-call probe would flag it.
        """
        run = run_adapter(_StubAdapter(nondeterministic=True), dataset)

        assert run.integrity["deterministic"] is False
        # Undecidable, not cleared: reported as None rather than False.
        assert run.integrity["leakage_detected"] is None
        assert run.integrity["nondeterministic_questions"] > 0

    def test_baselines_are_deterministic(self, dataset):
        for factory in (BM25Baseline, RecencyBaseline):
            run = run_adapter(factory(), dataset)
            assert run.integrity["deterministic"] is True, factory.__name__
            assert run.integrity["leakage_detected"] is False


class TestDisclosure:
    def test_artifact_always_carries_the_conflict_disclosure(self, dataset):
        payload = run_leaderboard(dataset, [BM25Baseline()])
        disclosure = payload["disclosure"]

        assert disclosure["is_official"] is False
        assert "not independently audited" in disclosure["conflict_of_interest"]
        assert "official" in disclosure["official_status_note"].lower()

    def test_maintainer_authored_adapters_are_named(self, dataset):
        payload = run_leaderboard(dataset, [BM25Baseline()])

        assert "BM25 (baseline)" in payload["disclosure"]["adapters_authored_by_maintainer"]

    def test_third_party_adapter_is_not_marked_maintainer_authored(self, dataset):
        payload = run_leaderboard(dataset, [_StubAdapter(authored_by="mem0-team")])

        assert payload["disclosure"]["adapters_authored_by_maintainer"] == []

    def test_verified_adapter_is_tracked_separately(self, dataset):
        payload = run_leaderboard(
            dataset,
            [_StubAdapter(authored_by="zep-team", verified_by="zep-team")],
        )

        assert payload["disclosure"]["adapters_without_independent_verification"] == []

    def test_report_leads_with_the_disclosure(self, dataset):
        payload = run_leaderboard(dataset, [BM25Baseline()])
        markdown = render_markdown(payload, digest="abc123")

        assert markdown.index("not independently audited") < markdown.index("## Results")


class TestAbstentionScoring:
    def test_abstention_is_separate_from_recall(self, dataset):
        """Recall is undefined on unanswerable questions and must not absorb them."""
        run = run_adapter(_StubAdapter(returns=["s1-t1"]), dataset)

        assert run.metrics["recall_at_10"]["n"] == len(dataset.answerable_questions)
        assert run.metrics["abstention_accuracy"]["n"] == len(dataset.abstention_questions)

    def test_always_returning_results_scores_zero_abstention(self, dataset):
        run = run_adapter(_StubAdapter(returns=["s1-t1", "s2-t1"]), dataset)

        assert run.metrics["abstention_accuracy"]["mean"] == 0.0

    def test_returning_nothing_scores_perfect_abstention(self, dataset):
        run = run_adapter(_StubAdapter(returns=[]), dataset)

        assert run.metrics["abstention_accuracy"]["mean"] == 1.0

    def test_fixture_contains_both_question_kinds(self, dataset):
        assert dataset.answerable_questions
        assert dataset.abstention_questions


class TestDatasetIntegrity:
    def test_fingerprint_is_stable(self, dataset):
        assert compute_fingerprint(dataset.items, dataset.questions) == dataset.fingerprint

    def test_fingerprint_changes_when_ground_truth_changes(self, dataset):
        mutated = list(dataset.questions)
        original = mutated[0]
        mutated[0] = Question(
            question_id=original.question_id,
            text=original.text,
            answer_item_ids=frozenset({"different-item"}),
            category=original.category,
        )

        assert compute_fingerprint(dataset.items, mutated) != dataset.fingerprint

    def test_fingerprint_is_order_independent_for_answer_sets(self, dataset):
        """Set iteration order must not perturb the fingerprint."""
        first = compute_fingerprint(dataset.items, dataset.questions)
        rebuilt = [
            Question(
                question_id=q.question_id,
                text=q.text,
                answer_item_ids=frozenset(sorted(q.answer_item_ids, reverse=True)),
                category=q.category,
            )
            for q in dataset.questions
        ]

        assert compute_fingerprint(dataset.items, rebuilt) == first

    def test_missing_fixture_raises_rather_than_fabricating(self, tmp_path):
        with pytest.raises(DatasetUnavailableError):
            load_fixture(tmp_path / "does-not-exist.json")

    def test_malformed_fixture_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"items": [], "questions": []}))

        with pytest.raises(DatasetUnavailableError):
            load_fixture(path)

    def test_dataset_summary_is_json_serialisable(self, dataset):
        json.dumps(dataset.summary())


class TestBaselineBehaviour:
    def test_bm25_ranks_a_lexical_match_first(self):
        adapter = BM25Baseline()
        adapter.reset()
        adapter.ingest(
            [
                MemoryItem("a", "the cat sat on the mat", "s1"),
                MemoryItem("b", "quantum computing uses qubits", "s1"),
                MemoryItem("c", "my greyhound is called Pip", "s1"),
            ]
        )

        result = adapter.retrieve(Question("q", "what is my greyhound called", frozenset({"c"})), 3)

        assert result.item_ids[0] == "c"

    def test_recency_ignores_the_query(self):
        adapter = RecencyBaseline()
        adapter.reset()
        adapter.ingest([MemoryItem(f"i{n}", f"turn {n}", "s1") for n in range(5)])

        first = adapter.retrieve(Question("q1", "anything at all", frozenset({"i0"})), 2)
        second = adapter.retrieve(Question("q2", "something else entirely", frozenset({"i4"})), 2)

        assert first.item_ids == second.item_ids == ["i4", "i3"]

    def test_bm25_returns_nothing_for_a_zero_overlap_query(self):
        adapter = BM25Baseline()
        adapter.reset()
        adapter.ingest([MemoryItem("a", "alpha beta gamma", "s1")])

        assert adapter.retrieve(Question("q", "zzzz", frozenset({"a"})), 5).item_ids == []


class TestReportRendering:
    def test_not_run_adapters_appear_in_the_report(self, dataset):
        payload = run_leaderboard(
            dataset,
            [BM25Baseline(), _StubAdapter(name="Mem0", runnable=False, reason="no API key")],
        )
        markdown = render_markdown(payload)

        assert "## Not measured" in markdown
        assert "Mem0" in markdown
        assert "no API key" in markdown

    def test_report_states_interval_overlap_caveat(self, dataset):
        markdown = render_markdown(run_leaderboard(dataset, [BM25Baseline()]))

        assert "not statistically resolved" in markdown

    def test_report_flags_leakage(self, dataset):
        payload = run_leaderboard(dataset, [_StubAdapter(name="Cheater", reads_ground_truth=True)])
        markdown = render_markdown(payload)

        assert "Integrity warning" in markdown
        assert "Cheater" in markdown

    def test_report_flags_nondeterminism_without_calling_it_cheating(self, dataset):
        payload = run_leaderboard(dataset, [_StubAdapter(name="Stateful", nondeterministic=True)])
        markdown = render_markdown(payload)

        assert "Reproducibility note" in markdown
        assert "Integrity warning" not in markdown

    def test_report_handles_a_run_with_no_scores(self, dataset):
        payload = run_leaderboard(dataset, [_StubAdapter(runnable=False, reason="unavailable")])
        markdown = render_markdown(payload)

        assert "No framework produced a score" in markdown

    def test_unverified_adapters_are_flagged_inline(self, dataset):
        markdown = render_markdown(run_leaderboard(dataset, [BM25Baseline()]))

        assert "unverified" in markdown


class TestRunnerValidation:
    def test_empty_adapter_list_is_rejected(self, dataset):
        with pytest.raises(ValueError, match="at least one adapter"):
            run_leaderboard(dataset, [])

    def test_non_positive_top_k_is_rejected(self, dataset):
        with pytest.raises(ValueError, match="top_k"):
            run_leaderboard(dataset, [BM25Baseline()], top_k=0)

    def test_payload_is_json_serialisable(self, dataset):
        json.dumps(run_leaderboard(dataset, [BM25Baseline(), RecencyBaseline()]))

    def test_ingest_and_query_are_timed_separately(self, dataset):
        run = run_adapter(BM25Baseline(), dataset)

        assert "ingest_seconds" in run.timings
        assert "query_seconds_total" in run.timings
        assert run.timings["query_latency_ms"] is not None
