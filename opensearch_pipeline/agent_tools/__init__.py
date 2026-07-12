# -*- coding: utf-8 -*-
"""
agent_tools — 业务工具实现（EnterpriseTool）。

依赖方向：agent_tools → agent_runtime（只 import 契约，不碰 loop/executor 框架）。
内置工具在此经 build_default_registry 注册进 ToolRegistry（实例注入通道）。
"""
import os

from opensearch_pipeline.agent_runtime.registry import ToolRegistry
from opensearch_pipeline.agent_tools.knowledge_search import KnowledgeSearchTool  # noqa: F401

__all__ = ["KnowledgeSearchTool", "build_default_registry", "ontology_tools_enabled"]


def ontology_tools_enabled() -> bool:
    """PR13 唯一接线开关 `RAG_ONTOLOGY_TOOLS_ENABLE`（默认 **off**）。

    off（默认）= 生产形态维持知识检索 canary：ontology 工具不进 registry、policy 无
    ontology 授予、系统提示词零变化（L7 冻结基线不受默认态影响）。
    开启前置（缺一不可，均 user-gated）：①组织 gate ①③④ 签字 ②`make agent-eval`
    对 flag-on 臂重跑并重冻 L7 基线（提示词/工具面都变了）③RAG_PROMPT_INJECTION_GUARD
    已开（写工具上线的 P1-1 前置）。守护单测锁双向语义（默认排除 + flag-on 注册）。"""
    return os.environ.get("RAG_ONTOLOGY_TOOLS_ENABLE",
                          "").strip().lower() in ("1", "true", "yes", "on")


def build_default_registry() -> ToolRegistry:
    """注册内置工具 → ToolRegistry。

    ontology 工具族（ontology_resolve / ontology_identity_resolve / packing_calc）
    的注册由 `RAG_ONTOLOGY_TOOLS_ENABLE` 门控（见 ontology_tools_enabled 前置清单；
    2026-07-11 重审计 §3 复核为「计划内中间态」）——policy 的授予同 flag 联动
    （policy.default_policy_engine），三处一把杆。"""
    reg = ToolRegistry()
    reg.register(KnowledgeSearchTool())
    if ontology_tools_enabled():
        from opensearch_pipeline.agent_tools.ontology_identity_resolve import (
            OntologyIdentityResolveTool)
        from opensearch_pipeline.agent_tools.ontology_resolve import OntologyResolveTool
        from opensearch_pipeline.agent_tools.packing_calc import PackingCalcTool
        reg.register(OntologyResolveTool())
        reg.register(OntologyIdentityResolveTool())
        reg.register(PackingCalcTool())
    return reg
