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
import os
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
        if _budget_enabled():
            content, receipt, artifacts = _format_budgeted(chunks, ctx)
        else:
            content, receipt = _format_chunks(chunks)
            # 进程内旁路（exclude=True 不落库不进线协议）：原样 chunks 供 serving 层
            # 构建 sources/content_blocks 帧——agent 答案契约与普通问答对齐。
            artifacts = {"chunks": chunks}
        if speculative_hit:
            receipt["speculative"] = True   # 落 tool_invocation.receipt_json，命中率可量化
        # PR12 实体锚定（RAG_ONTOLOGY_ANCHOR_ENABLE 默认 off；不动 retriever.py）：
        # 问句里的编号 exact-only 消解命中且可读 → 本体档案块前置到检索片段之前。
        # off 臂逐字节不变（L7 冻结基线不受默认态影响）；on 臂 fail-open——锚定任何
        # 失败只等于没锚，绝不影响检索结果本身。
        from opensearch_pipeline.ontology.packing_lookup import anchor_enabled
        if anchor_enabled():
            try:
                from opensearch_pipeline.ontology.packing_lookup import anchor_for_query
                block, anchor_receipt = anchor_for_query(
                    query, acl_groups=(ctx.acl_groups or ()))
                if block:
                    content = [ContentBlock.of_text(block)] + list(content)
                    receipt["anchor"] = anchor_receipt
            except Exception:   # noqa: BLE001
                logger.warning("实体锚定挂载失败（忽略）", exc_info=True)
        result = ToolResult.ok(content=content, receipt=receipt)
        result.artifacts = artifacts
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


# ── 上下文预算打包（延迟优化第三刀，2026-07-11，设计+三评审：docs/agent-platform-v2/
#    tooltext-context-budget-design_2026-07-11.md）───────────────────────────────
# 复用普通问答路径打包器 _format_context_ex：同预算（缺省 6000=llm_generator 签名默认
# 同值）、同截断语义、同 header/相关度标签/图标记；agent 增量=诚实注记两行 + 跨检索
# 字节等同去重。RAG_AGENT_TOOL_CONTEXT_BUDGET 默认 on（质量评测过门后翻转，证据见
# 设计文档 §8）；kill switch=false。


def _budget_enabled() -> bool:
    """默认 on（2026-07-11 质量评测过门后翻转：L7 扩展 31 例 grounded 9/9 + staging
    三臂探针 on@6000 事实零回退/中位 tokens -37%；证据见设计文档 §4.3 结果）。"""
    return os.environ.get("RAG_AGENT_TOOL_CONTEXT_BUDGET", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def _budget_chars() -> int:
    try:
        return max(500, int(os.environ.get("RAG_AGENT_TOOL_CONTEXT_CHARS", "6000")))
    except ValueError:
        return 6000


class SearchSession:
    """per-run 检索会话（挂 ctx.search_session；瞬态三律：服务端构造/绝不序列化/
    resume 恒 None→去重自动禁用 fail-open——审批挂起续跑段去重失效是已声明行为）。

    去重登记走【送达点提交】（评审 R②-4）：工具只把 keys staged 进
    artifacts["dedup_keys"]，executor 驱动线程在成功结果 gen.send 前调 commit_keys
    ——超时孤儿线程/义务扣留路径的结果永不被消费，keys 永不提交，无「登记了没送达」
    语义竞态，且所有写收敛到驱动线程（无锁）。
    前提（R②-5）：依赖同 run 工具调用串行（executor 单驱动线程）；并行调度须重审。
    """

    __slots__ = ("seen", "committed_calls")

    def __init__(self) -> None:
        self.seen: set = set()
        self.committed_calls: int = 0

    def commit_keys(self, keys) -> None:
        self.seen.update(keys or ())
        self.committed_calls += 1


def _dedup_key(c: Dict[str, Any]) -> tuple:
    """字节等同去重 key（R①-2/3）：(身份, sha1(完整 chunk_text))——只在字节等同时
    去重，「零信息损失」是构造性保证；stitch fail-open 不对称/重拼形态差异自然放行。"""
    import hashlib
    h = hashlib.sha1(str(c.get("chunk_text") or "").encode("utf-8")).hexdigest()
    cid = c.get("chunk_id")
    if cid:
        return ("cid", str(cid), h)
    return ("doc", str(c.get("doc_id") or ""), h)


def _honest_notes(packed: List[Dict[str, Any]], meta: Dict[str, Any], n_dedup: int) -> List[str]:
    """诚实注记（no silent caps）：预算丢弃分形态（同流程后续步骤 ≠ 较低相关度尾巴，
    R①-4）+ 去重聚合行。不进 marker 体系。"""
    notes: List[str] = []
    dropped = [packed[j] for j in meta.get("dropped_idx", ())]
    if dropped:
        shown_parents = {packed[j].get("parent_chunk_id")
                         for j in list(meta.get("full_idx", ())) + list(meta.get("halfcut_idx", ()))
                         if packed[j].get("parent_chunk_id")}
        step_m = sum(1 for c in dropped
                     if c.get("parent_chunk_id") and c.get("parent_chunk_id") in shown_parents)
        other_n = len(dropped) - step_m
        if step_m:
            notes.append(f"（该流程还有 {step_m} 个后续步骤因篇幅未展开，可继续检索）")
        if other_n:
            notes.append(f"（另有 {other_n} 条较低相关度资料因篇幅未展开，可换关键词再检索）")
    if n_dedup:
        notes.append(f"（另有 {n_dedup} 条与先前检索重复，未重复展开）")
    return notes


def _format_budgeted(chunks: List[Dict[str, Any]], ctx: "ExecutionContext"
                     ) -> Tuple[List[ContentBlock], Dict[str, Any], Dict[str, Any]]:
    """预算打包路径：跨检索字节等同去重 → _format_context_ex（同普通路径语义）→
    诚实注记。返回 (content, receipt, artifacts)；artifacts.chunks **恒等于打包列表**
    （<<IMG:N>> 编号基准，R①-5），included 供 sources 帧，dedup_keys 供送达点提交。"""
    # 全量遥测（R①-8）：doc_ids/chunk_count 按检索原样计，与预算/去重无关
    receipt: Dict[str, Any] = {
        "doc_ids": [c.get("doc_id") for c in chunks if c.get("doc_id")],
        "chunk_count": len(chunks),
    }
    if not chunks:
        return [ContentBlock.of_text("未检索到相关资料。")], receipt, {"chunks": []}
    session = getattr(ctx, "search_session", None)
    seen = session.seen if session is not None else set()
    later_call = bool(session is not None and session.committed_calls > 0)
    packed = [c for c in chunks if _dedup_key(c) not in seen] if seen else list(chunks)
    n_dedup = len(chunks) - len(packed)
    meta: Dict[str, Any] = {}
    if packed:
        from opensearch_pipeline.llm_generator import _format_context_ex
        # 第 2+ 次检索 pure_text=True（R①-11）：保留 [📷 图片] 语义文本、不注入
        # <<IMG:N>>——v1 出图门仅单检索，多检索的标记只是裸标记泄漏面+编号漂移面。
        text, included = _format_context_ex(packed, max_chars=_budget_chars(),
                                            pure_text=later_call, meta_out=meta)
        notes = _honest_notes(packed, meta, n_dedup)
        if notes:
            text = text + "\n" + "\n".join(notes)
    else:
        included = []
        text = "（本次检索结果与先前已展开的资料完全重复，未重复展开；可换关键词再检索。）"
    # seen 只登记完整展开块（half-cut/salvage 排除，R①-1）——由此获得「翻页」性质：
    # 首查被截的尾步不登记，再检索时前排命中被去重腾出预算、尾步得以完整展开。
    dedup_keys = [_dedup_key(packed[j]) for j in meta.get("full_idx", ())]
    receipt.update({"ctx_chars": len(text), "dropped": len(meta.get("dropped_idx", ())),
                    "dedup": n_dedup})
    artifacts = {"chunks": packed, "included": included, "dedup_keys": dedup_keys}
    return [ContentBlock.of_text(text)], receipt, artifacts


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

    构造与起跑分离（F1）：构造零成本，`start()` 由 executor 在并发准入（占到 run 槽）
    之后调用——曾经构造即起跑，让并发墙上每个被 429 拒绝的 submit 仍各烧一次
    embedding+检索（S2 实测 ≈1.0 次/被拒）。未起跑时 take_if_match 恒 None（回退真检索）。
    """

    def __init__(self, question: str, user_dept: Optional[List[str]], pool) -> None:
        self.question = question
        self._used = False
        self._pool = pool
        self._user_dept = user_dept
        self._future = None

    def start(self) -> None:
        """准入后起跑（幂等；executor 单线程调用）。失败不抛——_future 保持 None，
        工具侧照走真检索。"""
        if self._future is not None:
            return
        try:
            self._future = self._pool.submit(self._fetch, self.question, self._user_dept)
        except Exception:   # noqa: BLE001 — 池已关/饱和等：fail-open
            logger.info("投机检索预取起跑失败（回退真检索）", exc_info=True)

    @staticmethod
    def _fetch(question: str, user_dept: Optional[List[str]]):
        from opensearch_pipeline.retriever import retrieve_and_enrich
        return retrieve_and_enrich(query=question, top_k=None, user_dept=user_dept)

    def take_if_match(self, query: str, top_k: Optional[int],
                      timeout: float = 25.0) -> Optional[List[Dict[str, Any]]]:
        # timeout < ToolSpec.timeout_s(30)：预取卡死时留出真检索被执行器统一收尸的余量
        if (self._future is None or self._used or top_k is not None
                or not query_matches_question(query, self.question)):
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
