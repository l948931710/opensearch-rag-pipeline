# -*- coding: utf-8 -*-
"""access_grants.py — Phase D 跨部门检索授权的【唯一聚合注入点】。

事实来源（authority）= `fuling_knowledge.kb_access_request`（status='approved'）。
`chunk_meta.allowed_depts`（RDS）与 HA3 `allowed_depts` 字段都只是其【物化投影】——
任何写/重建路径（普通 ingestion / 升版 / re-chunk / HA3 rebuild / 回填脚本）都必须经本模块
解析 allowed_depts 再写，否则文档一旦重建就丢授权（Phase D 约束 2：单一注入点）。

授权值 = 被授权检索本文档的【用户组码】集合（= 申请人 managed_owner_depts，组代码粒度），
经写组码白名单（kb_authz._valid_owner_depts = retriever._VALID_ACL_GROUPS）校验 + 去重 + 稳定
排序；未知/非白名单码 **fail-closed 丢弃** 并显式 warning（约束 6：不静默吞）。授权按逻辑 doc_id
生效、自动跟随 current version（约束 3：调用方按 current-version 关系定位 chunk，本模块只按 doc_id 聚合）。
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, List

logger = logging.getLogger(__name__)


def _kb_db() -> str:
    """知识库库名（kb_access_request/chunk_meta/document_*/kb_acl_projection_outbox 所在库）；
    经 RAG_RDS_DATABASE 配置（STAGING=_stg）。惰性读 config（caller-cursor 路径也共用同一库名）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.database


def resolve_allowed_depts(doc_ids: Iterable[str], cursor) -> Dict[str, List[str]]:
    """聚合给定 doc_id 集的 approved 跨部门检索授权 → {doc_id: [组码...]}。

    - 只返回【有】approved 授权的 doc（无授权的 doc 不在返回里 → 调用方按空 [] 处理）。
    - 组码经白名单 + 去重 + 稳定排序；非白名单码丢弃并 per-doc warning。
    - cursor：调用方提供的 `fuling_knowledge` 库游标（连接/事务由调用方掌控，本模块不建池）。
    """
    ids = [d for d in dict.fromkeys(doc_ids) if d]   # 去重保序
    if not ids:
        return {}
    from opensearch_pipeline.kb_authz import sanitize_owner_depts, _valid_owner_depts
    whitelist = _valid_owner_depts()
    placeholders = ",".join(["%s"] * len(ids))
    cursor.execute(
        f"SELECT doc_id, requester_depts FROM {_kb_db()}.kb_access_request "
        f"WHERE status='approved' AND doc_id IN ({placeholders})",
        tuple(ids),
    )
    raw: Dict[str, List[str]] = {}
    for row in cursor.fetchall():
        doc_id, rdepts = row[0], row[1]
        if not doc_id:
            continue
        raw.setdefault(doc_id, []).extend((rdepts or "").split(","))

    out: Dict[str, List[str]] = {}
    for doc_id, codes in raw.items():
        clean = sanitize_owner_depts(codes)                 # 白名单内、去重、有序（单一净化口径）
        dropped = sorted({
            c.strip() for c in codes
            if c.strip() and c.strip() not in whitelist
        })
        if dropped:
            logger.warning(
                "kb_access_request doc=%s 含非白名单授权组码（fail-closed 丢弃、不放行）: %s",
                doc_id, dropped,
            )
        if clean:
            out[doc_id] = clean
    return out


def resolve_allowed_depts_one(doc_id: str, cursor) -> List[str]:
    """单文档便利：该 doc 的授权组码列表（无授权 → []）。"""
    if not doc_id:
        return []
    return resolve_allowed_depts([doc_id], cursor).get(doc_id, [])


def current_allowed_for_doc(cursor, doc_id: str, version_no: int) -> List[str]:
    """该 doc 指定版本 active chunk 现存的 allowed_depts 并集（去重、稳定排序）。

    用于 diff「应有 vs 现存」——decide 端点的同步标脏、allowed_depts_reconcile 对账、回填脚本
    共用同一口径（单一 diff 实现，避免三处漂移）。cursor 由调用方提供（连接/事务自掌）。
    """
    import json as _json
    cursor.execute(
        f"SELECT DISTINCT allowed_depts FROM {_kb_db()}.chunk_meta "
        "WHERE doc_id=%s AND version_no=%s AND is_active=1",
        (doc_id, version_no),
    )
    vals = set()
    for (ad,) in cursor.fetchall():
        if not ad:
            continue
        if isinstance(ad, list):
            vals.update(ad)
            continue
        # 单行坏 JSON 不得 abort 整篇 doc 的 ACL 投影/对账（否则该文档授权永久卡死、无法收敛）：
        # 跳过坏行并告警；少计 current 只会让 reconcile 朝"重投影"自愈方向，绝不越权扩散。
        try:
            parsed = _json.loads(ad)
        except (ValueError, TypeError):
            logger.warning("current_allowed_for_doc: 跳过 doc=%s v=%s 的坏 allowed_depts JSON: %r",
                           doc_id, version_no, str(ad)[:80])
            continue
        vals.update(parsed or [])
    return sorted(vals)


def gate_by_permission(
    allowed: Dict[str, List[str]], permission_by_doc: Dict[str, str]
) -> Dict[str, List[str]]:
    """纵深防御守卫：只有【当前/将投影版本】permission_level=='dept_internal' 的文档保留 allowed_depts。

    - allowed: `resolve_allowed_depts` 的输出 {doc_id: [组码...]}。
    - permission_by_doc: {doc_id: permission_level}——调用方按【权威来源】提供：ingestion / 重推用
      chunk 自身的 permission_level（= 将写入的新版本，权威）；回填用当前 active chunk 的 permission_level。
    - 非 dept_internal（restricted / public / 未知 None）→ 丢弃 + warning。

    为何要这层（审计 Step 4 backstop a）：一篇文档在提交时是 dept_internal、获批授权后被改判为
    restricted（重传到 restricted 路径→新版本），若不守卫，approved 行会把 allowed_depts 物化到
    restricted chunk 上。消费侧（retriever）已把 allowed_depts OR 项 AND-bind 到
    permission_level='dept_internal'（故今天不泄露），本守卫在【写入源头】再加一层：restricted 文档
    绝不携带 allowed_depts，杜绝任何未来旁路 filter 因残留字段而泄露。被丢弃的 approved 文档在回填
    materialize 循环里会以空 want 清空（= 撤销其残留物化）。
    """
    out: Dict[str, List[str]] = {}
    for doc_id, groups in allowed.items():
        if permission_by_doc.get(doc_id) == "dept_internal":
            out[doc_id] = groups
        else:
            logger.warning(
                "allowed_depts 守卫：doc=%s permission_level=%r != dept_internal → 不物化（纵深防御）",
                doc_id, permission_by_doc.get(doc_id),
            )
    return out


def materialize_doc_allowed_depts(cursor, doc_id: str, *, apply: bool = True) -> Dict[str, object]:
    """把单篇文档的 approved 授权【物化】到 chunk_meta.allowed_depts 投影——decide 端点与
    allowed_depts_reconcile 对账共用的【唯一写实现】（与上面 resolve/gate/current 读原语配套）。

    单一注入点，绝不重复手写。流程：
      1. 解析 current version，并施加 2h PROCESSING 反抢锁（与 stage-3 loader 同约定）——current
         version 正在 stage-3 跑（PROCESSING < 2h）→ 返回 skipped_locked、本轮不动，交对账下轮重对。
         **必须有这层**：否则在 stage-3 装载窗口内抢改 index_status，会被 stage-3 写回 INDEXED
         覆盖、而 HA3 仍是旧 ACL → chunk_meta 已等于 authority 致对账判 unchanged 不再重推的【自愈
         失败漂移】（Step 5 审计；decide 旧实现缺此守卫）。
      2. 从 authority 重解析该 doc 应有 allowed_depts，并按【该 version】permission_level **版本限定**
         gate 到 dept_internal（对账旧实现版本无关 GROUP_CONCAT → 双活混级误撤合法授权；此处统一）。
      3. 与现存投影 diff；变更则 UPDATE chunk_meta.allowed_depts + 标脏 index_status='NOT_INDEXED'
         （= stage-3 outbox，下次 drain 重推 HA3）。

    **不提交事务、不写 HA3**（连接/事务由调用方掌控；HA3 由 stage-3 drain 携带）。
    apply=False：只算 want/have/status、不写（对账只读预览）。

    Returns: {"status": "skipped"|"skipped_locked"|"unchanged"|"materialized"|"retracted",
              "reset_chunks": int, "version_no": int|None}
    """
    import json as _json
    if not doc_id:
        return {"status": "skipped", "reset_chunks": 0, "version_no": None}
    # 1. current version + 2h PROCESSING 反抢锁（与 stage-3 loader / 对账同约定）
    cursor.execute(
        f"SELECT dm.current_version_no FROM {_kb_db()}.document_meta dm "
        f"LEFT JOIN {_kb_db()}.document_version dv "
        "  ON dv.doc_id=dm.doc_id AND dv.version_no=dm.current_version_no "
        "WHERE dm.doc_id=%s AND (dv.index_status IS NULL "
        "  OR dv.index_status!='PROCESSING' OR dv.updated_at < NOW() - INTERVAL 2 HOUR)",
        (doc_id,),
    )
    row = cursor.fetchone()
    if not row:
        return {"status": "skipped_locked", "reset_chunks": 0, "version_no": None}
    ver = int(row[0] or 1)
    # 2. authority → 该版本 permission_level 版本限定 gate 到 dept_internal
    raw_want = resolve_allowed_depts_one(doc_id, cursor)
    cursor.execute(
        f"SELECT GROUP_CONCAT(DISTINCT permission_level) FROM {_kb_db()}.chunk_meta "
        "WHERE doc_id=%s AND version_no=%s AND is_active=1",
        (doc_id, ver),
    )
    prow = cursor.fetchone()
    want = gate_by_permission(
        {doc_id: raw_want}, {doc_id: (prow[0] if prow else None)},
    ).get(doc_id, [])
    # 3. diff vs 现存投影
    have = current_allowed_for_doc(cursor, doc_id, ver)
    if sorted(want) == have:
        return {"status": "unchanged", "reset_chunks": 0, "version_no": ver}
    status = "materialized" if want else "retracted"
    if not apply:
        return {"status": status, "reset_chunks": 0, "version_no": ver}
    aj = _json.dumps(want, ensure_ascii=False) if want else None
    cursor.execute(
        f"UPDATE {_kb_db()}.chunk_meta SET allowed_depts=%s, index_status='NOT_INDEXED' "
        "WHERE doc_id=%s AND version_no=%s AND is_active=1",
        (aj, doc_id, ver),
    )
    return {"status": status, "reset_chunks": cursor.rowcount, "version_no": ver}


# ── 投影 outbox：decide 同事务入队 + stage-3 幂等 drain（与全扫 reconcile 互补）──
def enqueue_acl_projection(cursor, doc_id: str, reason: str = "") -> None:
    """把一篇文档持久入队到 allowed_depts 投影 outbox（kb_acl_projection_outbox）。

    decide 端点在【改 kb_access_request.status 的同一事务】内调用——权威变更与投影意图原子提交：
    enqueue 失败 → 整笔回滚（绝不出现权威已改而无 outbox 行的撕裂；与「内联 materialize best-effort」
    不同，本入队是【持久保证】，刻意不吞异常）。一行一 doc（UNIQUE doc_id）：重复入队走 ON DUPLICATE
    复活（done_at=NULL, attempts=0），不留历史。**不提交事务**（连接/事务由调用方掌控）。
    """
    if not doc_id:
        return
    cursor.execute(
        f"INSERT INTO {_kb_db()}.kb_acl_projection_outbox (doc_id, reason) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE done_at=NULL, attempts=0, last_error=NULL, "
        "reason=VALUES(reason), updated_at=NOW()",
        (doc_id, (reason or "")[:64]),
    )


def drain_acl_projection_outbox(commit: bool = True, limit: int = 200) -> dict:
    """Drain 投影 outbox：逐文档幂等 materialize（标脏 chunk_meta + index_status='NOT_INDEXED'），
    成功/unchanged → 标 done_at；skipped_locked/失败 → attempts++ 留待下轮。**绝不抛**（失败进 errors）。

    与 allowed_depts_reconcile 互补：outbox 是 decide 受影响 doc 的【定向必达】重试，reconcile 是
    authority↔投影漂移的【全扫兜底】。stage-3 pre-drain 调用（HA3 重推由其后的 drain 循环携带）。
    flag 关 → 直接返回 skipped（投影路径全程惰性）。commit=False 为只读预览（不写 outbox/不标脏）。
    """
    from opensearch_pipeline.config import get_config

    result = {"processed": 0, "done": 0, "locked": 0, "failed": 0, "skipped": False, "errors": []}
    if not get_config().rag.allowed_depts_acl:
        result["skipped"] = True
        return result

    from opensearch_pipeline.db import _get_db_conn

    try:
        conn = _get_db_conn()
    except Exception as e:   # noqa: BLE001
        result["errors"].append(f"DB connect failed: {e}")
        return result
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, doc_id FROM {_kb_db()}.kb_acl_projection_outbox "
                "WHERE done_at IS NULL ORDER BY enqueued_at LIMIT %s",
                (int(limit),),
            )
            rows = [(r[0], r[1]) for r in cur.fetchall() if r and r[1]]
        for row_id, doc_id in rows:
            result["processed"] += 1
            try:
                with conn.cursor() as cur:
                    outcome = materialize_doc_allowed_depts(cur, doc_id, apply=commit)
                    if not commit:
                        conn.rollback()          # 预览：不改 outbox/不落标脏
                        continue
                    if outcome["status"] == "skipped_locked":
                        # current version 正在 stage-3 跑 → 本轮跳过、下轮再 drain（留 done_at=NULL）
                        cur.execute(
                            f"UPDATE {_kb_db()}.kb_acl_projection_outbox "
                            "SET attempts=attempts+1, last_error='skipped_locked', updated_at=NOW() WHERE id=%s",
                            (row_id,),
                        )
                        result["locked"] += 1
                    else:
                        # unchanged / materialized / retracted / skipped → 投影意图已落实 → 标 done
                        cur.execute(
                            f"UPDATE {_kb_db()}.kb_acl_projection_outbox "
                            "SET done_at=NOW(), last_error=NULL, updated_at=NOW() WHERE id=%s",
                            (row_id,),
                        )
                        result["done"] += 1
                conn.commit()
            except Exception as e:   # noqa: BLE001 — 单文档失败不抛、记 errors、attempts++ 待重试
                result["errors"].append(f"{doc_id}: {e}")
                result["failed"] += 1
                try:
                    conn.rollback()
                    with conn.cursor() as cur:
                        cur.execute(
                            f"UPDATE {_kb_db()}.kb_acl_projection_outbox "
                            "SET attempts=attempts+1, last_error=%s, updated_at=NOW() WHERE id=%s",
                            (str(e)[:512], row_id),
                        )
                    conn.commit()
                except Exception:   # noqa: BLE001 — 连记错误都失败 → 下轮 reconcile 兜底
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    finally:
        conn.close()
    return result
