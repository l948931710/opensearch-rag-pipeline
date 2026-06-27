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
              "reset_chunks": 0, "skipped": False, "errors": []}
    if not get_config().rag.allowed_depts_acl:
        result["skipped"] = True
        return result

    from opensearch_pipeline.pipeline_nodes import _get_db_conn
    from opensearch_pipeline.access_grants import (
        resolve_allowed_depts, gate_by_permission, current_allowed_for_doc,
    )
    import json

    try:
        conn = _get_db_conn(select_db=True)
    except Exception as e:
        result["errors"].append(f"DB connect failed: {e}")
        return result

    try:
        with conn.cursor() as cur:
            # 1. authority → approved doc_ids → 经单一注入点 resolve（组码白名单/去重/稳定排序）
            cur.execute(f"SELECT DISTINCT doc_id FROM {_KB}.kb_access_request WHERE status='approved'")
            approved = [r[0] for r in cur.fetchall() if r and r[0]]
            result["approved"] = len(approved)
            resolved = resolve_allowed_depts(approved, cur) if approved else {}
            # 2. 纵深守卫：只有当前 active permission_level=='dept_internal' 的文档物化
            if resolved:
                ph = ",".join(["%s"] * len(resolved))
                cur.execute(
                    f"SELECT doc_id, GROUP_CONCAT(DISTINCT permission_level) FROM {_KB}.chunk_meta "
                    f"WHERE is_active=1 AND doc_id IN ({ph}) GROUP BY doc_id", tuple(resolved.keys()))
                resolved = gate_by_permission(resolved, {r[0]: r[1] for r in cur.fetchall()})
            # 3. 对账目标 = approved ∪ 仍有残留 allowed_depts 的文档（后者是 retract 候选）
            cur.execute(f"SELECT DISTINCT doc_id FROM {_KB}.chunk_meta "
                        f"WHERE is_active=1 AND allowed_depts IS NOT NULL")
            have_ad = {r[0] for r in cur.fetchall() if r and r[0]}
            targets = sorted(set(approved) | have_ad)[:_LIMIT]

            for doc_id in targets:
                try:
                    want = resolved.get(doc_id, [])      # gate 外 / 不再 approved → [] = 撤销
                    # current version + 2h PROCESSING 反抢锁（与 stage-3 loader 同约定）：
                    # current version 正在 stage-3 跑（PROCESSING < 2h）→ row 为空 → 本轮跳过、下轮再对
                    cur.execute(
                        f"SELECT dm.current_version_no FROM {_KB}.document_meta dm "
                        f"LEFT JOIN {_KB}.document_version dv "
                        f"  ON dv.doc_id=dm.doc_id AND dv.version_no=dm.current_version_no "
                        f"WHERE dm.doc_id=%s AND (dv.index_status IS NULL "
                        f"  OR dv.index_status!='PROCESSING' OR dv.updated_at < NOW() - INTERVAL 2 HOUR)",
                        (doc_id,))
                    row = cur.fetchone()
                    if not row:
                        continue
                    ver = int(row[0] or 1)
                    have = current_allowed_for_doc(cur, doc_id, ver)
                    if sorted(want) == have:
                        result["unchanged"] += 1
                        continue
                    if commit:
                        aj = json.dumps(want, ensure_ascii=False) if want else None
                        cur.execute(
                            f"UPDATE {_KB}.chunk_meta SET allowed_depts=%s, index_status='NOT_INDEXED' "
                            f"WHERE doc_id=%s AND version_no=%s AND is_active=1", (aj, doc_id, ver))
                        result["reset_chunks"] += cur.rowcount
                        conn.commit()                    # 逐文档提交（单文档失败不连累其余）
                    result["materialized" if want else "retracted"] += 1
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
