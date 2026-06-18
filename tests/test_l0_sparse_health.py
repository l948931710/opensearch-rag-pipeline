"""Regression tests for L0 G3 sparse-health.

The original G3 did a "sparse-only self-query" by issuing a kNN query with a ZERO dense
vector plus the chunk's sparse vector. That method is regime-fragile: under InnerProduct a
zero dense vector scores 0 for EVERY doc, so the query ranks by a meaningless score
(observed score=-0.0) and returned a false 0/40 post-rebuild even though sparse was fully
healthy (probe-verified 2026-06-17: stored sparse_data present on every chunk + real hybrid
retrieval perfect; see scratch/probe_l0_sparse.py). It was replaced 2026-06-17 by reading
the STORED sparse_data directly. These tests lock in the replacement and prevent a revert.
"""
from __future__ import annotations

import inspect
import os as _os

# Importing the L0 layer triggers eval_harness.envboot.boot() at import time, which mutates
# global os.environ (forces RAG_SIMULATE=false + prod endpoints) for the LIVE harness.
# Snapshot + restore so the rest of the suite's env is unchanged; the helper under test is
# pure and never reads os.environ.
_SAVED_ENV = dict(_os.environ)
from eval_harness.layers import l0_index_health as L0  # noqa: E402  (boots envboot)
_os.environ.clear()
_os.environ.update(_SAVED_ENV)


def test_item_sparse_indices_present_top_level():
    item = {"id": "x", "sparse_data": {"indices": [15, 19, 760], "values": [0.1, 0.2, 0.3]}}
    assert L0._item_sparse_indices(item) == [15, 19, 760]


def test_item_sparse_indices_present_in_fields():
    item = {"fields": {"sparse_data": {"indices": [1, 2]}}}
    assert L0._item_sparse_indices(item) == [1, 2]


def test_item_sparse_indices_object_attr():
    class _SD:
        indices = [7, 8, 9]

    assert L0._item_sparse_indices({"sparse_data": _SD()}) == [7, 8, 9]


def test_item_sparse_indices_absent_is_empty():
    # Sparse vector NOT built => empty => G3 will (correctly) fail, signalling hybrid would
    # collapse to BM25. This is the real anomaly the gate must catch (vs the false 0/40).
    assert L0._item_sparse_indices({"id": "x"}) == []
    assert L0._item_sparse_indices({"sparse_data": {"indices": []}}) == []
    assert L0._item_sparse_indices({"sparse_data": None}) == []


def test_g3_does_not_reintroduce_zero_dense_sparse_only_query():
    """The zero-dense sparse-only self-query is forbidden: a zero dense vector scores 0 for
    every doc under InnerProduct, so it ranks garbage and reports a false 0/40. G3 must
    verify the sparse vector via the STORED sparse_data instead."""
    src = inspect.getsource(L0.run)
    assert "[0.0]" not in src, (
        "G3 reintroduced a zero dense vector — the zero-dense sparse-only query is "
        "regime-fragile (scores 0 under InnerProduct). Verify stored sparse_data instead."
    )
    assert "_item_sparse_indices" in src, (
        "G3 must verify the sparse vector via stored sparse_data (_item_sparse_indices)."
    )
