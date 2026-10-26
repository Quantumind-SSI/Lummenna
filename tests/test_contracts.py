"""Grounding tests: documented claims must be enforced, not merely asserted.

Each test corresponds to a concrete public contract and proves the code refuses
to violate it.
"""

import math

import numpy as np
import pytest

from lummenna import contracts as c

# --- §3.1 / §3.4 enumerable claims -----------------------------------------


def test_source_types_match_documented_set():
    assert {
        "user_input",
        "agent_reasoning",
        "consolidation",
        "import",
        "p2p_share",
    } == c.SOURCE_TYPES


def test_room_types_match_documented_set():
    assert {"domain", "project", "person", "ephemeral"} == c.ROOM_TYPES


def test_intent_labels_match_documented_set():
    assert {"factual", "exploratory", "relational", "temporal"} == c.INTENT_LABELS


def test_resolution_chain_is_the_documented_ladder():
    # FP32 → FP16 → INT8 → BINARY → RELEASED
    order = ["FP32", "FP16", "INT8", "BINARY", "RELEASED"]
    for cur, nxt in zip(order[:-1], order[1:], strict=True):
        assert c.RESOLUTION_CHAIN[cur] == nxt
    assert "RELEASED" not in c.RESOLUTION_CHAIN  # terminal


def test_k_rrf_is_sixty():
    assert c.K_RRF == 60


def test_embedding_dims_is_384():
    assert c.EMBEDDING_DIMS == 384


# --- validators reject out-of-contract values -------------------------------


def test_validate_source_type_rejects_unknown():
    assert c.validate_source_type("user_input") == "user_input"
    with pytest.raises(ValueError):
        c.validate_source_type("hearsay")


def test_validate_intent_label_rejects_unknown():
    assert c.validate_intent_label("factual") == "factual"
    with pytest.raises(ValueError):
        c.validate_intent_label("smalltalk")


def test_validate_embedding_dims_rejects_mismatch():
    assert c.validate_embedding_dims(384) == 384
    with pytest.raises(ValueError):
        c.validate_embedding_dims(768)


# --- §3.4 V(m) reliability ordering + source_type grounding -----------------


def test_reliability_ordering_matches_whitepaper():
    from lummenna.force.mnemonic.value_model import RELIABILITY_BY_SOURCE as r

    assert (
        r["user_input"] > r["consolidation"] > r["agent_reasoning"] > r["import"] > r["p2p_share"]
    )


def test_extract_factors_rejects_unknown_source_type():
    from lummenna.force.mnemonic.value_model import extract_factors

    with pytest.raises(ValueError):
        extract_factors("hello", "made_up_source", [], [])


def test_compute_vm_is_bounded_unit_interval():
    from lummenna.force.mnemonic.value_model import compute_vm

    vm, _ = compute_vm("I must schedule a call", {}, "user_input")
    assert 0.0 <= vm <= 1.0


# --- §3.3 intent router grounding -------------------------------------------


def test_intent_router_returns_documented_label():
    from lummenna.intent import IntentRouter

    label = IntentRouter().classify("what is the capital of France")
    assert label in c.INTENT_LABELS


def test_intent_router_train_rejects_bad_labels():
    from lummenna.intent import IntentRouter

    X = np.random.randn(4, 8)
    with pytest.raises(ValueError):
        IntentRouter().train_lr(X, ["factual", "banter", "factual", "temporal"])


# --- §3.3 FRQAD distance bound ----------------------------------------------


def test_frqad_distance_within_zero_and_half_pi():
    from lummenna.sovereign.frqad import compute_frqad

    q = np.array([1.0, 0.0, 0.0])
    for cand in (
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([-1.0, 0.0, 0.0]),
    ):
        d = compute_frqad(q, cand)
        assert 0.0 <= d <= math.pi / 2 + 1e-9


# --- §3.2 TFC state + signal grounding --------------------------------------


def test_tfc_state_bounds_enforced():
    from pydantic import ValidationError

    from lummenna.controller import TFCState

    TFCState(e=0.0, a=1.0, tau=1.0, r=5)  # ok
    with pytest.raises(ValidationError):
        TFCState(e=1.5)
    with pytest.raises(ValidationError):
        TFCState(r=6)


def test_tfc_update_rejects_unknown_signal_key():
    from pydantic import ValidationError

    from lummenna.controller import TwinForceController

    with pytest.raises(ValidationError):
        TwinForceController().update({"novlety": 0.9})


# --- provenance confidence bound --------------------------------------------


def test_provenance_confidence_bounds(memory_db):
    from lummenna.force.mnemonic.provenance import create_provenance

    memory_db.execute("INSERT INTO room(name, room_type) VALUES ('r', 'domain')")
    room_id = memory_db.execute("SELECT room_id FROM room").fetchone()[0]
    memory_db.execute(
        "INSERT INTO chunk(room_id, content, content_hash) VALUES (?, 'x', 'h')",
        (room_id,),
    )
    chunk_id = memory_db.execute("SELECT chunk_id FROM chunk").fetchone()[0]

    # valid
    create_provenance(memory_db, chunk_id, "user_input", confidence=0.5)
    # out of range
    with pytest.raises(ValueError):
        create_provenance(memory_db, chunk_id, "user_input", confidence=1.5)
    # bad source_type
    with pytest.raises(ValueError):
        create_provenance(memory_db, chunk_id, "rumor", confidence=0.5)
