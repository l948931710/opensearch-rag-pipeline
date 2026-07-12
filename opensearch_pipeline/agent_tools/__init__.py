# -*- coding: utf-8 -*-
"""
agent_tools — 业务工具实现（EnterpriseTool）。

依赖方向：agent_tools → agent_runtime（只 import 契约，不碰 loop/executor 框架）。
内置工具在此经 build_default_registry 注册进 ToolRegistry（实例注入通道）。
"""
from opensearch_pipeline.agent_runtime.registry import ToolRegistry
from opensearch_pipeline.agent_tools.knowledge_search import KnowledgeSearchTool  # noqa: F401

__all__ = ["KnowledgeSearchTool", "build_default_registry"]


def build_default_registry() -> ToolRegistry:
    """注册内置工具 → ToolRegistry（首批仅 knowledge_search；新工具在此追加）。

    ⚠️ ontology 工具（ontology_resolve / ontology_identity_resolve）**有意不在此注册**
    （2026-07-11 重审计 §3 复核为「计划内中间态」）：真实播种/回填/身份写的组织 gate
    ①③④ 未签字，PMC-1 工具面接线归 PR11-13，届时随 gate 一起放行；双守护单测锁死
    本排除（改这里须先改测试 = 显式决策）。当前生产形态 = 带审批/治理底座的知识检索
    canary；per-tool scope 现算（resolve_scope_live）随工具接线自动生效（快照回退已
    加一次性告警）。"""
    reg = ToolRegistry()
    reg.register(KnowledgeSearchTool())
    return reg
