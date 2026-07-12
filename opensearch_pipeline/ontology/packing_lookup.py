# -*- coding: utf-8 -*-
"""
packing_lookup.py — 检索问句的实体锚定编排（本体 P0 PR12）。

职责：从自由文本问句里提取"像编号"的 token → **exact-only 纯读消解**（active 别名精确
命中，零模糊零候选——宁可不锚不误锚）→ 命中 SKU 时经 sem.lookup_specs 取可见箱规/香规
→ 产出一段结构化"实体档案"文本块，由 knowledge_search 在 RAG 片段**之前**前置
（flag `RAG_ONTOLOGY_ANCHOR_ENABLE`，默认 off；接入点在工具层，**不动 retriever.py**）。

纪律：
- S1 纯读：本模块零写库、miss 不入 case（观测入队归回填 worker）；
- ACL 与 sem/resolve 工具同源（can_read_object fail-closed；不可读=不锚，零存在性泄露）；
- 探测命名空间序由 `RAG_ONTOLOGY_ANCHOR_NAMESPACES` 配置（默认 "u8"）——normalize
  分派表之外的命名空间原样跳过；
- 每问至多锚 `RAG_ONTOLOGY_ANCHOR_MAX`（默认 1）个实体（锚定是提示不是围墙，贪多
  会把工具结果推成档案清单、稀释检索片段）；
- 全程 fail-open：任何异常只等于"没锚上"，绝不影响检索主路径。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["anchor_enabled", "anchor_for_query", "extract_code_tokens"]

# "像编号"启发：≥4 字符、至少一位数字、由字母数字与 -/#. 组成（中文问句里的货号形态）。
# 纯数字长串（电话/日期）刻意排除——要求至少一个字母或分隔符，降低误检。
_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/#.]{2,31}")
_HAS_DIGIT = re.compile(r"\d")
_HAS_ALPHA_OR_SEP = re.compile(r"[A-Za-z\-/#]")


def anchor_enabled() -> bool:
    """PR12 flag（默认 off——开关翻转改变工具输出，须重跑 make agent-eval 评估后再开）。"""
    return os.environ.get("RAG_ONTOLOGY_ANCHOR_ENABLE",
                          "").strip().lower() in ("1", "true", "yes", "on")


def _namespaces() -> List[str]:
    raw = os.environ.get("RAG_ONTOLOGY_ANCHOR_NAMESPACES", "u8")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _max_anchors() -> int:
    try:
        return max(1, int(os.environ.get("RAG_ONTOLOGY_ANCHOR_MAX", "1")))
    except ValueError:
        return 1


def extract_code_tokens(query: str, *, limit: int = 8) -> List[str]:
    """问句 → 候选编号 token（保序去重；过不了"像编号"启发的丢弃）。"""
    out: List[str] = []
    for m in _CODE_RE.finditer(query or ""):
        t = m.group(0).rstrip(".")          # 句末标点粘连
        if not _HAS_DIGIT.search(t) or not _HAS_ALPHA_OR_SEP.search(t):
            continue
        if t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _resolve_exact(store, namespace: str, token: str) -> Optional[Dict[str, Any]]:
    """exact-only：active 别名精确命中 + 目标对象 active。其余一律 None（零模糊）。"""
    from opensearch_pipeline.ontology.normalize import normalize
    try:
        norm = normalize(namespace, token)
    except ValueError:
        return None
    ident = store.get_active_identifier(namespace, norm)
    if ident is None:
        return None
    obj = store.get_object(ident["target_object_id"])
    if obj is None or obj.get("status") != "active":
        return None
    return obj


def _spec_line(domain: str, row: Optional[Dict[str, Any]]) -> Optional[str]:
    if row is None:
        return None
    state = row.get("spec_state")
    tail = f"（{state or '?'}" + (f"，截至 {row['as_of']}" if row.get("as_of") else "") + "）"
    if domain == "packing":
        return (f"箱规：{row.get('box_type') or '—'}，每箱 {row.get('per_box') or '?'} 件，"
                f"外箱 {row.get('outer_dim') or '?'}mm{tail}")
    return (f"堆叠/装柜：{row.get('stack_mode') or '—'}，每托 {row.get('per_stack') or '?'}，"
            f"柜容 {row.get('container_cbm') or '?'}m³{tail}")


def anchor_for_query(query: str, *, acl_groups, store=None
                     ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """问句 → (锚定文本块 or None, receipt or None)。fail-open：异常=不锚。"""
    try:
        from opensearch_pipeline.ontology.authz import can_read_object, visible_title
        from opensearch_pipeline.ontology.sem import lookup_specs
        if store is None:
            from opensearch_pipeline.ontology.store import RDSOntologyStore
            store = RDSOntologyStore()
        acl = set(acl_groups or ())
        anchors: List[str] = []
        receipt: List[Dict[str, Any]] = []
        for token in extract_code_tokens(query):
            if len(anchors) >= _max_anchors():
                break
            for ns in _namespaces():
                obj = _resolve_exact(store, ns, token)
                if obj is None:
                    continue
                if not can_read_object(obj, acl=acl):
                    continue                         # 不可读=不锚（零存在性泄露）
                title = visible_title(obj.get("title"), obj.get("data_classification"),
                                      obj.get("owner_dept"), acl=acl)
                lines = [f"编号 {token} = {title or obj['canonical_ref']} "
                         f"[{obj['canonical_ref']}]（{obj['object_type']}）"]
                spec_refs: Dict[str, Any] = {}
                if obj.get("object_type") == "sku":
                    ans = lookup_specs(store, obj["object_id"], acl_groups=acl)
                    for domain in ("packing", "stacking"):
                        line = _spec_line(domain, ans.specs.get(domain))
                        if line:
                            lines.append(line)
                            spec_refs[domain] = (ans.specs[domain] or {}).get("spec_ref")
                    for n in ans.notes:
                        if "draft" in n or "未验证" in n:
                            lines.append(f"⚠️ {n}")
                anchors.append("\n".join(lines))
                receipt.append({"token": token, "namespace": ns,
                                "canonical_ref": obj["canonical_ref"],
                                "object_type": obj["object_type"], "specs": spec_refs})
                break                                # 该 token 已锚定，不再试后续命名空间
        if not anchors:
            return None, None
        block = ("【实体档案·本体锚定】\n" + "\n---\n".join(anchors)
                 + "\n（以上为企业本体登记档案；以下为知识库检索片段）")
        return block, {"anchors": receipt}
    except Exception:   # noqa: BLE001 — 锚定是增强，绝不影响检索主路径
        logger.warning("实体锚定失败（fail-open，无锚返回）", exc_info=True)
        return None, None
