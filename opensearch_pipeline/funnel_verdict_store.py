# -*- coding: utf-8 -*-
"""funnel_verdict_store.py — 图片漏斗判决的持久记录（选项 E，方案 §11，2026-07-26）。

**问题**：漏斗判决决定哪些图能进知识库，但它今天只活在 VLM 缓存里 —— 一个 advisory、
按 rowid FIFO 淘汰、可被 `RAG_VLM_CACHE_VERSION` 一键作废的 SQLite KV。实测（§8）：
缓存里 07-20 的判决与今天的 VLM 有 **6.5% 不一致、全部单向 keep→discard**，其中 4 张是
GT 明确期望的步骤配图。也就是说现网这些文档的图覆盖，此刻是靠缓存条目撑着的。

**本模块**把同一个判决升级为 RDS 里的记录：内容寻址、不淘汰、失效必须显式换 policy 版本。

## 三个必须记住的边界

1. **只存判决，不存派生内容。** `ocr_text`/`visual_summary` 留在 VLM 缓存里（那个暴露面
   已存在），不复制进 RDS 这个新查询面 —— 图像 OCR 的 PII 脱敏默认 OFF 且不回写
   canonical。代价：RDS 命中但缓存未命中时仍要调一次 VLM 取 caption，但**强制采用记录里
   的 status**。图集稳定，caption 允许重掷（PDF-D5 已切断 caption→绑定耦合）。

2. **三态，不是二态。** `HIT` / `MISS` / `UNAVAILABLE`。把 error 当成 miss 就等于"库一抖
   就允许重判"，而重判正是我们要挡的事。`UNAVAILABLE` 且无同 policy 缓存条目时，调用方
   必须走 `partial_loss_notes` → NEEDS_REVIEW，**不得静默定稿**（codex BLOCKER-3：靠方案
   F 兜住不成立 —— F 依赖同一个 RDS、自身 fail-open，且正文改一个字就只产 `source_changed`）。

3. **内容纯度是前提。** 判决按图片字节寻址，只有在 VLM 输入确实只是图片字节时才成立。
   `RAG_VLM_DOC_CONTEXT` 会把文档标题塞进 prompt（`unified_extractor.py:370` 的
   `#F-mm7 防串染` 注释已承认跨文档 first-wins 串染）⇒ 两者**互斥**，见 `store_enabled()`。

主流程永不因本模块中断：任何异常都被吞成 `UNAVAILABLE`。
"""
import os
from typing import Any, Dict, Optional, Sequence

# 三态。刻意不用布尔/None —— "查不到"与"查不了"必须在类型层面就分开。
HIT = "hit"
MISS = "miss"
UNAVAILABLE = "unavailable"

# 只有这些 status 值得记录。degraded / DISCARD_UNREADABLE 是一次性故障的兜底，
# 与 VLM 缓存写入门（`_vlm_cache_put_allowed`）同一判据 —— 把一次性故障固化成永久
# 记录，比不记录坏得多（记录还不会被淘汰）。
RECORDABLE_STATUSES = frozenset({
    "ROUTE_TO_VECTOR", "ROUTE_TO_TEXT", "DISCARD_DECORATIVE", "QUARANTINE_SENSITIVE",
})

_TABLE = "image_funnel_verdict"


def store_enabled() -> bool:
    """判决记录总闸（`RAG_FUNNEL_VERDICT_STORE`，默认 **OFF**）。

    **apply 059 在前、开 flag 在后**；表缺失时也只是恒 UNAVAILABLE，先部署后 apply 安全。

    与 `RAG_VLM_DOC_CONTEXT` 互斥：后者把文档标题塞进 VLM prompt，判决就不再是图片字节
    的纯函数，内容寻址的主键随即失去意义（同一张图在不同文档下该有不同判决，却共用一行）。
    互斥而不是"把标题也进键"——那等于放弃跨文档复用，是另一个设计。
    """
    if os.environ.get("RAG_FUNNEL_VERDICT_STORE", "").strip().lower() \
            not in ("1", "true", "yes", "on"):
        return False
    if os.environ.get("RAG_VLM_DOC_CONTEXT", "").strip().lower() in ("1", "true", "yes", "on"):
        print("    ⚠️ [verdict-store] RAG_VLM_DOC_CONTEXT 开着 ⇒ 判决不再是图片字节的纯函数，"
              "内容寻址主键失效 —— 判决记录**不启用**（两者互斥）")
        return False
    return True


def namespace(is_public: bool) -> str:
    return "pub" if is_public else "sec"


def recordable(funnel_res: Dict[str, Any]) -> bool:
    """该判决是否值得落库。与 VLM 缓存写入门同判据（degraded / UNREADABLE 一律不记）。"""
    return (isinstance(funnel_res, dict)
            and not funnel_res.get("degraded")
            and funnel_res.get("status") in RECORDABLE_STATUSES)


def _funnel_stage(funnel_res: Dict[str, Any]) -> str:
    """判决出自哪一级。Funnel 1 是确定的几何判据、与 VLM 制度无关，值得单独标出。"""
    reason = str(funnel_res.get("reason", ""))
    if "Funnel 1" in reason:
        return "funnel1"
    return "funnel3" if reason else ""


def fetch_many(conn, hashes: Sequence[str], is_public: bool, policy: str):
    """**按文档批量预读** → `(state, {sha256: row})`。

    逐图往返会给每篇文档加 N 次 RDS round-trip（图爆多的文档 N 可上百）；一次
    `IN (...)` 拿全篇。空输入是合法的 `MISS`，不查库。
    """
    if not hashes:
        return MISS, {}
    try:
        uniq = sorted(set(h for h in hashes if h and not str(h).startswith("fallback_")))
        if not uniq:
            return MISS, {}
        ph = ",".join(["%s"] * len(uniq))
        sql = (f"SELECT image_sha256, status, image_category, funnel_stage, provenance "
               f"FROM {_TABLE} WHERE namespace=%s AND funnel_policy_version=%s "
               f"AND superseded_by IS NULL AND image_sha256 IN ({ph})")
        with conn.cursor() as cur:
            cur.execute(sql, [namespace(is_public), policy] + uniq)
            rows = cur.fetchall() or []
        out = {r[0]: {"status": r[1], "image_category": r[2],
                      "funnel_stage": r[3], "provenance": r[4]} for r in rows}
        return HIT, out
    except Exception as e:
        # 表缺失（1146）与库不可达在这里刻意**同等对待**：调用方要的信息是"权威判决拿不到"，
        # 而不是拿不到的原因。原因打日志，不进返回值。
        print(f"    ⚠️ [verdict-store] 判决记录不可读（降级为纯缓存行为）: {type(e).__name__}: {e}")
        return UNAVAILABLE, {}


def record_many(conn, entries: Sequence[Dict[str, Any]], is_public: bool, policy: str,
                vlm_model: Optional[str], doc_id: Optional[str],
                version_no: Optional[int], provenance: str = "fresh_decision") -> int:
    """写入判决，**first-writer-wins**。返回尝试写入的行数（不是实际新增行数）。

    并发语义必须钉死（codex MAJOR-4）：一个 `UnifiedExtractor` 跨文档并发、同篇内又有图片
    线程池，两个线程可能同时 miss、各付一次 VLM、得到**不同**结果后竞争同一主键。主键只
    防重复行，不定义谁赢。这里用空更新的 `ON DUPLICATE KEY UPDATE` 明确选 **first-writer-
    wins**：先落库的那条就是权威，后来者不覆盖。理由是"权威判决"的意义在于稳定，不在于新
    —— 让后写覆盖等于把并发时序变成判决的输入。

    `cache_promoted` 的行 `vlm_model` 必须传 None：存量 VLM 缓存没有模型/时间/首篇信息，
    拿当前值冒充历史事实会让这张表的溯源列不可信（codex MAJOR-3）。
    """
    rows = []
    for e in entries:
        h, res = e.get("image_sha256"), e.get("funnel_res") or {}
        if not h or str(h).startswith("fallback_") or not recordable(res):
            continue
        rows.append((h, namespace(is_public), policy, res.get("status"),
                     res.get("image_category") or "", _funnel_stage(res),
                     vlm_model, provenance, doc_id, version_no))
    if not rows:
        return 0
    try:
        sql = (f"INSERT INTO {_TABLE} (image_sha256, namespace, funnel_policy_version, "
               f"status, image_category, funnel_stage, vlm_model, provenance, "
               f"first_doc_id, first_version_no) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
               # 空更新 = first-writer-wins；主键冲突不报错也不覆盖
               f"ON DUPLICATE KEY UPDATE image_sha256=image_sha256")
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
        return len(rows)
    except Exception as e:
        print(f"    ⚠️ [verdict-store] 判决记录写入失败（不影响摄取）: {type(e).__name__}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


# 记录**永不**能把这些本轮判定盖掉。目前只有一个：本轮 VLM 主动认出敏感内容
# （公章/身份证/签名）。同款纪律仓库已有先例 —— `unified_extractor` 的 legacy 裸 MD5
# 回退白名单也刻意排除 QUARANTINE_SENSITIVE（"绝不回退，避免跳过敏感审计"）。
_UNPINNABLE_FRESH_STATUSES = frozenset({"QUARANTINE_SENSITIVE"})

# 被钉成这些 status 的资产会真正进入索引/答案，因此必须带得动内容；
# DISCARD_* 则不进 assets，无此要求。
_CONTENT_BEARING_STATUSES = frozenset({"ROUTE_TO_VECTOR", "ROUTE_TO_TEXT"})


def apply_verdict(funnel_res: Dict[str, Any], recorded: Dict[str, Any]) -> Dict[str, Any]:
    """把记录里的权威 status 盖回一份新算的 funnel 结果。

    用于"RDS 命中但缓存未命中"：caption 只能重新调 VLM 取（本表刻意不存它），但**图集
    不能因此改变** —— status 以记录为准。这正是 E 的主目标：已判过的图免疫制度漂移。

    只覆盖 status 与判决证据列，其余（caption/OCR/尺寸）留新算的值。

    **两条 pin 不得逾越的红线**（审查 P0-1 / P0-2，2026-07-26）：

    1. **本轮判 `QUARANTINE_SENSITIVE` ⇒ 永不 pin。** 否则一条陈旧的 ROUTE_TO_VECTOR 记录
       会把本轮刚认出的公章/证件图改回"入向量索引"，且 `node_detect_sensitive` 的
       "任一 asset 是 QUARANTINE ⇒ 整篇隔离" 永不触发 —— 敏感图静默进索引与钉钉卡片。
       安全判定只能收紧、不能被历史记录放松。
    2. **要钉成 content-bearing（VECTOR/TEXT）就必须真的带得动内容。** 若本轮结果连
       `visual_summary` 与 `ocr_text` 都没有（典型：Funnel-1 结构性弃图，压根没调过 VLM），
       钉回去只会产出**空壳资产**：检索侧零可检文本、内容覆写的 Path A/B 失去输入、
       而且这条"keep + 空 caption"还会被写进跨文档共享缓存永久固化。
       此时**放弃 pin**并置 `verdict_pin_declined`，由调用方走降级留痕。

    返回值总是新 dict；`verdict_pinned` / `verdict_pin_declined` 二者至多有一个为 True。
    """
    out = dict(funnel_res)
    fresh_status = funnel_res.get("status")

    if fresh_status in _UNPINNABLE_FRESH_STATUSES:
        out["verdict_pin_declined"] = f"fresh_{fresh_status}_wins"
        return out

    if (recorded["status"] in _CONTENT_BEARING_STATUSES
            and not (funnel_res.get("visual_summary") or funnel_res.get("ocr_text"))):
        out["verdict_pin_declined"] = "no_content_to_pin"
        return out

    out["status"] = recorded["status"]
    if recorded.get("image_category"):
        out["image_category"] = recorded["image_category"]
    out["reason"] = (f"{funnel_res.get('reason', '')} | verdict-store: status pinned to "
                     f"{recorded['status']} (recorded)").strip(" |")
    out["verdict_pinned"] = True
    return out


def pin_declined_note(doc_id, n_declined: int) -> str:
    """pin 被红线拒绝 ⇒ 该图集本轮**未**受权威记录保护，走 NEEDS_REVIEW 而不是静默定稿。"""
    return (f"[VERDICT-STORE] {doc_id or '?'}: {n_declined} 张图的权威判决**未被采用**"
            f"（本轮敏感判定优先，或记录为 keep 类但本轮结果无内容可承载）"
            f"⇒ 这些图的去留由本轮现判决定，可能与已服务版本不一致")


def unavailable_note(doc_id: Optional[str], n_undecided: int) -> str:
    """权威判决拿不到、缓存也没有 ⇒ 给 `partial_loss_notes` 的一行。

    走这个通道而不是静默继续：本次的 VLM 结果是在**无权威兜底**的情况下现判的，可能与
    该文档已服务版本的图集不一致。落 NEEDS_REVIEW 让人来看，而不是当成稳定定稿。
    """
    return (f"[VERDICT-STORE] {doc_id or '?'}: 判决记录不可用且 {n_undecided} 张图无同策略"
            f"缓存兜底 ⇒ 本次图集为现场重判结果，可能与已服务版本不一致（未静默定稿）")
