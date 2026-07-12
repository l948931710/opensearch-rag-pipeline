# -*- coding: utf-8 -*-
"""test_agent_runtime_approval.py — ApprovalOutcome 判别联合（Task 11 / 执行模型 §2）"""
import pytest
from pydantic import ValidationError

from opensearch_pipeline.agent_runtime.approval import (
    Approved,
    ApprovalOutcome,
    Edited,
    RejectedFeedback,
    RejectedTerminate,
    dump_outcome,
    parse_outcome,
)


def _all():
    return [Approved(), Edited(edited_args={"qty": 5}),
            RejectedFeedback(reason="口径不对"), RejectedTerminate()]


def test_kinds():
    assert [o.kind for o in _all()] == [
        "approved", "edited", "rejected_feedback", "rejected_terminate"]


def test_all_are_approval_outcome():
    for o in _all():
        assert isinstance(o, ApprovalOutcome)


def test_round_trip():
    for o in _all():
        restored = parse_outcome(dump_outcome(o))
        assert type(restored) is type(o) and restored == o


def test_edited_carries_args():
    assert dump_outcome(Edited(edited_args={"qty": 5}))["edited_args"] == {"qty": 5}


def test_frozen():
    with pytest.raises(ValidationError):
        Approved().kind = "x"


def test_unknown_kind_rejected():
    with pytest.raises(ValidationError):
        parse_outcome({"kind": "bogus"})
