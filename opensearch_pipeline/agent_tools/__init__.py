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
    """注册内置工具 → ToolRegistry（首批仅 knowledge_search；新工具在此追加）。"""
    reg = ToolRegistry()
    reg.register(KnowledgeSearchTool())
    return reg
