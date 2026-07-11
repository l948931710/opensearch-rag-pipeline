# -*- coding: utf-8 -*-
"""
knowledge_search.py — 首工具（v2 报告 §4/§C · plan WS1-3）

包装统一检索入口 `retrieve_and_enrich`。**READ_ONLY**；权限数据面复用现有 ACL：
`user_dept` 由 `ctx.acl_groups` **服务端注入**，input_schema 里根本没有身份参数
（`additionalProperties:false` → 请求体/模型伪造 user_dept 直接被拒）。不建第二套 ACL。

业务工具：只 import agent_runtime 的**契约**（ToolSpec/ToolResult/…），不碰 loop/executor 框架。
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from opensearch_pipeline.agent_runtime.tool import (
    ContentBlock,
    RiskLevel,
    ToolResult,
    ToolSpec,
)

if TYPE_CHECKING:
    from opensearch_pipeline.agent_runtime.context import ExecutionContext

logger = logging.getLogger(__name__)

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
        # 投机检索（延迟优化）：submit 时已用原问题并行预取（同 ACL），改写是原问题的
        # 凝练时直接复用——省掉整段串行检索。miss/失败恒回退真检索（fail-open）。
        chunks: Optional[List[Dict[str, Any]]] = None
        speculative_hit = False
        spec = getattr(ctx, "speculative_search", None)
        if spec is not None:
            try:
                chunks = spec.take_if_match(query, top_k)
                speculative_hit = chunks is not None
            except Exception:   # noqa: BLE001 — 投机层任何异常都不影响真检索
                logger.info("投机检索消费异常（回退真检索）", exc_info=True)
        if chunks is None:
            try:
                from opensearch_pipeline.retriever import retrieve_and_enrich
                chunks = retrieve_and_enrich(query=query, top_k=top_k, user_dept=user_dept)
            except Exception as e:   # noqa: BLE001 — 检索失败以 ToolResult 表达，不外泄异常
                return ToolResult.fail(f"知识库检索失败: {e}")
        content, receipt = _format_chunks(chunks)
        if speculative_hit:
            receipt["speculative"] = True   # 落 tool_invocation.receipt_json，命中率可量化
        result = ToolResult.ok(content=content, receipt=receipt)
        # 进程内旁路（exclude=True 不落库不进线协议）：原样 chunks 供 serving 层
        # 构建 sources/content_blocks 帧——agent 答案契约与普通问答对齐。
        result.artifacts = {"chunks": chunks}
        return result


def _format_chunks(chunks: List[Dict[str, Any]]) -> Tuple[List[ContentBlock], Dict[str, Any]]:
    if not chunks:
        return [ContentBlock.of_text("未检索到相关资料。")], {"doc_ids": [], "chunk_count": 0}
    lines: List[str] = []
    doc_ids: List[str] = []
    for i, c in enumerate(chunks, 1):
        text = c.get("chunk_text") or c.get("content") or c.get("text") or ""
        title = c.get("doc_title") or c.get("title") or c.get("doc_id") or ""
        lines.append(f"[{i}] {title}{_img_marker(i, c)}\n{text}".strip())
        did = c.get("doc_id")
        if did:
            doc_ids.append(did)
    receipt = {"doc_ids": doc_ids, "chunk_count": len(chunks)}
    return [ContentBlock.of_text("\n\n".join(lines))], receipt


# ── 投机检索（延迟优化，2026-07-11）────────────────────────────────────────────
# submit 时用【原问题】并行预取 retrieve_and_enrich（与普通问答同一查询语义、同一 ACL），
# 模型提案 knowledge_search 时若查询是原问题的凝练改写 → 直接复用预取结果，把 7-16s 的
# 串行检索与第一轮模型调用重叠掉。质量依据：普通问答恒用原问题检索且为校准基线，
# 凝练改写命中时复用原问题结果 ≈ 普通模式质量，不是降级。
_PUNCT = re.compile(r"[\s，。？！?!、,.:：;；\"'“”‘’（）()\[\]【】《》<>·…\-—_/\\]+")


def _norm(s: str) -> str:
    return _PUNCT.sub("", (s or "").lower())


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def query_matches_question(query: str, question: str, threshold: float = 0.8) -> bool:
    """改写是否为原问题的凝练：归一化相等 / 子串 / query 的 bigram 有 ≥threshold 落在
    原问题里（乱序凝练也命中）。主题变了（如二次检索换关键词）→ 包含率骤降 → miss。"""
    nq, nu = _norm(query), _norm(question)
    if not nq or not nu:
        return False
    if nq == nu or nq in nu:
        return True
    bq = _bigrams(nq)
    if not bq:
        return False
    return len(bq & _bigrams(nu)) / len(bq) >= threshold


class SpeculativeSearch:
    """一次性投机预取句柄（挂在 ctx.speculative_search，serving 层构造）。

    只消费一次：首个命中的 knowledge_search 拿走结果；多轮检索的后续 call 与
    显式改 top_k 的 call 恒走真检索。预取失败/超时 → None（fail-open 回退真检索）。
    """

    def __init__(self, question: str, user_dept: Optional[List[str]], pool) -> None:
        self.question = question
        self._used = False
        self._future = pool.submit(self._fetch, question, user_dept)

    @staticmethod
    def _fetch(question: str, user_dept: Optional[List[str]]):
        from opensearch_pipeline.retriever import retrieve_and_enrich
        return retrieve_and_enrich(query=question, top_k=None, user_dept=user_dept)

    def take_if_match(self, query: str, top_k: Optional[int],
                      timeout: float = 25.0) -> Optional[List[Dict[str, Any]]]:
        # timeout < ToolSpec.timeout_s(30)：预取卡死时留出真检索被执行器统一收尸的余量
        if self._used or top_k is not None or not query_matches_question(query, self.question):
            return None
        self._used = True   # 无论成败只消费一次
        try:
            return self._future.result(timeout=timeout)
        except Exception:   # noqa: BLE001 — 预取失败/超时 → 回退真检索
            logger.info("投机检索预取失败/超时（回退真检索）", exc_info=True)
            return None


def _img_marker(i: int, chunk: Dict[str, Any]) -> str:
    """带图 chunk 的 ` [📷 图片] <<IMG:i>>` 段——**复用普通问答同一实现**（含
    RAG_IMG_SUBINDEX 分图行为与 fail-open），编号与 [i] 平铺序号同源，
    content_blocks 按此对位穿插图片。无可渲染图返回空串。"""
    try:
        from opensearch_pipeline.content_blocks_builder import renderable_image_refs
        if not renderable_image_refs(chunk):
            return ""
        from opensearch_pipeline.llm_generator import _img_marker_segment
        return _img_marker_segment(i - 1, chunk)   # 0-based 入参，内部 n=i+1 → <<IMG:i>>
    except Exception:   # noqa: BLE001 — 标记失败绝不影响检索文本本体
        return ""
