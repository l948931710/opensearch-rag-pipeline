# -*- coding: utf-8 -*-
"""test_agent_runtime_registry.py — ToolRegistry（Task 13 / 报告 §4）"""
import json

import pytest

from opensearch_pipeline.agent_runtime.registry import ToolDisabled, ToolNotFound, ToolRegistry
from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolResult, ToolSpec


class _Tool:
    def __init__(self, name, version="1.0.0", risk=RiskLevel.READ_ONLY,
                 scope="kb.search", deprecated=False):
        self.spec = ToolSpec(
            name=name, version=version, description="d",
            input_schema={"type": "object"}, output_schema={"type": "object"},
            risk_level=risk, permission_scope=scope, data_classification="internal",
            deprecated=deprecated,
            idempotency="key_required" if risk is not RiskLevel.READ_ONLY else "none")

    def run(self, ctx, args, idempotency_key=None):
        return ToolResult.text_ok("ok")


def test_register_get_resolve():
    reg = ToolRegistry()
    t = _Tool("knowledge_search")
    reg.register(t)
    assert reg.get("knowledge_search") is t
    assert reg.resolve("knowledge_search") is t


def test_resolve_unknown_raises():
    with pytest.raises(ToolNotFound):
        ToolRegistry().resolve("nope")


def test_versioned_get_latest_and_specific():
    reg = ToolRegistry()
    t1, t2 = _Tool("t", "1.0.0"), _Tool("t", "2.0.0")
    reg.register(t1)
    reg.register(t2)
    assert reg.get("t") is t2                 # 最新版
    assert reg.get("t", "1.0.0") is t1
    assert reg.get("t", "9.9.9") is None


def test_kill_switch():
    reg = ToolRegistry()
    t = _Tool("u8_writeback", risk=RiskLevel.HIGH_WRITE, scope="u8.writeback")
    reg.register(t)
    reg.disable("u8_writeback")
    assert reg.is_disabled("u8_writeback")
    with pytest.raises(ToolDisabled):
        reg.resolve("u8_writeback")
    reg.enable("u8_writeback")
    assert reg.resolve("u8_writeback") is t


def test_list_specs_excludes_deprecated_and_disabled():
    reg = ToolRegistry()
    reg.register(_Tool("a"))
    reg.register(_Tool("b", deprecated=True))
    reg.register(_Tool("c"))
    reg.disable("c")
    assert {s.name for s in reg.list_specs()} == {"a"}


def test_to_registry_rows():
    reg = ToolRegistry()
    reg.register(_Tool("knowledge_search"))
    rows = reg.to_registry_rows(registered_by="platform")
    assert len(rows) == 1
    r = rows[0]
    assert r["tool_name"] == "knowledge_search" and r["status"] == "active"
    assert r["risk_level"] == "read_only"
    assert json.loads(r["spec_json"])["name"] == "knowledge_search"


def test_drift_check():
    reg = ToolRegistry()
    reg.register(_Tool("a", "1.0.0"))
    reg.register(_Tool("b", "1.0.0"))
    warnings = reg.drift_check([{"tool_name": "a", "version": "1.0.0"},
                                {"tool_name": "z", "version": "1.0.0"}])
    assert any("b@1.0.0" in w for w in warnings)      # 代码有 DB 无
    assert any("z@1.0.0" in w for w in warnings)      # DB 有代码无


# ── DB 驱动 kill switch 的读侧钩子（深度审查多实例运维组）─────────────────────
def test_external_disabled_source_blocks_resolve_and_list():
    reg = ToolRegistry()
    reg.register(_Tool("knowledge_search"))
    reg.register(_Tool("u8_writeback"))
    reg.attach_disabled_source(lambda: {"u8_writeback"})
    with pytest.raises(ToolDisabled):
        reg.resolve("u8_writeback")
    assert reg.resolve("knowledge_search") is not None       # 其余工具不受影响
    assert reg.is_disabled("u8_writeback")
    assert [s.name for s in reg.list_specs()] == ["knowledge_search"]   # 模型不可见被停用工具


def test_external_source_failure_is_fail_open():
    """kill switch 读故障绝不误杀：源抛错 → 只按进程内集合。"""
    reg = ToolRegistry()
    reg.register(_Tool("knowledge_search"))
    def _boom():
        raise RuntimeError("db down")
    reg.attach_disabled_source(_boom)
    assert reg.resolve("knowledge_search") is not None
    assert not reg.is_disabled("knowledge_search")


def test_process_local_and_external_union():
    reg = ToolRegistry()
    reg.register(_Tool("a"))
    reg.register(_Tool("b"))
    reg.disable("a")                                          # 进程内
    reg.attach_disabled_source(lambda: {"b"})                 # DB 源
    assert reg.is_disabled("a") and reg.is_disabled("b")
    assert reg.list_specs() == []
