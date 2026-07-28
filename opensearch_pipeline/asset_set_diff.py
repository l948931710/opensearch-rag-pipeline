# -*- coding: utf-8 -*-
"""asset_set_diff.py — 资产集变化的可见性（方案 F，2026-07-25）。纯函数 + 明确 IO 缝。

**要解决的沉默**：同一 doc 的新版本入库时，"漏斗存活图集比上一版本少了几张"目前**完全
没有任何信号**。实测（选项 A，`docs/evidence/funnel_flip_matrix_20260725/`）：缓存里
2026-07-20 写下的判决与今天的 VLM 有 **6.5% 不一致，全部单向 ROUTE_TO_VECTOR →
DISCARD_DECORATIVE**，其中 **4 张是 GT 明确期望的步骤配图**。也就是说这些文档现在图还在，
靠的是旧缓存条目；缓存一旦淘汰或版本提升，图就会静默消失。

**设计要点（每条都有踩过的坑）**：

· 数据源是 canonical 的 `assets`，**不是** `chunk_meta.image_refs_json` —— 后者按 chunk 存，
  而独立 image chunk 与 visual_knowledge chunk 把图字段放在 `extra` 顶层、根本不写
  `image_refs`，validator 还可能在绑定后丢掉整个 chunk ⇒ 它表达的是"最终载荷承载"，
  不是漏斗的存活判定。

· **缺 `assets` 键 = unknown，显式 `[]` 才是空集**。任一侧 unknown 一律 `unavailable`，
  **绝不产出全增/全删** —— 那会把一次读取失败包装成"整篇图没了"的假事件。

· `image_index` 是 **occurrence ordinal 而非内容身份**。跨内容版本只能称 ordinal diff；
  高置信的"某张图被删了"只在 raw checksum 相同时才成立。

· 判 `image_index` 用 `is not None` —— `0` 是合法 index。

· 只有 `source_relation=same_checksum` **且** `set_status=complete` **且** 有 removal，
  才升为高置信 `same_source_drift`；任一侧 partial 只给 `ordinal_diff_partial`。

本模块**不做** IO、不写库、不改任何摄取判定；调用方负责取数与留痕，且全程 fail-open。
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

# 与 pipeline_nodes 的 clean-route 过滤同一判据（两处已有同款：:1257 / :2922）
CLEAN_ROUTES = ("ROUTE_TO_TEXT", "ROUTE_TO_VECTOR")

MAX_LISTED = 20          # 审计 message 里每侧最多列出的 index 数（避免 TEXT 无限膨胀）


class SourceRelation:
    SAME_CHECKSUM = "same_checksum"   # raw sha256 相等 —— 高置信同源
    SAME_ETAG = "same_etag"           # 仅 ETag 相等 —— 中置信（ETag 未必等于内容哈希）
    CHANGED = "changed"
    UNKNOWN = "unknown"


class SetStatus:
    COMPLETE = "complete"         # 两侧集合都完整可比
    PARTIAL = "partial"           # 某侧存在 clean-route 但缺 image_index 的 asset
    UNAVAILABLE = "unavailable"   # 任一侧 unknown（缺 assets 键 / 读取失败）


@dataclass(frozen=True)
class SurvivorSet:
    """一个版本的漏斗存活图集。`known=False` 表示"不知道"，**不等于空集**。"""
    indices: frozenset
    n_unindexed: int          # clean-route 但没有 image_index 的 asset 数
    known: bool

    @property
    def complete(self) -> bool:
        return self.known and self.n_unindexed == 0


UNKNOWN_SET = SurvivorSet(frozenset(), 0, False)


# canonical 里表示"本轮抽取不完整"的三个既有信号（`pipeline_nodes` 明确为跨 stage 传递
# 降级而设）。审查 P1-8：只看 `assets` 是不够的 —— 一次"整段图提取失败"产出的正是一个
# **干净的 `[]`**，与"这篇本来就没图"字面等价。
_DEGRADED_KEYS = ("vlm_degraded_count", "partial_loss_notes", "warnings")


def extraction_degraded(canonical: Optional[Dict[str, Any]]) -> bool:
    """本轮抽取是否带降级信号 —— 带则其空/少的 assets **不可作高置信 removal 依据**。"""
    if not isinstance(canonical, dict):
        return False
    return any(canonical.get(k) for k in _DEGRADED_KEYS)


def survivors(canonical: Optional[Dict[str, Any]]) -> SurvivorSet:
    """canonical → 存活图集。**缺 `assets` 键返回 unknown**，显式 `[]` 是空集。"""
    if not isinstance(canonical, dict) or "assets" not in canonical:
        return UNKNOWN_SET
    assets = canonical.get("assets")
    if not isinstance(assets, (list, tuple)):
        return UNKNOWN_SET
    idx, unindexed = set(), 0
    for a in assets:
        if not isinstance(a, dict) or a.get("status") not in CLEAN_ROUTES:
            continue
        i = a.get("image_index")
        if i is None:                     # 0 是合法 index，必须用 is None 判
            unindexed += 1
        else:
            idx.add(i)
    return SurvivorSet(frozenset(idx), unindexed, True)


def _norm_etag(v: Optional[str]) -> str:
    return (v or "").strip().strip('"').lower()


def source_relation(prev_checksum: Optional[str], cur_checksum: Optional[str],
                    prev_etag: Optional[str] = None, cur_etag: Optional[str] = None) -> str:
    """原文档的同源关系。checksum 相等 = 高置信；仅 ETag 相等 = 中置信。"""
    if prev_checksum and cur_checksum:
        return SourceRelation.SAME_CHECKSUM if prev_checksum == cur_checksum \
            else SourceRelation.CHANGED
    pe, ce = _norm_etag(prev_etag), _norm_etag(cur_etag)
    if pe and ce:
        return SourceRelation.SAME_ETAG if pe == ce else SourceRelation.CHANGED
    return SourceRelation.UNKNOWN


def event_key(doc_id: str, baseline_version_no: Optional[int],
              observed_version_no: Optional[int]) -> str:
    """事件标识。**当前 kb_audit_log 没有业务唯一键，本值不构成幂等保证** ——
    它只是给人工/后续去重用的稳定串（唯一索引列为后续项）。"""
    raw = f"ASSET_SET_DIFF|{doc_id}|{baseline_version_no}|{observed_version_no}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_event(doc_id: str, baseline_version_no: Optional[int],
                observed_version_no: Optional[int],
                prev: SurvivorSet, cur: SurvivorSet, relation: str,
                observed_stage: str) -> Optional[Dict[str, Any]]:
    """产出事件 dict；**集合无变化或不可比时返回 None（不写审计噪声）**。

    observed_stage 说明这条事件是在哪个时点观察到的（`SKIP_GATE` / `CHUNK_COMMITTED`）——
    两者都**不表示** observed 版本已上线（stage 3 成功才写 index_status=SUCCESS）。
    """
    if not prev.known or not cur.known:
        return None                       # unavailable：绝不产出全增/全删
    removed = sorted(prev.indices - cur.indices)
    added = sorted(cur.indices - prev.indices)
    if not removed and not added:
        return None
    status = SetStatus.COMPLETE if (prev.complete and cur.complete) else SetStatus.PARTIAL
    if relation == SourceRelation.SAME_CHECKSUM and status == SetStatus.COMPLETE and removed:
        kind = "same_source_drift"        # 高置信：同一份文件，图却少了
    elif status == SetStatus.PARTIAL:
        kind = "ordinal_diff_partial"
    elif relation == SourceRelation.CHANGED:
        kind = "source_changed"
    else:
        kind = "ordinal_diff"
    return {
        "kind": kind,
        "source_relation": relation,
        "set_status": status,
        "observed_stage": observed_stage,
        "baseline_version_no": baseline_version_no,
        "observed_version_no": observed_version_no,
        "n_prev": len(prev.indices), "n_cur": len(cur.indices),
        "n_prev_unindexed": prev.n_unindexed, "n_cur_unindexed": cur.n_unindexed,
        "n_removed": len(removed), "n_added": len(added),
        "removed": removed[:MAX_LISTED], "added": added[:MAX_LISTED],
        "removed_truncated": len(removed) > MAX_LISTED,
        "added_truncated": len(added) > MAX_LISTED,
        "event_key": event_key(doc_id, baseline_version_no, observed_version_no),
    }


def is_high_confidence_loss(event: Optional[Dict[str, Any]]) -> bool:
    """是否值得打显著日志：高置信同源漂移且确有图消失。"""
    return bool(event and event.get("kind") == "same_source_drift")


def event_message(event: Dict[str, Any]) -> str:
    """审计 message：有界结构化摘要。**不含 OCR 原文 / VLM 摘要 / 任何图像内容。**"""
    return json.dumps(event, ensure_ascii=False, sort_keys=True)[:1500]


def log_line(doc_id: str, event: Dict[str, Any]) -> str:
    return (f"    ⚠️ [asset-drift] {doc_id} v{event['baseline_version_no']}"
            f"→v{event['observed_version_no']}: 同源文件但存活图集少了 {event['n_removed']} 张"
            f"（removed={event['removed']}{'…' if event['removed_truncated'] else ''}"
            f", added={event['n_added']}, stage={event['observed_stage']}"
            f", event_key={event['event_key']}）—— 疑似 VLM 判定漂移，图会从答案里消失")


def baseline_sql() -> str:
    """上一**成功服务**版本（不是 version_no-1，也不是 current_version_no 指针）。

    失败/跳过/隔离的版本不得作基线；`canonical_json_key IS NULL` 无从比较 ⇒ 不入选。
    """
    return ("SELECT version_no, canonical_json_key, checksum_sha256 "
            "FROM document_version "
            "WHERE doc_id=%s AND version_no<%s AND index_status='SUCCESS' "
            "AND canonical_json_key IS NOT NULL "
            "ORDER BY version_no DESC LIMIT 1")


def iter_clean_assets(assets: Iterable[Any]):
    """给调用方复用的 clean-route 过滤（与 survivors 同判据）。"""
    for a in assets or ():
        if isinstance(a, dict) and a.get("status") in CLEAN_ROUTES:
            yield a
