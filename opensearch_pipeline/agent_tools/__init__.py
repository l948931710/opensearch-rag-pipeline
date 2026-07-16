# -*- coding: utf-8 -*-
"""
agent_tools — 业务工具实现（EnterpriseTool）。

依赖方向：agent_tools → agent_runtime（只 import 契约，不碰 loop/executor 框架）。
内置工具在此经 build_default_registry 注册进 ToolRegistry（实例注入通道）。
"""
import os

from opensearch_pipeline.agent_runtime.registry import ToolRegistry
from opensearch_pipeline.agent_tools.knowledge_search import KnowledgeSearchTool  # noqa: F401

__all__ = ["KnowledgeSearchTool", "build_default_registry", "ontology_tools_enabled",
           "ontology_write_tools_enabled"]


def ontology_tools_enabled() -> bool:
    """PR13 接线开关 `RAG_ONTOLOGY_TOOLS_ENABLE`（默认 **off**）——批次5（unknown-unknowns
    P0-07a）后只管**只读**工具族（ontology_resolve / packing_calc）。

    off（默认）= 生产形态维持知识检索 canary：ontology 工具不进 registry、policy 无
    ontology 授予、系统提示词零变化（L7 冻结基线不受默认态影响）。
    开启前置（缺一不可，均 user-gated）：①组织 gate ①③④ 签字 ②`make agent-eval`
    对 flag-on 臂重跑并重冻 L7 基线（提示词/工具面都变了）。
    守护单测锁双向语义（默认排除 + flag-on 注册）。"""
    return os.environ.get("RAG_ONTOLOGY_TOOLS_ENABLE",
                          "").strip().lower() in ("1", "true", "yes", "on")


def ontology_write_tools_enabled() -> bool:
    """批次5（unknown-unknowns P0-07a）：HIGH_WRITE 工具（ontology_identity_resolve）
    独立开关 `RAG_ONTOLOGY_WRITE_TOOLS_ENABLE`（默认 **off**）——此前一杆同开只读解析
    与身份事实写入：运维为开只读解析翻 flag，会同时把风险面扩大到 HIGH_WRITE。
    写开关**叠加**在读开关之上（读关则写恒关——写工具依赖 resolve/packing 的提示词
    与授予语境）；前置在读开关清单之上再加 ③RAG_PROMPT_INJECTION_GUARD 已开
    （build_default_registry 机器强制，不再只是 warning）。"""
    return (ontology_tools_enabled()
            and os.environ.get("RAG_ONTOLOGY_WRITE_TOOLS_ENABLE",
                               "").strip().lower() in ("1", "true", "yes", "on"))


def build_default_registry() -> ToolRegistry:
    """注册内置工具 → ToolRegistry。

    ontology 只读族（ontology_resolve / packing_calc）由 `RAG_ONTOLOGY_TOOLS_ENABLE`
    门控；HIGH_WRITE（ontology_identity_resolve）另需 `RAG_ONTOLOGY_WRITE_TOOLS_ENABLE`
    （批次5 P0-07a 拆分——2026-07-11 重审计 §3 的「计划内中间态」到此收口）。
    policy 的授予同两杆联动（policy.default_policy_engine），三处一致。

    P0-07c 机器强制：写工具开启而 RAG_PROMPT_INJECTION_GUARD 未开 → **硬失败**
    （routes/_build_runtime 的 warning 只为只读窗口保留；「工具结果不带不可信边界
    进模型」与身份事实写入同场即事故预备役，绝不带病注册）。"""
    reg = ToolRegistry()
    reg.register(KnowledgeSearchTool())
    if ontology_tools_enabled():
        from opensearch_pipeline.agent_tools.ontology_resolve import OntologyResolveTool
        from opensearch_pipeline.agent_tools.packing_calc import PackingCalcTool
        reg.register(OntologyResolveTool())
        reg.register(PackingCalcTool())
        if ontology_write_tools_enabled():
            from opensearch_pipeline.agent_runtime.loop import tool_data_guard_enabled
            if not tool_data_guard_enabled():
                raise RuntimeError(
                    "RAG_ONTOLOGY_WRITE_TOOLS_ENABLE 已开而 RAG_PROMPT_INJECTION_GUARD 未开——"
                    "HIGH_WRITE 工具面必须带不可信工具数据边界（unknown-unknowns 批次5 "
                    "P0-07c）。请先开启 guard 并重冻 L7 基线，再启用写工具。")
            from opensearch_pipeline.agent_tools.ontology_identity_resolve import (
                OntologyIdentityResolveTool)
            reg.register(OntologyIdentityResolveTool())
    return reg
