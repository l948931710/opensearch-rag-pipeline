# -*- coding: utf-8 -*-
"""allowed_depts_reconcile.py — Phase D 跨部门授权投影对账（RDS 侧自愈；HA3 推送交 stage-3）。

从 authority（`fuling_knowledge.kb_access_request` status='approved'）重算每篇文档应有的
allowed_depts（经 `access_grants` 单一注入点 resolve + gate 到 dept_internal），与 chunk_meta
现存投影 diff，drift → 写 `chunk_meta.allowed_depts` + 重置 `index_status='NOT_INDEXED'`
（= stage-3 outbox；下次 stage-3 drain 重解析 authority→重嵌 dense+sparse→cmd=add 推 HA3，
清空/收窄 MULTI_STRING）。**双向**：materialize（approved 且 dept_internal）+ retract（不再
approved、或已改判非 dept_internal 的残留 → 清 NULL）。

与 `spot_checker.reconcile_*` 同型：逐文档提交、**绝不抛**、2h PROCESSING 反抢锁、LIMIT、flag 关
→ no-op。**只写 RDS，绝不写 HA3**（HA3 由既有 stage-3 drain 携带）。

定位：decide 端点的同步 dirty-mark 给最佳延迟下限；本对账每次 stage-3 pre-drain 跑一遍，兜住
「端点漏标脏（优雅降级吞写）」「绕过端点直接改库的 authority」两类漂移——authority 永远是唯一
事实源，投影任何时刻可从它全量重算。
"""
import logging

from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)

_KB = "fuling_knowledge"
_LIMIT = 200


def reconcile_allowed_depts(commit: bool = True) -> dict:
    """全量对账 approved authority → chunk_meta.allowed_depts 投影。

    commit=False 为只读预览（统计 drift，不写）。Returns 统计 dict，**绝不抛**（失败进 errors）。
    flag 关 → 直接返回 skipped（投影路径全程惰性，零写）。
    """
    result = {"approved": 0, "materialized": 0, "retracted": 0, "unchanged": 0,
              "reset_chunks": 0, "capped": False, "skipped": False, "errors": []}
    if not get_config().rag.allowed_depts_acl:
        result["skipped"] = True
        return result

    from opensearch_pipeline.pipeline_nodes import _get_db_conn
    from opensearch_pipeline.access_grants import materialize_doc_allowed_depts

    try:
        conn = _get_db_conn(select_db=True)
    except Exception as e:
        result["errors"].append(f"DB connect failed: {e}")
        return result

    try:
        with conn.cursor() as cur:
            # 1. authority → approved doc_ids（统计 + 候选并集；逐文档物化经单一注入点 helper）
            cur.execute(f"SELECT DISTINCT doc_id FROM {_KB}.kb_access_request WHERE status='approved'")
            approved = [r[0] for r in cur.fetchall() if r and r[0]]
            result["approved"] = len(approved)
            # 2. 候选 = approved ∪ 仍有残留 allowed_depts 的文档（后者是 retract 候选）
            cur.execute(f"SELECT DISTINCT doc_id FROM {_KB}.chunk_meta "
                        f"WHERE is_active=1 AND allowed_depts IS NOT NULL")
            have_ad = {r[0] for r in cur.fetchall() if r and r[0]}
            # 全量扫描候选，但按【实际漂移写】数封顶（_LIMIT）——unchanged 文档只读不占写预算，故高位
            # 漂移文档绝不会被一致文档挤出（旧实现 sorted(...)[:_LIMIT] 固定切片会饿死高位漂移；Step 5
            # 审计）。漂移文档本轮处理后下轮即变 unchanged，预算自然腾给后续漂移 → 自清、最终全覆盖。
            targets = sorted(set(approved) | have_ad)

            for doc_id in targets:
                if result["materialized"] + result["retracted"] >= _LIMIT:
                    result["capped"] = True
                    logger.info("allowed_depts reconcile 单轮写达上限 _LIMIT=%d，剩余漂移下轮续（自清不饿死）",
                                _LIMIT)
                    break
                try:
                    # helper：current version + 2h PROCESSING 反抢锁 → 版本限定 gate → diff → 写标脏。
                    # 不提交、不写 HA3；apply=commit 支持只读预览（commit=False 只统计漂移不写）。
                    outcome = materialize_doc_allowed_depts(cur, doc_id, apply=commit)
                    status = outcome["status"]
                    if status == "unchanged":
                        result["unchanged"] += 1
                    elif status in ("materialized", "retracted"):
                        result[status] += 1
                        result["reset_chunks"] += outcome["reset_chunks"]
                        if commit:
                            conn.commit()                # 逐文档提交（单文档失败不连累其余）
                    # skipped / skipped_locked：current version 正在 stage-3 跑 → 本轮跳过、下轮再对
                except Exception as e:                   # noqa: BLE001 — 单文档失败不抛、记 errors
                    result["errors"].append(f"{doc_id}: {e}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    except Exception as e:                               # noqa: BLE001 — 顶层亦绝不抛
        result["errors"].append(f"reconcile failed: {e}")
    finally:
        conn.close()
    return result
