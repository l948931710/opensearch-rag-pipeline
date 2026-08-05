# -*- coding: utf-8 -*-
"""
routes/notices.py — 站内通知域：文档升版提醒的 console 侧读取与已读标记。

配套 `opensearch_pipeline/doc_update_notify.py`（日调批处理写 notice 行）与 schema/065。
本模块**只读 + 标已读**，不产生通知、不投递。

两条纪律（改动前先读）：
  1. **读时复核**：notice 行是批处理在【解析时点】按当时权威算出来的。用户当前是否仍
     可见该文档，必须在读取时用 `acl_policy.can_read_doc` + `resolve_doc_acl(strict=True)`
     重新判定；文档退役/隔离的历史通知同样隐藏。撤销/下架后不得再从站内信泄露标题。
  2. **无条件 401**：这是纯个人数据端点，identity 为 None 一律 401 —— **不随
     RAG_REQUIRE_AUTH 摆动**（那个 flag 管的是问答面的匿名准入，不是个人收件箱）。

⚠️ 不得定义或遮蔽任何被 tests monkeypatch 的 api 属性（规则见 routes/__init__.py）。
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from opensearch_pipeline.api import (
    Identity,
    _build_acl_ctx,
    _enforce_rate_limit,
    _kb_db,
    current_identity,
)
from opensearch_pipeline.qa_logger import _op_db

router = APIRouter()
logger = logging.getLogger(__name__)

# 读时复核的最大行数：从不开 console 的用户会攒下数百行 PENDING，逐行 can_read_doc +
# 标题查询的成本无界。截断到最近 50 行，未读数显示上限 99+（超出部分等用户读完再露出）。
_RECHECK_LIMIT = 50
_UNREAD_DISPLAY_CAP = 99


class NoticeItem(BaseModel):
    id: int
    doc_id: str
    title: str
    version_no: Optional[int] = None
    state: str
    created_at: Optional[str] = None


class NoticesResponse(BaseModel):
    items: List[NoticeItem] = Field(default_factory=list)
    unread_count: int = 0
    unread_capped: bool = False


class NoticesReadRequest(BaseModel):
    ids: Optional[List[int]] = None
    all: bool = False


def _require_identity(identity: Optional[Identity]) -> Identity:
    if identity is None or not getattr(identity, "user_id", ""):
        raise HTTPException(status_code=401, detail="请先登录")
    return identity


def _visible_rows(cur, identity: Identity, rows: List[tuple]) -> List[tuple]:
    """读时复核：只保留【当前】仍可见、且文档在架未隔离的行。

    复核失败（权威不可达 / ctx 构造不出）⇒ 该行隐藏（fail-closed），绝不放行。
    """
    if not rows:
        return []
    from opensearch_pipeline.access_grants import resolve_doc_acl
    from opensearch_pipeline.acl_policy import can_read_doc
    from opensearch_pipeline.doc_state import version_quarantined
    from opensearch_pipeline.retriever import _node_acl_flags

    doc_ids = sorted({r[1] for r in rows})
    ph = ",".join(["%s"] * len(doc_ids))
    cur.execute(
        f"""SELECT dm.doc_id, dm.title, dm.original_filename, dm.status,
                   dv.publish_status, dv.gate_status
            FROM {_kb_db()}.document_meta dm
            LEFT JOIN {_kb_db()}.document_version dv
              ON dv.doc_id = dm.doc_id AND dv.version_no = dm.current_version_no
            WHERE dm.doc_id IN ({ph})""", tuple(doc_ids))
    meta = {}
    for r in (cur.fetchall() or []):
        meta[r[0]] = {"title": r[1] or r[2] or r[0], "status": r[3],
                      "quarantined": version_quarantined(r[4], r[5])}
    try:
        grant, enforce = _node_acl_flags()
        acls = resolve_doc_acl(doc_ids, cur, strict=True)
        ctx = _build_acl_ctx(identity)
    except Exception as e:   # noqa: BLE001 — 权威不可达 ⇒ 整批隐藏（fail-closed）
        logger.warning("notices 读时复核不可达（本次全部隐藏）: %s", e)
        return []
    if ctx is None:
        return []
    out = []
    for row in rows:
        m = meta.get(row[1])
        acl = acls.get(row[1])
        if not m or str(m["status"] or "").lower() != "active" or m["quarantined"]:
            continue
        if acl is None or not can_read_doc(ctx, acl, grant_enabled=grant,
                                           enforce_enabled=enforce):
            continue
        out.append(row + (m["title"],))
    return out


@router.get("/api/kb/notices", response_model=NoticesResponse)
def kb_notices(request: Request, limit: int = 20,
               identity: Optional[Identity] = Depends(current_identity)):
    """当前用户的站内通知（文档升版提醒）。

    UI 契约（Sam 2026-08-04「不想太多通知影响 UIUX」）：console 只用顶栏铃铛 + 折叠面板，
    无未读时零装饰。故本端点只回列表与未读数，不含任何"请立即处理"的强提示语义。

    表未 apply（1146）⇒ 空列表降级（先部署后 apply 安全）。
    """
    ident = _require_identity(identity)
    _enforce_rate_limit(request, ident, scope="aux")
    try:
        limit = max(1, min(int(limit or 20), _RECHECK_LIMIT))
    except (TypeError, ValueError):
        limit = 20
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT n.id, n.doc_id, n.state, n.created_at, e.version_no
                    FROM {_op_db()}.doc_update_notice n
                    LEFT JOIN {_op_db()}.doc_update_event e ON e.id = n.event_id
                    WHERE n.user_id=%s AND n.channel='console'
                      AND n.state IN ('PENDING','READ')
                    ORDER BY n.id DESC LIMIT %s""",
                (ident.user_id, _RECHECK_LIMIT))
            rows = [(int(r[0]), r[1], r[2], r[3], r[4]) for r in (cur.fetchall() or [])]
            visible = _visible_rows(cur, ident, rows)
    except Exception as e:   # noqa: BLE001 — 表缺失/读失败 ⇒ 空列表，绝不 500 掉整个 console
        logger.warning("notices 读取降级为空（schema/065 未 apply?）: %s", e)
        return NoticesResponse()
    finally:
        conn.close()
    items = [NoticeItem(id=r[0], doc_id=r[1], state=r[2],
                        created_at=str(r[3]) if r[3] else None,
                        version_no=int(r[4]) if r[4] is not None else None,
                        title=r[5])
             for r in visible[:limit]]
    # 未读数只数【复核通过】的行：否则被撤权用户会看到"红点=1、列表为空"的自相矛盾，
    # 那本身也是一个弱存在性信号（他不该知道有一篇他看不见的文档更新了）。
    unread = sum(1 for r in visible if r[2] == "PENDING")
    return NoticesResponse(items=items,
                           unread_count=min(unread, _UNREAD_DISPLAY_CAP),
                           unread_capped=unread > _UNREAD_DISPLAY_CAP)


@router.post("/api/kb/notices/read")
def kb_notices_read(req: NoticesReadRequest, request: Request,
                    identity: Optional[Identity] = Depends(current_identity)):
    """标记已读（幂等）。只能标自己的行：WHERE 恒带 user_id，绝不按 id 裸更新。"""
    ident = _require_identity(identity)
    _enforce_rate_limit(request, ident, scope="aux")
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            if req.all:
                cur.execute(
                    f"""UPDATE {_op_db()}.doc_update_notice
                        SET state='READ', read_at=NOW()
                        WHERE user_id=%s AND channel='console' AND state='PENDING'""",
                    (ident.user_id,))
            elif req.ids:
                ids = [int(i) for i in req.ids][:200]
                ph = ",".join(["%s"] * len(ids))
                cur.execute(
                    f"""UPDATE {_op_db()}.doc_update_notice
                        SET state='READ', read_at=NOW()
                        WHERE user_id=%s AND channel='console' AND state='PENDING'
                          AND id IN ({ph})""",
                    (ident.user_id,) + tuple(ids))
            else:
                return {"updated": 0}
            updated = getattr(cur, "rowcount", 0) or 0
        conn.commit()
        return {"updated": updated}
    except Exception as e:   # noqa: BLE001 — 标已读失败不该阻断 console
        logger.warning("notices 标已读失败（忽略）: %s", e)
        return {"updated": 0}
    finally:
        conn.close()
