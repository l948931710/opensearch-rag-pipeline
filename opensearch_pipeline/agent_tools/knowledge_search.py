# -*- coding: utf-8 -*-
"""
knowledge_search.py — 首工具（v2 报告 §4/§C · plan WS1-3）

包装统一检索入口 `retrieve_and_enrich`。**READ_ONLY**；权限数据面复用现有 ACL：
`user_dept` 由 `ctx.acl_groups` **服务端注入**，input_schema 里根本没有身份参数
（`additionalProperties:false` → 请求体/模型伪造 user_dept 直接被拒）。不建第二套 ACL。

业务工具：只 import agent_runtime 的**契约**（ToolSpec/ToolResult/…），不碰 loop/executor 框架。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from opensearch_pipeline.agent_runtime.tool import (
    ContentBlock,
    RiskLevel,
    ToolResult,
    ToolSpec,
)

if TYPE_CHECKING:
    from opensearch_pipeline.agent_runtime.context import ExecutionContext

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "要检索的问题文本", "minLength": 1},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 20,
                  "description": "返回片段数（缺省按系统配置，通常 7）"},
    },
    "required": ["query"],
    "additionalProperties": False,   # 拒绝伪造 user_dept 等身份参数（ACL 由 ctx 注入）
}


class KnowledgeSearchTool:
    """EnterpriseTool：企业知识库检索（含部门权限过滤）。"""

    spec = ToolSpec(
        name="knowledge_search",
        version="1.0.0",
        description="从企业知识库检索与问题相关的资料片段（自动按调用者部门权限过滤）。",
        input_schema=_INPUT_SCHEMA,
        output_schema={"type": "object"},
        risk_level=RiskLevel.READ_ONLY,
        permission_scope="kb.search",
        data_classification="internal",
        owner_team="platform",
    )

    def run(self, ctx: "ExecutionContext", args: Dict[str, Any],
            idempotency_key: Optional[str] = None) -> ToolResult:
        # 执行前 schema 校验（防御性；伪造 user_dept 等在此被拒）。middleware 也会先校验。
        self.spec.validate_args(args)
        query = args["query"]
        top_k = args.get("top_k")
        # 🔒 ACL 从 ctx 服务端注入，绝不从 args。acl_groups 是白名单归一后的部门组。
        user_dept = list(ctx.acl_groups) if ctx.acl_groups else None
        try:
            from opensearch_pipeline.retriever import retrieve_and_enrich
            chunks = retrieve_and_enrich(query=query, top_k=top_k, user_dept=user_dept)
        except Exception as e:   # noqa: BLE001 — 检索失败以 ToolResult 表达，不外泄异常
            return ToolResult.fail(f"知识库检索失败: {e}")
        content, receipt = _format_chunks(chunks)
        return ToolResult.ok(content=content, receipt=receipt)


def _format_chunks(chunks: List[Dict[str, Any]]) -> Tuple[List[ContentBlock], Dict[str, Any]]:
    if not chunks:
        return [ContentBlock.of_text("未检索到相关资料。")], {"doc_ids": [], "chunk_count": 0}
    lines: List[str] = []
    doc_ids: List[str] = []
    for i, c in enumerate(chunks, 1):
        text = c.get("chunk_text") or c.get("content") or c.get("text") or ""
        title = c.get("doc_title") or c.get("title") or c.get("doc_id") or ""
        lines.append(f"[{i}] {title}\n{text}".strip())
        did = c.get("doc_id")
        if did:
            doc_ids.append(did)
    receipt = {"doc_ids": doc_ids, "chunk_count": len(chunks)}
    return [ContentBlock.of_text("\n\n".join(lines))], receipt
