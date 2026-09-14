"""Regression tests for context-window budget enforcement.

``assemble_context`` promises a window within ``config.context_budget``. These
tests pin that promise against the three ways it was previously broken:

1. The emergency path emitted the top chunk whole, over budget, without raising.
2. Oversized chunks were truncated with a fixed 4-chars-per-token heuristic,
   which understates dense text (CJK, base64, minified code) by up to 4x.
3. The template's own scaffolding was never charged against the budget.
"""

import pytest

from lummenna.brand.errors import BudgetExceededError
from lummenna.config import LummennaConfig
from lummenna.controller import TFCState
from lummenna.force.contextual.assembly import assemble_context
from lummenna.fusion import RetrievedChunk


class CharCounter:
    """Stand-in for the 4:1 character heuristic."""

    def count(self, text: str) -> int:
        return max(1, len(text) // 4)


class DenseCounter:
    """Worst-case real tokenizer: one token per character.

    CJK, base64 and minified source all approach this ratio, which is precisely
    where a hard-coded 4:1 truncation assumption fails.
    """

    def count(self, text: str) -> int:
        return max(1, len(text))


def make_chunk(chunk_id: int, size: int, fill: str = "X", score: float | None = None):
    return RetrievedChunk(
        chunk_id=chunk_id,
        room_name="room",
        locus_name="locus",
        content=fill * size,
        provenance_id=chunk_id,
        rrf_score=0.1,
        vm_score=0.5,
        frqad_score=0.5,
        recency_hours=0.0,
        final_score=score if score is not None else 1.0 / (chunk_id + 1),
    )


@pytest.fixture
def tfc():
    return TFCState()


class TestBudgetIsNeverExceeded:
    @pytest.mark.parametrize(
        ("counter", "budget"),
        [
            (CharCounter(), 100),
            (CharCounter(), 512),
            (DenseCounter(), 2048),
            (DenseCounter(), 600),
        ],
    )
    def test_oversized_top_chunk_stays_within_budget(self, tfc, counter, budget):
        """The emergency path must truncate, not emit an over-budget window."""
        config = LummennaConfig(context_budget=budget)
        chunks = [make_chunk(0, 50_000), make_chunk(1, 50)]

        out = assemble_context("query", chunks, [], tfc, config, token_counter=counter)

        assert counter.count(out) <= budget

    def test_single_oversized_chunk_is_truncated_not_rejected(self, tfc):
        config = LummennaConfig(context_budget=512)
        counter = CharCounter()

        out = assemble_context(
            "query", [make_chunk(0, 40_000)], [], tfc, config, token_counter=counter
        )

        assert counter.count(out) <= 512
        assert "[...truncated]" in out

    def test_many_chunks_stay_within_budget(self, tfc):
        config = LummennaConfig(context_budget=300)
        counter = CharCounter()
        chunks = [make_chunk(i, 400) for i in range(20)]

        out = assemble_context("query", chunks, [], tfc, config, token_counter=counter)

        assert counter.count(out) <= 300

    def test_template_scaffolding_is_charged_against_budget(self, tfc):
        """The rendered prompt, not just the chunk texts, must fit.

        Counting only ``system + minimap + goals + query`` omits every literal
        in the template, so a window could measure as fitting and still
        overflow once rendered.
        """
        config = LummennaConfig(context_budget=120)
        counter = CharCounter()
        chunks = [make_chunk(i, 80) for i in range(6)]

        out = assemble_context("query", chunks, [], tfc, config, token_counter=counter)

        assert counter.count(out) <= 120
        assert "--- Retrieved Memories ---" in out


class TestBudgetExceeded:
    def test_scaffolding_alone_over_budget_raises(self, tfc):
        config = LummennaConfig(context_budget=5)

        with pytest.raises(BudgetExceededError, match="scaffolding"):
            assemble_context(
                "query", [make_chunk(0, 10)], [], tfc, config, token_counter=CharCounter()
            )

    def test_error_message_names_the_budget(self, tfc):
        config = LummennaConfig(context_budget=5)

        with pytest.raises(BudgetExceededError) as excinfo:
            assemble_context(
                "query", [make_chunk(0, 10)], [], tfc, config, token_counter=CharCounter()
            )

        assert "LCX-2001" in str(excinfo.value)
        assert "5-token budget" in str(excinfo.value)

    def test_empty_results_never_raise(self, tfc):
        config = LummennaConfig(context_budget=2048)

        out = assemble_context("query", [], [], tfc, config, token_counter=CharCounter())

        assert "query" in out


class TestChunkAttribution:
    def test_header_carries_chunk_id(self, tfc):
        """Without chunk_id the assemble to feedback loop cannot be closed."""
        config = LummennaConfig(context_budget=2048)

        out = assemble_context(
            "query", [make_chunk(77, 20)], [], tfc, config, token_counter=CharCounter()
        )

        assert "[Chunk:77 Room:room Locus:locus Prov:77]" in out

    def test_truncated_chunks_keep_their_header(self, tfc):
        config = LummennaConfig(context_budget=256)

        out = assemble_context(
            "query", [make_chunk(42, 40_000)], [], tfc, config, token_counter=CharCounter()
        )

        assert "[Chunk:42" in out

    def test_chunks_are_packed_in_score_order(self, tfc):
        config = LummennaConfig(context_budget=2048)
        low = make_chunk(1, 20, fill="a", score=0.1)
        high = make_chunk(2, 20, fill="b", score=0.9)

        out = assemble_context("query", [low, high], [], tfc, config, token_counter=CharCounter())

        assert out.index("[Chunk:2") < out.index("[Chunk:1")


class TestTruncationFitting:
    def test_truncation_uses_the_supplied_counter(self, tfc):
        """A dense tokenizer must shrink content far more than a 4:1 heuristic.

        Under the old fixed heuristic both counters produced the same character
        cut, which is how a "30 % of budget" cap consumed the whole window.
        """
        config = LummennaConfig(context_budget=1024)
        chunks = [make_chunk(0, 20_000)]

        char_out = assemble_context("q", chunks, [], tfc, config, token_counter=CharCounter())
        dense_out = assemble_context("q", chunks, [], tfc, config, token_counter=DenseCounter())

        assert len(dense_out) < len(char_out)
        assert CharCounter().count(char_out) <= 1024
        assert DenseCounter().count(dense_out) <= 1024

    def test_truncation_keeps_a_content_prefix(self, tfc):
        config = LummennaConfig(context_budget=512)
        chunk = make_chunk(0, 10_000, fill="Z")

        out = assemble_context("q", [chunk], [], tfc, config, token_counter=CharCounter())

        assert "ZZZ" in out
        assert "[...truncated]" in out
