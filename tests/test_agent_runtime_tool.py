# -*- coding: utf-8 -*-
"""
test_agent_runtime_tool.py — EnterpriseTool 契约（Task 5 / 报告 §4/§5）

覆盖：ToolSpec 注册期校验（schema 合法性 / 写型幂等强制 / 名版非空）· frozen 不可变 ·
入参 schema 校验与越权参数拒绝 · ToolResult/ContentBlock 构造 · EnterpriseTool Protocol。
"""
from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from opensearch_pipeline.agent_runtime.tool import (
    ContentBlock,
    EnterpriseTool,
    RiskLevel,
    ToolArgsError,
    ToolResult,
    ToolSpec,
    ToolSpecError,
)

_QUERY_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}
_OUT_SCHEMA = {"type": "object"}


def _spec(**over):
    base = dict(
        name="knowledge_search", version="1.0.0", description="检索知识库",
        input_schema=_QUERY_SCHEMA, output_schema=_OUT_SCHEMA,
        risk_level=RiskLevel.READ_ONLY, permission_scope="kb.search",
        data_classification="internal",
    )
    base.update(over)
    return ToolSpec(**base)


# ── ToolSpec 注册期校验 ─────────────────────────────────────────
def test_valid_spec_constructs():
    spec = _spec()
    assert spec.qualified_name == "knowledge_search@1.0.0"
    assert spec.risk_level is RiskLevel.READ_ONLY


def test_empty_name_or_version_rejected():
    with pytest.raises(ToolSpecError):
        _spec(name="")
    with pytest.raises(ToolSpecError):
        _spec(version="")


def test_invalid_json_schema_rejected():
    with pytest.raises(ToolSpecError):
        _spec(input_schema={"type": "not-a-real-type"})
    with pytest.raises(ToolSpecError):
        _spec(output_schema="not-a-dict")


def test_write_tool_requires_idempotency_key():
    # 写型工具缺 key_required → 拒绝
    with pytest.raises(ToolSpecError):
        _spec(name="u8_writeback", risk_level=RiskLevel.HIGH_WRITE, permission_scope="u8.writeback")
    with pytest.raises(ToolSpecError):
        _spec(risk_level=RiskLevel.LOW_WRITE)
    # 带 key_required → 通过
    ok = _spec(name="u8_writeback", risk_level=RiskLevel.HIGH_WRITE,
               permission_scope="u8.writeback", idempotency="key_required",
               side_effects=True, approval_policy="always")
    assert ok.idempotency == "key_required"


def test_spec_is_frozen():
    spec = _spec()
    with pytest.raises(FrozenInstanceError):
        spec.risk_level = RiskLevel.HIGH_WRITE   # 模型/调用方不可篡改风险等级


# ── 入参校验 + 越权参数拒绝 ─────────────────────────────────────
def test_validate_args_accepts_valid():
    _spec().validate_args({"query": "富岭包装规范"})   # 不抛


def test_validate_args_rejects_missing_required():
    with pytest.raises(ToolArgsError):
        _spec().validate_args({})


def test_validate_args_rejects_forged_identity_param():
    # 报告 §4：user_dept 由 ctx 注入、schema 中无此参数 → additionalProperties:false 拒绝伪造
    with pytest.raises(ToolArgsError):
        _spec().validate_args({"query": "x", "user_dept": "forged_finance"})


# ── ToolResult / ContentBlock ───────────────────────────────────
def test_toolresult_constructors():
    assert ToolResult.ok().status == "succeeded"
    assert ToolResult.text_ok("答案").content[0].text == "答案"
    assert ToolResult.fail("boom").status == "failed"
    assert ToolResult.denied("越权").status == "denied"
    assert ToolResult.pending().status == "pending_approval"
    r = ToolResult.text_ok("x", receipt={"order": "SO-1"})
    assert r.receipt == {"order": "SO-1"}


def test_contentblock_variants():
    assert ContentBlock.of_text("hi").type == "text"
    tb = ContentBlock.of_table(["a", "b"], [[1, 2]])
    assert tb.type == "table" and tb.table["columns"] == ["a", "b"]
    assert ContentBlock.of_image("oss://k").image_ref == "oss://k"


def test_contentblock_wrong_payload_rejected():
    with pytest.raises(ValidationError):
        ContentBlock(type="text")           # text 字段缺失
    with pytest.raises(ValidationError):
        ContentBlock(type="image_ref", text="x")   # image_ref 字段缺失


def test_risk_level_is_str_enum():
    assert RiskLevel.READ_ONLY == "read_only"
    assert RiskLevel.HIGH_WRITE.value == "high_write"


# ── EnterpriseTool Protocol ─────────────────────────────────────
def test_enterprise_tool_protocol_structural():
    class _KnowledgeSearch:
        def __init__(self, spec):
            self.spec = spec

        def run(self, ctx, args, idempotency_key=None):
            return ToolResult.text_ok("ok")

    tool = _KnowledgeSearch(_spec())
    assert isinstance(tool, EnterpriseTool)          # runtime_checkable 结构匹配
    assert tool.run(None, {"query": "x"}).status == "succeeded"


def test_non_tool_is_not_enterprise_tool():
    class _NotATool:
        pass

    assert not isinstance(_NotATool(), EnterpriseTool)
