"""B1: Context Assembly Jinja Schema.

Input wire: Jinja2, retrieval results (C2), goal-tree (C6), TFC state (C7)
Output wire: LLM prompt string
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import BaseLoader, Environment

from lummenna.brand.errors import BudgetExceededError
from lummenna.config import LummennaConfig
from lummenna.controller import TFCState
from lummenna.force.contextual.token_budget import get_token_counter
from lummenna.fusion import RetrievedChunk
from lummenna.logging import get_console_logger

ASSEMBLY_TEMPLATE_STRING = """{{ system }}

{{ minimap }}

{{ goals }}

--- Retrieved Memories ---
{{ memories }}
--- End Memories ---

User: {{ query }}
Agent:
"""

# autoescape is intentionally disabled: this template renders a plain-text
# LLM prompt, never HTML/XML. Enabling autoescape would corrupt memory
# content (e.g. '&' -> '&amp;', quotes -> entities) before it reaches the
# model. No template output is ever served to a browser.
env = Environment(loader=BaseLoader(), autoescape=False)  # nosec B701
ASSEMBLY_TEMPLATE = env.from_string(ASSEMBLY_TEMPLATE_STRING)


def assemble_context(
    query: str,
    retrieved_chunks: list[RetrievedChunk],
    active_goals: list[str],
    tfc_state: TFCState,
    config: LummennaConfig,
    token_counter=None,
    system_prompt_override: str | None = None,
) -> str:
    """Build the working window and enforce the token budget.

    The returned string is guaranteed to count no more than
    ``config.context_budget`` tokens under *this* ``token_counter``. The budget
    is charged against the fully rendered prompt, including the template's own
    scaffolding, not merely against the concatenated chunk texts.

    Recovery strategy, in order:
    1. Cap any single chunk at 30 % of budget, truncating its content to the
       largest prefix that fits.
    2. Stop packing once the remaining budget is exhausted; lower-scoring
       chunks are dropped.
    3. If the highest-scoring chunk does not fit on its own, truncate it to the
       remaining budget rather than emitting an over-budget window.
    4. If not even the chunk header fits, raise ``BudgetExceededError``.

    Args:
        query: User query string.
        retrieved_chunks: Ranked chunks from the fusion engine.
        active_goals: List of active goal strings.
        tfc_state: Current Twin-Force Controller state.
        config: Runtime configuration (provides context_budget).
        token_counter: Object exposing ``count(str) -> int``. If ``None``, one
            is constructed from the configured embedding model.
        system_prompt_override: If provided, replaces the default system prompt.

    Returns:
        Assembled prompt string, within budget.

    Raises:
        BudgetExceededError: If the scaffolding alone, or the top chunk's
            header alone, cannot fit in ``config.context_budget``.
    """
    if token_counter is None:
        model_dir = Path(config.model_path) / config.embedding_model
        token_counter = get_token_counter(model_dir)

    budget_tokens = config.context_budget
    system_prompt = (
        system_prompt_override
        if system_prompt_override is not None
        else _build_system_prompt(tfc_state)
    )
    minimap = _build_minimap(retrieved_chunks)
    goal_block = _render_goal_tree(active_goals)

    def render(memories: str) -> str:
        rendered: str = ASSEMBLY_TEMPLATE.render(
            system=system_prompt,
            minimap=minimap,
            goals=goal_block,
            memories=memories,
            query=query,
            tfc=tfc_state,
        )
        return rendered

    # Charge the budget against the real rendered skeleton. Counting only
    # `system + minimap + goals + query` undercounts by every literal in the
    # template ("--- Retrieved Memories ---", "User: ", "Agent:", newlines),
    # which is how a window that measured as fitting could still overflow.
    skeleton_tokens = token_counter.count(render(""))
    remaining = budget_tokens - skeleton_tokens
    if remaining <= 0:
        raise BudgetExceededError(
            f"LCX-2001 BudgetExceeded: prompt scaffolding alone needs "
            f"{skeleton_tokens} tokens of a {budget_tokens}-token budget"
        )

    per_chunk_cap = max(1, int(budget_tokens * 0.3))

    packed: list[str] = []
    truncated_any = False
    ranked = sorted(retrieved_chunks, key=lambda x: x.final_score, reverse=True)
    for rc in ranked:
        chunk_text = _chunk_block(rc, rc.content)
        chunk_tokens = token_counter.count(chunk_text)

        # Cap any single chunk so one huge memory cannot monopolise the window.
        if chunk_tokens > per_chunk_cap:
            fitted = _fit_chunk_block(rc, per_chunk_cap, token_counter)
            if fitted is None:
                # Header alone exceeds the per-chunk cap; skip this chunk and
                # let a smaller one use the space.
                continue
            chunk_text = fitted
            chunk_tokens = token_counter.count(chunk_text)
            truncated_any = True

        if chunk_tokens > remaining:
            if packed:
                break
            # Top-ranked chunk does not fit even alone. Shrink it to the space
            # actually available instead of emitting an over-budget window.
            fitted = _fit_chunk_block(rc, remaining, token_counter)
            if fitted is None:
                raise BudgetExceededError(
                    f"LCX-2001 BudgetExceeded: chunk {rc.chunk_id} header does not fit "
                    f"in the {remaining} tokens left of a {budget_tokens}-token budget"
                )
            chunk_text = fitted
            chunk_tokens = token_counter.count(chunk_text)
            truncated_any = True
            packed.append(chunk_text)
            remaining -= chunk_tokens
            break

        packed.append(chunk_text)
        remaining -= chunk_tokens

    if retrieved_chunks and (len(packed) < len(retrieved_chunks) or truncated_any):
        _log_truncation(
            query,
            packed=len(packed),
            dropped=len(retrieved_chunks) - len(packed),
            truncated=truncated_any,
        )

    if not packed and retrieved_chunks:
        raise BudgetExceededError(
            f"LCX-2001 BudgetExceeded: no retrieved chunk fits in the {remaining} tokens "
            f"left of a {budget_tokens}-token budget"
        )

    return render("".join(packed))


def _chunk_block(rc: RetrievedChunk, content: str, suffix: str = "") -> str:
    """Render one memory block.

    ``chunk_id`` is part of the header so a caller reading the assembled window
    can attribute a memory back to its row and submit feedback on it; without it
    the assemble -> feedback loop cannot be closed.
    """
    return (
        f"[Chunk:{rc.chunk_id} Room:{rc.room_name} "
        f"Locus:{rc.locus_name} Prov:{rc.provenance_id}]\n"
        f"{content}{suffix}\n\n"
    )


def _fit_chunk_block(
    rc: RetrievedChunk,
    max_tokens: int,
    token_counter,
) -> str | None:
    """Largest prefix of ``rc.content`` whose rendered block fits ``max_tokens``.

    Uses the caller's token counter rather than a characters-per-token
    heuristic: a fixed 4:1 assumption understates CJK, base64 and minified code
    by up to 4x, which is how a "30 % of budget" cap could consume the whole
    window.

    Args:
        rc: The chunk to render.
        max_tokens: Inclusive upper bound on the rendered block's token count.
        token_counter: Object exposing ``count(str) -> int``.

    Returns:
        The rendered block, or ``None`` if the header alone exceeds
        ``max_tokens``.

    Complexity:
        O(log n) counter calls for content of length n; the bisection interval
        halves every iteration, so termination is bounded by ceil(log2(n)) + 1.
    """
    full = _chunk_block(rc, rc.content)
    if token_counter.count(full) <= max_tokens:
        return full

    marker = "\n[...truncated]"
    best = _chunk_block(rc, "", marker)
    if token_counter.count(best) > max_tokens:
        return None

    lo, hi = 0, len(rc.content)
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _chunk_block(rc, rc.content[:mid], marker)
        if token_counter.count(candidate) <= max_tokens:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _log_truncation(query: str, packed: int, dropped: int, truncated: bool) -> None:
    """Log context-window truncation events."""
    if logger is not None:
        logger.info(
            "context_truncated",
            query=query[:50],
            packed=packed,
            dropped=dropped,
            truncated=truncated,
        )


def _build_minimap(chunks: list[RetrievedChunk]) -> str:
    rooms = sorted({c.room_name for c in chunks})
    return "Palace minimap: " + " -> ".join(rooms) + "\n"


def _build_system_prompt(tfc: TFCState) -> str:
    personality = "builder" if tfc.e > 0.6 else "explorer"
    return (
        f"You are a sovereign agent with a memory palace. "
        f"Your mnemonic bias is {tfc.e:.2f} ({personality} mode). "
        f"Attend to retrieved memories with care."
    )


def _render_goal_tree(goals: list[str]) -> str:
    if not goals:
        return "Goals: (none)\n"
    return "Goals:\n" + "\n".join(f"  - {g}" for g in goals) + "\n"


logger = get_console_logger(__name__)
