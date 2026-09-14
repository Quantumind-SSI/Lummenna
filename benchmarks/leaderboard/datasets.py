"""Dataset loaders for memory-framework evaluation.

Two datasets, chosen because they are what this class of system is actually
evaluated on, and because both are public so a third party can reproduce a run:

**LoCoMo** (Maharana et al., 2024) - very long multi-session dialogues with
questions spanning sessions. Tests cross-session recall and temporal reasoning.

**LongMemEval** (Wu et al., 2024) - chat histories with five question types,
including knowledge updates (a fact changes mid-history and the system must
return the current value) and abstention.

Both are fetched from HuggingFace at run time rather than vendored, matching the
existing BEIR harness. That is a deliberate scope boundary: benchmarks are a
developer-machine activity, so the network call never occurs inside the shipped
package or at any runtime path subject to ``assert_sovereign``.

No loader fabricates data. If a dataset cannot be fetched or its schema does not
match, the loader raises and the suite records NOT_RUN. A benchmark that
silently substitutes synthetic data for a named public dataset is worse than no
benchmark, because the label makes it look comparable when it is not.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from benchmarks.leaderboard.adapter import MemoryItem, Question


class DatasetUnavailableError(RuntimeError):
    """Raised when a dataset cannot be loaded in a usable form.

    Carries the reason through to the artifact so ``NOT_RUN`` is explained
    rather than mysterious.
    """


@dataclass(frozen=True)
class MemoryDataset:
    """A loaded evaluation set.

    Attributes:
        name: Canonical dataset name as published.
        version: Dataset revision/config identifier.
        items: Every memory item, in transcript order.
        questions: Questions to evaluate.
        source: Where this came from, for the artifact.
        fingerprint: SHA-256 over the normalised content. Two runs with the
            same fingerprint evaluated the same data, which is what makes
            independent results comparable.
    """

    name: str
    version: str
    items: list[MemoryItem]
    questions: list[Question]
    source: str
    fingerprint: str

    @property
    def answerable_questions(self) -> list[Question]:
        return [q for q in self.questions if not q.is_abstention]

    @property
    def abstention_questions(self) -> list[Question]:
        return [q for q in self.questions if q.is_abstention]

    def summary(self) -> dict[str, object]:
        """Return a JSON-serialisable description for the artifact."""
        categories: dict[str, int] = {}
        for question in self.questions:
            categories[question.category] = categories.get(question.category, 0) + 1
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "num_items": len(self.items),
            "num_questions": len(self.questions),
            "num_answerable": len(self.answerable_questions),
            "num_abstention": len(self.abstention_questions),
            "categories": dict(sorted(categories.items())),
        }


def compute_fingerprint(items: list[MemoryItem], questions: list[Question]) -> str:
    """Return a stable SHA-256 over dataset content.

    Hashes the fields that affect scoring only, so incidental metadata changes
    do not invalidate comparability while any change to text, ordering, or
    ground truth does.

    Args:
        items: Memory items in order.
        questions: Questions in order.

    Returns:
        64-character hex digest.

    Complexity:
        O(total text length).
    """
    digest = hashlib.sha256()
    for item in items:
        digest.update(item.item_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(item.text.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(item.session_id.encode("utf-8"))
        digest.update(b"\x01")
    for question in questions:
        digest.update(question.question_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(question.text.encode("utf-8"))
        digest.update(b"\x00")
        # Sorted so set iteration order cannot perturb the fingerprint.
        for answer_id in sorted(question.answer_item_ids):
            digest.update(answer_id.encode("utf-8"))
            digest.update(b"\x02")
        digest.update(b"\x01")
    return digest.hexdigest()


#: Canonical LoCoMo release. The paper authors distribute the data from their
#: GitHub repository; the HuggingFace entry under the same name holds only a
#: README, so this is the authoritative source rather than a mirror.
LOCOMO_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"

#: LongMemEval is published on the Hub as extensionless JSON files, one per
#: haystack size. ``oracle`` contains only evidence sessions (15 MB), ``s`` is
#: the standard small haystack (278 MB) that published results use.
LONGMEMEVAL_REPO = "xiaowu0162/longmemeval"
LONGMEMEVAL_VARIANTS = ("longmemeval_oracle", "longmemeval_s", "longmemeval_m")

#: Where downloaded source files are cached between runs.
_CACHE_DIR = Path(__file__).resolve().parent / ".cache"

_DOWNLOAD_TIMEOUT_SECONDS = 300


def _fetch_json_url(url: str, cache_name: str) -> object:
    """Download and cache a JSON document.

    Cached on first use so repeated runs and CI do not re-download, and so a
    run is reproducible offline once the cache is warm.

    Args:
        url: Source URL.
        cache_name: Filename to cache under.

    Returns:
        The parsed JSON document.

    Raises:
        DatasetUnavailableError: On any network or parse failure.
    """
    import urllib.error
    import urllib.request

    cached = _CACHE_DIR / cache_name
    if cached.is_file():
        try:
            return json.loads(cached.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt cache must not be fatal; fall through and refetch.
            cached.unlink(missing_ok=True)

    request = urllib.request.Request(url, headers={"User-Agent": "lummenna-leaderboard"})
    try:
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:  # nosec B310
            raw = response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DatasetUnavailableError(f"Could not download '{url}': {exc}") from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DatasetUnavailableError(f"'{url}' did not return valid JSON: {exc}") from exc

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(raw)
    return document


def _fetch_hub_file(repo: str, filename: str) -> object:
    """Download a single file from a HuggingFace dataset repo and parse it.

    Uses ``huggingface_hub`` directly rather than ``datasets.load_dataset``:
    these files carry no extension and no loader script, so the dataset builder
    cannot infer a format for them.

    Raises:
        DatasetUnavailableError: If the hub client is missing or the fetch or
            parse fails.
    """
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise DatasetUnavailableError(
            "huggingface_hub is required to fetch LongMemEval. "
            "Install it with: pip install -e '.[benchmarks]'"
        ) from exc

    try:
        path = hf_hub_download(repo, filename, repo_type="dataset")
    except Exception as exc:
        raise DatasetUnavailableError(
            f"Could not download '{filename}' from '{repo}': {exc}. "
            "This benchmark requires network access to the HuggingFace Hub."
        ) from exc

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetUnavailableError(f"'{filename}' is not valid JSON: {exc}") from exc


def _as_text(value: object) -> str:
    """Coerce a dataset field to text, rejecting empties."""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def load_locomo(max_questions: int | None = None, seed: int = 42) -> MemoryDataset:
    """Load the LoCoMo long-conversation benchmark.

    Args:
        max_questions: Cap on questions evaluated, applied after a seeded
            shuffle so a subset is representative rather than the first N.
            ``None`` evaluates all of them.
        seed: Shuffle seed, recorded in the artifact.

    Returns:
        A populated :class:`MemoryDataset`.

    Raises:
        DatasetUnavailableError: If the dataset cannot be fetched or its schema
            does not match what this loader understands.
    """
    raw = _fetch_json_url(LOCOMO_URL, "locomo10.json")
    if not isinstance(raw, list):
        raise DatasetUnavailableError(
            f"LoCoMo source returned {type(raw).__name__}, expected a list of conversations"
        )

    items: list[MemoryItem] = []
    questions: list[Question] = []
    text_to_item: dict[str, str] = {}

    for conversation_index, record in enumerate(raw):
        if not isinstance(record, dict):
            continue
        conversation = record.get("conversation")
        if not isinstance(conversation, dict):
            continue
        # `sample_id` is the dataset's own conversation key; prefer it so item
        # ids stay stable if upstream reorders the file.
        key = _as_text(record.get("sample_id")) or f"c{conversation_index}"
        _collect_locomo_turns(conversation, key, items, text_to_item)
        questions.extend(_collect_locomo_questions(record, key, text_to_item))

    if not items or not questions:
        raise DatasetUnavailableError(
            f"LoCoMo loaded from '{LOCOMO_URL}' but yielded "
            f"{len(items)} items and {len(questions)} questions; "
            "the upstream schema has changed and this loader needs updating."
        )

    questions = _subsample(questions, max_questions, seed)
    return MemoryDataset(
        name="locomo",
        version="locomo10",
        items=items,
        questions=questions,
        source=LOCOMO_URL,
        fingerprint=compute_fingerprint(items, questions),
    )


def _collect_locomo_turns(
    conversation: dict,
    conversation_key: str,
    items: list[MemoryItem],
    text_to_item: dict[str, str],
) -> None:
    """Flatten LoCoMo session turns into memory items.

    LoCoMo nests sessions as ``session_1``, ``session_2``, ... alongside
    ``session_N_date_time`` metadata strings, so session keys are detected by
    prefix and validated as lists rather than assumed.

    Turn ids come from the dataset's own ``dia_id`` (for example ``D1:3``),
    which is exactly what ``qa[].evidence`` references. Namespacing them by
    conversation keeps them unique across the ten conversations, which share a
    ``D<session>:<turn>`` numbering scheme.
    """
    session_keys = sorted(
        key
        for key in conversation
        if key.startswith("session_") and isinstance(conversation[key], list)
    )
    for session_key in session_keys:
        timestamp = _as_text(conversation.get(f"{session_key}_date_time")) or None
        for turn_index, turn in enumerate(conversation[session_key]):
            if not isinstance(turn, dict):
                continue
            text = _as_text(turn.get("text"))
            if not text:
                continue
            native_id = _as_text(turn.get("dia_id"))
            local_id = native_id or f"{session_key}:t{turn_index}"
            item_id = f"{conversation_key}:{local_id}"
            items.append(
                MemoryItem(
                    item_id=item_id,
                    text=text,
                    session_id=f"{conversation_key}:{session_key}",
                    speaker=_as_text(turn.get("speaker")) or None,
                    timestamp=timestamp,
                )
            )
            text_to_item.setdefault(text, item_id)


def _collect_locomo_questions(
    record: dict,
    conversation_key: str,
    text_to_item: dict[str, str],
) -> Iterator[Question]:
    """Yield LoCoMo questions whose evidence resolves to loaded items.

    Questions whose evidence cannot be resolved are skipped rather than scored
    against an empty ground truth, which would silently reclassify them as
    abstention questions and corrupt both columns.
    """
    qa_entries = record.get("qa")
    if not isinstance(qa_entries, list):
        return
    for question_index, entry in enumerate(qa_entries):
        if not isinstance(entry, dict):
            continue
        text = _as_text(entry.get("question"))
        if not text:
            continue
        evidence = entry.get("evidence")
        answer_ids: set[str] = set()
        if isinstance(evidence, list):
            for reference in evidence:
                reference_id = _as_text(reference)
                if reference_id:
                    answer_ids.add(f"{conversation_key}:{reference_id}")
        # `category` is an integer in the source file; 5 is the adversarial
        # (unanswerable) class, which is the abstention set.
        category = _as_text(entry.get("category")) or "unknown"
        is_adversarial = category == "5"
        if not answer_ids and not is_adversarial:
            continue
        yield Question(
            question_id=f"{conversation_key}:q{question_index}",
            text=text,
            answer_item_ids=frozenset() if is_adversarial else frozenset(answer_ids),
            category=f"locomo-cat-{category}",
        )


def load_longmemeval(
    max_questions: int | None = None,
    seed: int = 42,
    variant: str = "longmemeval_oracle",
) -> MemoryDataset:
    """Load the LongMemEval benchmark.

    Args:
        max_questions: Cap on evaluated questions, applied after a seeded
            shuffle. ``None`` evaluates all.
        seed: Shuffle seed, recorded in the artifact.
        variant: Which haystack size to load. ``longmemeval_oracle`` (15 MB)
            contains only evidence sessions and is the fast validation set;
            ``longmemeval_s`` (278 MB) is the standard haystack that published
            results use. Recorded in the artifact, because a score on oracle is
            not comparable to one on s.

    Returns:
        A populated :class:`MemoryDataset`.

    Raises:
        DatasetUnavailableError: If the dataset cannot be fetched or parsed.
        ValueError: If ``variant`` is not a known LongMemEval file.
    """
    if variant not in LONGMEMEVAL_VARIANTS:
        raise ValueError(
            f"unknown LongMemEval variant '{variant}'; "
            f"choose from {', '.join(LONGMEMEVAL_VARIANTS)}"
        )
    raw = _fetch_hub_file(LONGMEMEVAL_REPO, variant)
    if not isinstance(raw, list):
        raise DatasetUnavailableError(
            f"LongMemEval '{variant}' returned {type(raw).__name__}, expected a list"
        )

    items: list[MemoryItem] = []
    questions: list[Question] = []

    for record_index, record in enumerate(raw):
        if not isinstance(record, dict):
            continue
        question_text = _as_text(record.get("question"))
        if not question_text:
            continue
        answer_ids = _collect_longmemeval_sessions(record, record_index, items)
        category = _as_text(record.get("question_type")) or "unknown"
        # LongMemEval marks abstention questions with an `_abs` id suffix.
        question_id = _as_text(record.get("question_id")) or f"q{record_index}"
        if not answer_ids and not question_id.endswith("_abs"):
            continue
        questions.append(
            Question(
                question_id=question_id,
                text=question_text,
                answer_item_ids=frozenset(answer_ids),
                category=category,
            )
        )

    if not items or not questions:
        raise DatasetUnavailableError(
            f"LongMemEval '{variant}' loaded but yielded "
            f"{len(items)} items and {len(questions)} questions; "
            "the upstream schema has changed and this loader needs updating."
        )

    questions = _subsample(questions, max_questions, seed)
    return MemoryDataset(
        # Variant is part of the name: an oracle score and an s score are
        # different measurements and must never share a row.
        name=f"longmemeval:{variant.removeprefix('longmemeval_')}",
        version=variant,
        items=items,
        questions=questions,
        source=f"huggingface:{LONGMEMEVAL_REPO}/{variant}",
        fingerprint=compute_fingerprint(items, questions),
    )


def _collect_longmemeval_sessions(
    record: dict,
    record_index: int,
    items: list[MemoryItem],
) -> set[str]:
    """Flatten one LongMemEval haystack into items, returning evidence ids.

    Evidence is identified by the dataset's per-session ``has_answer`` flags,
    so the ground truth is the dataset's, never inferred by string matching.
    """
    sessions = record.get("haystack_sessions")
    if not isinstance(sessions, list):
        return set()
    session_ids = record.get("haystack_session_ids")
    answer_flags = record.get("answer_session_ids")
    answer_id_set = (
        {_as_text(value) for value in answer_flags if _as_text(value)}
        if isinstance(answer_flags, list)
        else set()
    )

    evidence: set[str] = set()
    for session_index, session in enumerate(sessions):
        if not isinstance(session, list):
            continue
        native_session_id = (
            _as_text(session_ids[session_index])
            if isinstance(session_ids, list) and session_index < len(session_ids)
            else ""
        )
        session_id = native_session_id or f"r{record_index}:s{session_index}"
        is_evidence_session = session_id in answer_id_set
        for turn_index, turn in enumerate(session):
            if not isinstance(turn, dict):
                continue
            text = _as_text(turn.get("content"))
            if not text:
                continue
            item_id = f"{session_id}:t{turn_index}"
            items.append(
                MemoryItem(
                    item_id=item_id,
                    text=text,
                    session_id=session_id,
                    speaker=_as_text(turn.get("role")) or None,
                    timestamp=None,
                )
            )
            # Turn-level flag when present, else the whole evidence session.
            if turn.get("has_answer") is True or (is_evidence_session and "has_answer" not in turn):
                evidence.add(item_id)
    return evidence


def _subsample(questions: list[Question], limit: int | None, seed: int) -> list[Question]:
    """Return a seeded, deterministic subsample of ``questions``.

    Shuffles before truncating so a subset spans categories instead of taking
    whichever type happens to be ordered first.

    Args:
        questions: All loaded questions.
        limit: Maximum to keep, or ``None`` for all.
        seed: Shuffle seed.

    Returns:
        Questions in a stable, seed-determined order.
    """
    if limit is None or limit >= len(questions):
        return questions
    if limit <= 0:
        raise ValueError(f"max_questions must be positive, got {limit}")
    import random

    shuffled = list(questions)
    random.Random(seed).shuffle(shuffled)
    return shuffled[:limit]


def load_fixture(path: Path) -> MemoryDataset:
    """Load a dataset from a local JSON fixture.

    Exists so the harness itself is testable offline and so a contributor can
    validate a new adapter without downloading gigabytes. Fixture results are
    clearly labelled and are never comparable to public-dataset results.

    Args:
        path: JSON file with ``items`` and ``questions`` arrays.

    Returns:
        A populated :class:`MemoryDataset` named ``fixture:<stem>``.

    Raises:
        DatasetUnavailableError: If the file is missing or malformed.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetUnavailableError(f"Cannot read fixture '{path}': {exc}") from exc
    if not isinstance(payload, dict):
        raise DatasetUnavailableError(f"Fixture '{path}' must contain a JSON object")

    raw_items = payload.get("items")
    raw_questions = payload.get("questions")
    if not isinstance(raw_items, list) or not isinstance(raw_questions, list):
        raise DatasetUnavailableError(
            f"Fixture '{path}' must contain 'items' and 'questions' arrays"
        )

    items = [
        MemoryItem(
            item_id=str(entry["item_id"]),
            text=str(entry["text"]),
            session_id=str(entry.get("session_id", "s0")),
            speaker=entry.get("speaker"),
            timestamp=entry.get("timestamp"),
        )
        for entry in raw_items
        if isinstance(entry, dict) and entry.get("item_id") and entry.get("text")
    ]
    questions = [
        Question(
            question_id=str(entry["question_id"]),
            text=str(entry["text"]),
            answer_item_ids=frozenset(str(value) for value in entry.get("answer_item_ids", [])),
            category=str(entry.get("category", "unknown")),
        )
        for entry in raw_questions
        if isinstance(entry, dict) and entry.get("question_id") and entry.get("text")
    ]
    if not items or not questions:
        raise DatasetUnavailableError(
            f"Fixture '{path}' yielded {len(items)} items and {len(questions)} questions"
        )
    return MemoryDataset(
        name=f"fixture:{path.stem}",
        version="local",
        items=items,
        questions=questions,
        source=f"file:{path.name}",
        fingerprint=compute_fingerprint(items, questions),
    )
