# -*- coding: utf-8 -*-
"""
routes/kb_access.py — 跨部门检索授权域：授权申请/审批/撤销、已授权清单、
审批历史、我的申请，以及 kb_admin 的 dept_admin 写授权管理（Phase F）。

F-A2 结构债拆分（2026-07-01）：从 api.py 机械搬移，行为不变。api.py 底部
include_router 并 re-export 全部端点函数/模型（tests 直接调用 api.<endpoint> /
引用 api.Kb* 模型）。本模块**不得**定义或遮蔽任何被 tests monkeypatch 的
api 属性（规则见 routes/__init__.py）。
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from opensearch_pipeline.config import get_config
from opensearch_pipeline.qa_logger import _op_db
from opensearch_pipeline.request_context import get_request_id

# api 驻留共享件（模型/助手/依赖）。from-import 拷贝绑定在这里是安全的：
# 这些名字均不在 tests 的 api monkeypatch 清单内（见 routes/__init__.py）。
from opensearch_pipeline.api import (
    Identity,
    _enforce_rate_limit,
    _kb_can_manage,
    _kb_db,
    _require_kb_admin,
    _require_kb_console,
    current_identity,
    logger,
)

router = APIRouter()




# ── 跨部门文档检索授权申请（Phase C 记录层）─────────────────────────────────
# 申请人 = 部门管理员（在「全部门」浏览里对其他部门 dept_internal 文档发起）；
# 审批方 = 文档所属部门管理员（owner_dept ∈ 其 managed）或 kb_admin（_kb_can_manage）。
# ⚠️ 审批通过【只记录决策】，不立即放行检索——真正让申请部门检索到该文档 = Phase D
#    （把授予部门写进 allowed_depts 并接入 retriever HA3 ACL，不可逆 HA3 改动，单独授权）。
class KbAccessRequestSubmit(BaseModel):
    doc_id: str
    owner_dept: Optional[str] = None   # 客户端值仅参考；owner_dept 一律以 DB 现查为准
    reason: Optional[str] = None


class KbAccessDecisionRequest(BaseModel):
    id: str
    reason: Optional[str] = None


class KbAccessRequestSubmitResponse(BaseModel):
    id: str = ""
    status: str = "pending"
    already: bool = False


class KbAccessDecisionResponse(BaseModel):
    id: str = ""
    status: str = ""
    decided: bool = False
    already: bool = False


class KbAccessRequestItem(BaseModel):
    id: str = ""
    doc_id: str = ""
    doc_title: str = ""
    owner_dept: str = ""
    requester_dept: str = ""
    requester_name: str = ""
    permission_level: str = "dept_internal"
    reason: str = ""
    created_at: str = ""


class KbAccessRequestListResponse(BaseModel):
    items: List[KbAccessRequestItem] = Field(default_factory=list)


class KbAccessGrantItem(BaseModel):
    """审批方侧的【已放行】跨部门授权（status='approved'）——供「已授权清单」展示 + 撤销。"""
    id: str = ""
    doc_id: str = ""
    doc_title: str = ""
    owner_dept: str = ""
    requester_dept: str = ""        # 获授权检索的组码（requester_depts）
    requester_name: str = ""
    permission_level: str = "dept_internal"
    reason: str = ""
    decided_at: str = ""            # 批准时间（授权生效时点）


class KbAccessGrantListResponse(BaseModel):
    items: List[KbAccessGrantItem] = Field(default_factory=list)


# ── Phase F：成员/角色管理（kb_admin 维护 dept_admin 写授权；三分授权 读≠管理≠授权）──
class KbAdminItem(BaseModel):
    user_id: str = ""
    user_name: str = ""
    role: str = ""                                            # dept_admin / kb_admin
    managed_owner_depts: List[str] = Field(default_factory=list)  # dept_admin 显式授权；kb_admin=全部(空数组表示全量)


class KbAdminListResponse(BaseModel):
    items: List[KbAdminItem] = Field(default_factory=list)
    grantable_owner_depts: List[str] = Field(default_factory=list)  # 表单可选项（写白名单单一来源）


class KbAdminGrantRequest(BaseModel):
    user_id: str = ""                                         # 钉钉 staffId
    user_name: str = ""
    owner_depts: List[str] = Field(default_factory=list)      # 授予可管理的 owner_dept（权威全集，提交即覆盖）
    note: str = ""


class KbAdminRevokeRequest(BaseModel):
    user_id: str = ""
    owner_dept: str = ""                                      # 空 = 撤销该用户全部授权并降级 employee


class KbAdminGrantResponse(BaseModel):
    user_id: str = ""
    role: str = ""
    managed_owner_depts: List[str] = Field(default_factory=list)
    ok: bool = True


class MyAccessRequestItem(BaseModel):
    id: str = ""
    doc_id: str = ""
    doc_title: str = ""
    owner_dept: str = ""
    requester_dept: str = ""        # 本次授予的组码（requester_depts）
    status: str = ""               # pending / approved / rejected
    sync_state: str = ""           # n/a | pending_sync（已批准·待同步）| projected（已放行）
    reason: str = ""
    created_at: str = ""
    decided_at: str = ""


class MyAccessRequestListResponse(BaseModel):
    items: List[MyAccessRequestItem] = Field(default_factory=list)


@router.post("/api/kb/access-requests", response_model=KbAccessRequestSubmitResponse)
def kb_access_request_submit(req: KbAccessRequestSubmit, request: Request,
                             identity: Optional[Identity] = Depends(current_identity)):
    """部门管理员对【其他部门】dept_internal 文档发起检索授权申请。

    硬规则（fail-closed）：只 dept_internal 可申请（public 本就可读、restricted 不可外露）；
    本部门文档无需申请；kb_admin 直接管理无需申请；同 (doc, 申请人) 已有 pending → 幂等返回。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    if kb.role == ROLE_KB_ADMIN:
        raise HTTPException(status_code=400, detail="知识库管理员可直接管理全部文档，无需申请授权")
    managed = set(managed_owner_depts(kb))
    if not managed:
        raise HTTPException(status_code=403, detail="无管理部门，无法代部门申请授权")
    assert_metadata_write_allowed("kb_access_request_submit", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    owner_dept = ""
    requester_depts = ",".join(sorted(managed))
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT owner_dept, permission_level, status FROM {_kb_db()}.document_meta "
                            "WHERE doc_id=%s LIMIT 1", (req.doc_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, perm, status = (row[0] or ""), (row[1] or ""), (row[2] or "active")
                if str(status).lower() != "active":
                    raise HTTPException(status_code=400, detail="该文档非在线状态，无法申请")
                if perm == "public":
                    raise HTTPException(status_code=400, detail="公开文档全公司可检索，无需申请")
                if perm != "dept_internal":
                    raise HTTPException(status_code=403, detail="该文档不可申请授权")
                if owner_dept in managed:
                    raise HTTPException(status_code=400, detail="本部门文档无需申请")
                # 幂等：已有同 (doc, 申请人) pending → 返回既有，不重复入队
                cur.execute(f"SELECT id FROM {_kb_db()}.kb_access_request "
                            "WHERE doc_id=%s AND requester_id=%s AND status='pending' LIMIT 1",
                            (req.doc_id, kb.user_id))
                ex = cur.fetchone()
                if ex:
                    conn.commit()
                    return KbAccessRequestSubmitResponse(id=str(ex[0]), status="pending", already=True)
                cur.execute(
                    f"INSERT INTO {_kb_db()}.kb_access_request "
                    "(doc_id, owner_dept, requester_id, requester_name, requester_depts, reason, status) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'pending')",
                    (req.doc_id, owner_dept, kb.user_id, kb.name, requester_depts, (req.reason or "")[:512]),
                )
                new_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_access_request_submit 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"提交授权申请失败 (trace: {trace_id})")
    write_audit(doc_id=req.doc_id, version_no=None, action_type="ACCESS_REQUEST_SUBMIT",
                operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                message=f"owner={owner_dept} requester_depts={requester_depts}")
    return KbAccessRequestSubmitResponse(id=str(new_id), status="pending", already=False)


@router.get("/api/kb/access-requests", response_model=KbAccessRequestListResponse)
def kb_access_requests_list(request: Request,
                            identity: Optional[Identity] = Depends(current_identity)):
    """审批方待办：列出【我有权审批】的 pending 申请（owner_dept ∈ 我 managed；kb_admin 全部）。只读。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    clause, params = "", []
    if kb.role != ROLE_KB_ADMIN:
        owners = managed_owner_depts(kb)
        if not owners:
            return KbAccessRequestListResponse(items=[])
        clause = "AND r.owner_dept IN (" + ",".join(["%s"] * len(owners)) + ")"
        params = list(owners)
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT r.id, r.doc_id, m.title, r.owner_dept, r.requester_depts,
                           r.requester_name, m.permission_level, r.reason, r.created_at
                    FROM {_kb_db()}.kb_access_request r
                    JOIN {_kb_db()}.document_meta m ON m.doc_id = r.doc_id
                    WHERE r.status='pending' {clause}
                    ORDER BY r.created_at DESC
                    LIMIT 100
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_access_requests_list 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"授权申请队列查询失败 (trace: {trace_id})")
    items = [
        KbAccessRequestItem(
            id=str(r[0]), doc_id=r[1] or "", doc_title=r[2] or "", owner_dept=r[3] or "",
            requester_dept=r[4] or "", requester_name=r[5] or "",
            permission_level=r[6] or "dept_internal", reason=r[7] or "",
            created_at=str(r[8]) if r[8] else "",
        )
        for r in rows
    ]
    return KbAccessRequestListResponse(items=items)


@router.get("/api/kb/access-grants", response_model=KbAccessGrantListResponse)
def kb_access_grants_list(request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """审批方侧：列出【我可管理】文档上现行有效（status='approved'）的跨部门检索授权，供撤销。

    owner_dept ∈ 我 managed（kb_admin 全部）。与待审批队列（pending）区分：此处是已放行的【存量】，
    撤销动作走 POST /api/kb/access-requests/revoke。只读。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    clause, params = "", []
    if kb.role != ROLE_KB_ADMIN:
        owners = managed_owner_depts(kb)
        if not owners:
            return KbAccessGrantListResponse(items=[])
        clause = "AND r.owner_dept IN (" + ",".join(["%s"] * len(owners)) + ")"
        params = list(owners)
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT r.id, r.doc_id, m.title, r.owner_dept, r.requester_depts,
                           r.requester_name, m.permission_level, r.reason, r.decided_at
                    FROM {_kb_db()}.kb_access_request r
                    JOIN {_kb_db()}.document_meta m ON m.doc_id = r.doc_id
                    WHERE r.status='approved' {clause}
                    ORDER BY r.decided_at DESC
                    LIMIT 200
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_access_grants_list 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"已授权清单查询失败 (trace: {trace_id})")
    items = [
        KbAccessGrantItem(
            id=str(r[0]), doc_id=r[1] or "", doc_title=r[2] or "", owner_dept=r[3] or "",
            requester_dept=r[4] or "", requester_name=r[5] or "",
            permission_level=r[6] or "dept_internal", reason=r[7] or "",
            decided_at=str(r[8]) if r[8] else "",
        )
        for r in rows
    ]
    return KbAccessGrantListResponse(items=items)


# ─────────────────────────────────────────────────────────────────────────────
# 审批历史 (Approval History) — 只读聚合，四条审批流的【历史决策】合并时间线。
#   dept_admin：见本部门的 access（跨部门检索授权）+ contribution（知识贡献采纳）历史；
#   kb_admin：见全库四类（再加 upload 上传审批 + admin_grant 成员/角色授权，取自审计日志）。
# 无需改表：kb_access_request/kb_contribution 有 decided_by/decided_at(reviewed_*)+备注；
# 上传审批 & 成员授权的决策元数据在 append-only kb_audit_log（operator_id/created_at/action_type）。
# 镜像 /api/kb/insights 的多子查询 fail-open：各子查询独立 try/except + fails 计数，全失败→诚实 500。
# ─────────────────────────────────────────────────────────────────────────────
_APPROVAL_HISTORY_LIMIT = 200
# 决策时间统一转北京时（与 Phase E 看板同口径）；tz 表缺失时 COALESCE 回退原值（Pacific）不致排序崩。
_TZ_PACIFIC_TO_BJ = "'America/Los_Angeles','Asia/Shanghai'"


def _parse_admin_target(msg: str) -> str:
    """从 KB_ADMIN_GRANT/REVOKE 审计 message 抽目标用户 id（best-effort，格式由我方代码固定）。

    grant：'grant dept_admin <uid> → <depts>'；revoke：'revoke <uid> owner=<..> demoted=..'。
    """
    try:
        parts = (msg or "").split()
        if len(parts) >= 3 and parts[0] == "grant":
            return parts[2]
        if len(parts) >= 2 and parts[0] == "revoke":
            return parts[1]
    except Exception:
        pass
    return ""


class KbApprovalHistoryItem(BaseModel):
    kind: str = ""            # 'access' | 'contribution' | 'upload' | 'admin_grant'
    action: str = ""          # approved|rejected|revoked|accepted|granted
    title: str = ""           # 文档标题 / 贡献问题 / 目标用户
    owner_dept: str = ""      # 作用域部门（contribution=category_dept；admin_grant 无）
    subject: str = ""         # requester_name / author_name / 目标 uid（已存展示名，与队列一致，不脱敏）
    detail: str = ""          # 理由/备注 —— 跨用户自由文本，已脱敏
    extra: str = ""           # 次要状态：contribution 的 ingestion_status
    decided_by: str = ""      # 操作者 staffId
    decided_by_name: str = ""  # 操作者展示名（best-effort，缺则回退 uid）
    decided_at: str = ""      # 北京时间 'YYYY-MM-DD HH:MM:SS'


class KbApprovalHistoryResponse(BaseModel):
    items: List[KbApprovalHistoryItem] = Field(default_factory=list)


@router.get("/api/kb/approval-history", response_model=KbApprovalHistoryResponse)
def kb_approval_history(request: Request,
                        identity: Optional[Identity] = Depends(current_identity)):
    """审批历史（只读聚合，owner 作用域）。dept_admin 见本部门 access+contribution；kb_admin 见全库四类。

    各子查询独立降级（单流取数失败只让该流缺失，不拖垮整块）；跑过的子查询【全部】失败 → 诚实 500。
    跨用户自由文本（申请理由/贡献问题/审批备注/审计 message）一律 redact_query_text 脱敏。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    is_admin = (kb.role == ROLE_KB_ADMIN)
    scope_owner, scope_owner_params = "", []
    scope_cat, scope_cat_params = "", []
    if not is_admin:
        owners = managed_owner_depts(kb)
        if not owners:
            return KbApprovalHistoryResponse(items=[])   # 无管理部门 → 空（fail-closed，绝不当全量）
        ph = ",".join(["%s"] * len(owners))
        scope_owner = f"AND r.owner_dept IN ({ph})"; scope_owner_params = list(owners)
        scope_cat = f"AND category_dept IN ({ph})"; scope_cat_params = list(owners)
    lim = _APPROVAL_HISTORY_LIMIT
    from opensearch_pipeline import contribution as _C

    def _rq(t: Optional[str]) -> str:   # 跨用户自由文本脱敏兜底（失败即安全空）
        try:
            return _C.redact_query_text(t or "")
        except Exception:
            return ""

    out: List[KbApprovalHistoryItem] = []
    op_ids: set = set()
    fails = 0
    ran = 0
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_approval_history 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"审批历史查询失败 (trace: {trace_id})")
    try:
        # 共享 buffered 游标跑多条子查询（pymysql 默认 Cursor，非 SSCursor）：某条异常后结果已缓冲，
        # 下一句 execute 不会 "Commands out of sync"。与 /api/kb/insights 同型。
        with conn.cursor() as cur:
            # 1) access —— kb_access_request 的已决行（两角色，owner_dept 作用域）
            ran += 1
            try:
                cur.execute(
                    "SELECT r.doc_id, m.title, r.owner_dept, r.requester_depts, r.requester_name,"
                    " r.status, r.reason, r.decision_note, r.decided_by,"
                    f" COALESCE(CONVERT_TZ(r.decided_at,{_TZ_PACIFIC_TO_BJ}), r.decided_at)"
                    f" FROM {_kb_db()}.kb_access_request r"
                    f" JOIN {_kb_db()}.document_meta m ON m.doc_id = r.doc_id"
                    " WHERE r.status IN ('approved','rejected','revoked') " + scope_owner +
                    " ORDER BY r.decided_at DESC LIMIT %s",
                    tuple(scope_owner_params + [lim]))
                for x in cur.fetchall():
                    out.append(KbApprovalHistoryItem(
                        kind="access", action=(x[5] or ""), title=(x[1] or ""),
                        owner_dept=(x[2] or ""), subject=(x[4] or ""),
                        detail=(_rq(x[6]) or _rq(x[7])), decided_by=(x[8] or ""),
                        decided_at=str(x[9]) if x[9] else ""))
                    if x[8]:
                        op_ids.add(x[8])
            except Exception as e:
                fails += 1; logger.warning("approval_history access 失败: %s", e)
            # 2) contribution —— kb_contribution 的已决行（两角色，category_dept 作用域；库=_op_db）
            ran += 1
            try:
                cur.execute(
                    "SELECT question, category_dept, author_name, review_status, review_note,"
                    " ingestion_status, reviewed_by,"
                    f" COALESCE(CONVERT_TZ(reviewed_at,{_TZ_PACIFIC_TO_BJ}), reviewed_at)"
                    f" FROM {_op_db()}.kb_contribution"
                    " WHERE review_status IN ('accepted','rejected') " + scope_cat +
                    " ORDER BY reviewed_at DESC LIMIT %s",
                    tuple(scope_cat_params + [lim]))
                for x in cur.fetchall():
                    out.append(KbApprovalHistoryItem(
                        kind="contribution", action=(x[3] or ""), title=_rq(x[0]),
                        owner_dept=(x[1] or ""), subject=(x[2] or ""), detail=_rq(x[4]),
                        extra=(x[5] or ""), decided_by=(x[6] or ""),
                        decided_at=str(x[7]) if x[7] else ""))
                    if x[6]:
                        op_ids.add(x[6])
            except Exception as e:
                fails += 1; logger.warning("approval_history contribution 失败: %s", e)
            if is_admin:
                # 3) upload —— 上传审批（仅 kb_admin，取自 kb_audit_log；APPROVE/REJECT 是上传专用 action）
                ran += 1
                try:
                    cur.execute(
                        "SELECT a.doc_id, m.title, m.owner_dept, a.action_type, a.operator_id,"
                        f" COALESCE(CONVERT_TZ(a.created_at,{_TZ_PACIFIC_TO_BJ}), a.created_at), a.message"
                        f" FROM {_kb_db()}.kb_audit_log a"
                        f" LEFT JOIN {_kb_db()}.document_meta m ON m.doc_id = a.doc_id"
                        " WHERE a.operator_type='user' AND a.action_type IN ('APPROVE','REJECT')"
                        " ORDER BY a.created_at DESC LIMIT %s", (lim,))
                    for x in cur.fetchall():
                        act = "approved" if (x[3] or "") == "APPROVE" else "rejected"
                        out.append(KbApprovalHistoryItem(
                            kind="upload", action=act, title=(x[1] or x[0] or ""),
                            owner_dept=(x[2] or ""), subject="", detail=_rq(x[6]),
                            decided_by=(x[4] or ""), decided_at=str(x[5]) if x[5] else ""))
                        if x[4]:
                            op_ids.add(x[4])
                except Exception as e:
                    fails += 1; logger.warning("approval_history upload 失败: %s", e)
                # 4) admin_grant —— 成员/角色授权（仅 kb_admin，取自 kb_audit_log）
                ran += 1
                try:
                    cur.execute(
                        "SELECT action_type, operator_id,"
                        f" COALESCE(CONVERT_TZ(created_at,{_TZ_PACIFIC_TO_BJ}), created_at), message"
                        f" FROM {_kb_db()}.kb_audit_log"
                        " WHERE operator_type='user' AND action_type IN ('KB_ADMIN_GRANT','KB_ADMIN_REVOKE')"
                        " ORDER BY created_at DESC LIMIT %s", (lim,))
                    for x in cur.fetchall():
                        act = "granted" if (x[0] or "") == "KB_ADMIN_GRANT" else "revoked"
                        tgt = _parse_admin_target(x[3] or "")
                        out.append(KbApprovalHistoryItem(
                            kind="admin_grant", action=act, title=tgt, subject=tgt,
                            detail=_rq(x[3]), decided_by=(x[1] or ""),
                            decided_at=str(x[2]) if x[2] else ""))
                        if x[1]:
                            op_ids.add(x[1])
                except Exception as e:
                    fails += 1; logger.warning("approval_history admin_grant 失败: %s", e)
            # 操作者 staffId → 展示名（best-effort，enrichment；失败不计入 fails、回退 uid）
            if op_ids:
                try:
                    ph = ",".join(["%s"] * len(op_ids))
                    cur.execute(f"SELECT user_id, user_name FROM {_kb_db()}.user_role WHERE user_id IN ({ph})",
                                tuple(op_ids))
                    names = {r0: (r1 or "") for (r0, r1) in cur.fetchall()}
                    for it in out:
                        it.decided_by_name = names.get(it.decided_by, "") or it.decided_by
                except Exception as e:
                    logger.warning("approval_history 操作者名解析失败: %s", e)
                    for it in out:
                        it.decided_by_name = it.decided_by
    finally:
        conn.close()
    if ran and fails >= ran:   # 跑过的子查询全失败 = 连接级故障：诚实 500，而非 all-empty 伪装无历史
        trace_id = get_request_id()
        logger.error("kb_approval_history 全部子查询失败 [trace=%s]", trace_id)
        raise HTTPException(status_code=500, detail=f"审批历史查询失败 (trace: {trace_id})")
    # 跨源合并按北京时字符串倒序（ISO 'YYYY-MM-DD HH:MM:SS' 字典序=时序）；空时间沉底。
    out.sort(key=lambda r: r.decided_at or "", reverse=True)
    return KbApprovalHistoryResponse(items=out[:lim])


@router.get("/api/kb/my-access-requests", response_model=MyAccessRequestListResponse)
def kb_my_access_requests(request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """申请人侧：列出【我提交】的授权申请 + 派生同步态。只读。

    派生（不存列，Phase D constraint 7）：approved 且该 doc current-version active chunk 全
    INDEXED 且 chunk_meta.allowed_depts ⊇ 本次授予组码 → 'projected'（已放行）；否则
    'pending_sync'（已批准·待同步）。pending/rejected → 'n/a'。flag 关时投影恒空 → approved
    恒显 pending_sync（如实，未真正放行）。INDEXED 在生产 parity-verify 开时 = HA3 物理存在态。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    items: List[MyAccessRequestItem] = []
    try:
        from opensearch_pipeline.db import _get_db_conn
        from opensearch_pipeline.access_grants import current_allowed_for_doc
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT r.id, r.doc_id, m.title, r.owner_dept, r.requester_depts, r.status,
                           r.reason, r.created_at, r.decided_at, m.current_version_no
                    FROM {_kb_db()}.kb_access_request r
                    LEFT JOIN {_kb_db()}.document_meta m ON m.doc_id = r.doc_id
                    WHERE r.requester_id = %s
                    ORDER BY r.created_at DESC
                    LIMIT 100
                    """,
                    (kb.user_id,),
                )
                rows = cur.fetchall()
                for r in rows:
                    doc_id = r[1] or ""
                    rdepts = r[4] or ""
                    status = r[5] or ""
                    sync = "n/a"
                    if status == "approved" and doc_id:
                        try:
                            ver = int(r[9] or 1)
                            cur.execute(
                                "SELECT COUNT(*), SUM(index_status='INDEXED') "
                                f"FROM {_kb_db()}.chunk_meta "
                                "WHERE doc_id=%s AND version_no=%s AND is_active=1", (doc_id, ver))
                            cnt_row = cur.fetchone() or (0, 0)
                            cnt = int(cnt_row[0] or 0)
                            n_idx = int(cnt_row[1] or 0)
                            allowed = set(current_allowed_for_doc(cur, doc_id, ver))
                            granted = {g.strip() for g in rdepts.split(",") if g.strip()}
                            projected = bool(cnt and cnt == n_idx and granted and granted <= allowed)
                            sync = "projected" if projected else "pending_sync"
                        except Exception as _re:   # noqa: BLE001 — 单行派生失败（如脏 allowed_depts JSON）→
                            # 降级该行为 n/a 并继续，绝不连累整张列表 500（与 reconcile 逐文档兜底同型）。
                            logger.warning("my-access 同步态派生失败 doc=%s，降级 n/a: %s", doc_id, _re)
                            sync = "n/a"
                    items.append(MyAccessRequestItem(
                        id=str(r[0]), doc_id=doc_id, doc_title=r[2] or "", owner_dept=r[3] or "",
                        requester_dept=rdepts, status=status, sync_state=sync, reason=r[6] or "",
                        created_at=str(r[7]) if r[7] else "", decided_at=str(r[8]) if r[8] else ""))
        finally:
            conn.close()
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_my_access_requests 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"我的授权申请查询失败 (trace: {trace_id})")
    return MyAccessRequestListResponse(items=items)


def _kb_access_decide(req: KbAccessDecisionRequest, request: Request,
                      identity: Optional[Identity], decision: str,
                      *, from_status: str = "pending") -> KbAccessDecisionResponse:
    """审批 / 撤销一条申请。授权：文档所属部门管理员（_kb_can_manage）或 kb_admin。

    状态机（单向）：pending→approved / pending→rejected（审批）；approved→revoked（撤销已批授权）。
    `from_status` = 本次操作要求的前态——非该前态 → 幂等返回（不重复改、不误转）。

    改 kb_access_request.status，并（flag 开）在同事务内经 materialize_doc_allowed_depts 把该 doc 的
    allowed_depts 投影标脏。撤销（approved→revoked）后该行不再 status='approved' → 重算时被剔除 →
    投影收窄/清空 → stage-3 下次 drain 从 HA3 收回（这正是「无撤销路径」缺口的修复）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config
    if not req.id:
        raise HTTPException(status_code=400, detail="缺少 id")
    assert_metadata_write_allowed(f"kb_access_request_{decision}", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    owner_dept = ""
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT owner_dept, status, doc_id FROM {_kb_db()}.kb_access_request "
                            "WHERE id=%s FOR UPDATE", (req.id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="申请不存在")
                owner_dept, status, doc_id = (row[0] or ""), (row[1] or ""), (row[2] or "")
                # 审批权：文档所属部门管理员（owner_dept ∈ managed）或 kb_admin
                if not _kb_can_manage(kb, owner_dept):
                    raise HTTPException(status_code=403, detail="无权操作该申请（非文档所属部门管理员）")
                if status != from_status:
                    conn.commit()       # 幂等：非目标前态（已决 / 非 approved）→ 返回既有态
                    return KbAccessDecisionResponse(id=req.id, status=status, decided=False, already=True)
                cur.execute(f"UPDATE {_kb_db()}.kb_access_request "
                            "SET status=%s, decided_by=%s, decided_at=NOW(), decision_note=%s WHERE id=%s",
                            (decision, kb.user_id, (req.reason or "")[:512], req.id))
                # Phase D（flag 开）：同事务内把该 doc 的 allowed_depts 投影【标脏】——经共享注入点
                # materialize_doc_allowed_depts 重算 authority（含刚改的本行 status，读己写：approve→纳入、
                # reject/revoke→剔除）→ 版本限定 gate 到 dept_internal → diff →（变更）写 chunk_meta.allowed_depts +
                # index_status='NOT_INDEXED'，stage-3 下次 drain 据此重推 HA3。helper 内置 2h PROCESSING
                # 反抢锁（与对账同口径）：current version 正在 stage-3 装载时跳过标脏，交对账下轮重对，杜绝
                # 标脏被 stage-3 写回 INDEXED 覆盖而 HA3 仍旧 ACL 的自愈失败漂移。**绝不写 HA3 / 不
                # re-embed**（重活留给 stage-3）。flag 关 = no-op；失败只记日志、**不回滚 status**
                # （allowed_depts_reconcile 每轮 stage-3 兜底）。
                if get_config().rag.allowed_depts_acl and doc_id:
                    from opensearch_pipeline.access_grants import (
                        enqueue_acl_projection, materialize_doc_allowed_depts,
                    )
                    # 持久入队（同事务、不吞异常）：权威变更与投影意图原子提交——enqueue 失败则整笔回滚，
                    # 绝不出现「权威已改而无 outbox 行」的撕裂。stage-3 outbox drain 据此定向幂等重试至成功。
                    enqueue_acl_projection(cur, doc_id, reason=decision)
                    # 内联标脏 = best-effort 快路径：成功则本轮 stage-3 即可重推；抛/skipped_locked → 上面
                    # 的 outbox 行兜底（+ allowed_depts_reconcile 全扫）。失败只记日志、**不回滚 status**。
                    try:
                        materialize_doc_allowed_depts(cur, doc_id)
                    except Exception as _pe:
                        logger.warning("decide allowed_depts 内联标脏失败（outbox+reconciler 兜底）doc=%s: %s",
                                       doc_id, _pe)
                # 审计行入【同事务】（commit 前、同 cursor）：与 status 变更 + outbox 入队原子提交（B1）。
                write_audit(doc_id=doc_id, version_no=None, action_type=f"ACCESS_REQUEST_{decision.upper()}",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"req_id={req.id} owner={owner_dept}", cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_access_request_%s 失败 [trace=%s]: %s", decision, trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"操作失败 (trace: {trace_id})")
    return KbAccessDecisionResponse(id=req.id, status=decision, decided=True, already=False)


@router.post("/api/kb/access-requests/approve", response_model=KbAccessDecisionResponse)
def kb_access_request_approve(req: KbAccessDecisionRequest, request: Request,
                              identity: Optional[Identity] = Depends(current_identity)):
    """通过申请（仅记录决策；真正放行检索 = Phase D allowed_depts）。"""
    return _kb_access_decide(req, request, identity, decision="approved")


@router.post("/api/kb/access-requests/reject", response_model=KbAccessDecisionResponse)
def kb_access_request_reject(req: KbAccessDecisionRequest, request: Request,
                             identity: Optional[Identity] = Depends(current_identity)):
    """驳回申请。"""
    return _kb_access_decide(req, request, identity, decision="rejected")


@router.post("/api/kb/access-requests/revoke", response_model=KbAccessDecisionResponse)
def kb_access_request_revoke(req: KbAccessDecisionRequest, request: Request,
                             identity: Optional[Identity] = Depends(current_identity)):
    """撤销一条【已批准】的跨部门授权（approved→revoked）。授权同审批方（owner-dept 管理员 / kb_admin）。

    复用 decide 机制：同事务把该 doc 的 allowed_depts 重算（剔除本撤销行、保留其余 approved 授权）→
    收窄/清空投影 + 标脏，stage-3 下次 drain 从 HA3 收回放行。这是「approved 无法经 API 撤销」缺口的
    一等修复——此前 reject 对 approved 行因 status!='pending' 幂等无效，只能直接改库 + 等夜间对账。
    撤销后申请人可重新申请（revoked 同 rejected，不阻 submit 去重——后者只挡 pending）。
    """
    return _kb_access_decide(req, request, identity, decision="revoked", from_status="approved")


# ═══════════════════════════════════════════════════════════════
# Phase F — 成员/角色管理（kb_admin 专属）：维护 dept_admin 角色 + 其 owner_dept 写授权。
#   权威表：fuling_knowledge.user_role.role + dept_admin_grant（resolve_kb_identity 现查,撤销即时生效）。
#   三分授权：读组(acl_groups) ≠ 可管理(dept_admin_grant) ≠ 可授权(本组端点=kb_admin)。
#   守卫：kb_admin 用户不经本 UI 改（防误降级/锁死）；不能改自己；owner_dept 经 sanitize fail-closed。
# ═══════════════════════════════════════════════════════════════
@router.get("/api/kb/admin-grants", response_model=KbAdminListResponse)
def kb_admin_grants_list(request: Request,
                         identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 查看现行管理员名单（dept_admin + kb_admin）及各自可管理的 owner_dept。只读。"""
    _enforce_rate_limit(request, identity, scope="aux")
    _require_kb_admin(identity)
    from opensearch_pipeline.kb_authz import _valid_owner_depts
    items: List[KbAdminItem] = []
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT user_id, user_name, dept_code, role FROM {_kb_db()}.user_role "
                            "WHERE is_active=1 AND role IS NOT NULL AND role<>'employee' ORDER BY role, user_id")
                roles = cur.fetchall()
                cur.execute(f"SELECT user_id, managed_owner_dept FROM {_kb_db()}.dept_admin_grant "
                            "WHERE is_active=1")
                grants: Dict[str, List[str]] = {}
                for r in cur.fetchall():
                    if r and r[0]:
                        grants.setdefault(r[0], []).append(r[1])
                for r in roles:
                    uid = r[0] or ""
                    items.append(KbAdminItem(
                        user_id=uid, user_name=r[1] or "", role=r[3] or "",
                        managed_owner_depts=sorted(grants.get(uid, []))))
        finally:
            conn.close()
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_admin_grants_list 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"成员名单查询失败 (trace: {trace_id})")
    return KbAdminListResponse(items=items, grantable_owner_depts=sorted(_valid_owner_depts()))


@router.post("/api/kb/admin-grants", response_model=KbAdminGrantResponse)
def kb_admin_grant(req: KbAdminGrantRequest, request: Request,
                   identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 授予/更新一名【部门管理员】可管理的 owner_dept（owner_depts = 权威全集,提交即覆盖）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_admin(identity)
    from opensearch_pipeline.kb_authz import sanitize_owner_depts, ROLE_DEPT_ADMIN, ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    uid = (req.user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="缺少 user_id（钉钉 staffId）")
    if uid == kb.user_id:
        raise HTTPException(status_code=400, detail="不能修改自己的角色/授权")
    depts = sanitize_owner_depts(req.owner_depts)   # 净化 + 写白名单（fail-closed 丢非法）
    if not depts:
        raise HTTPException(status_code=400, detail="可管理部门为空或全不在白名单（无法授予）")
    assert_metadata_write_allowed("kb_admin_grant", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    note = (req.note or "")[:255] or None
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 守卫：已是 kb_admin 的用户不经本 UI 改（避免误降级；kb_admin 调整走运维脚本）
                cur.execute(f"SELECT role FROM {_kb_db()}.user_role WHERE user_id=%s AND is_active=1 "
                            "ORDER BY updated_at DESC, id DESC LIMIT 1", (uid,))
                row = cur.fetchone()
                if row and (row[0] or "") == ROLE_KB_ADMIN:
                    raise HTTPException(status_code=400,
                                        detail="该用户已是知识库管理员（kb_admin），请用运维脚本调整以免误降级")
                # 角色 → dept_admin（dept_code 同步为可管理组 CSV，与 seed 口径一致）
                cur.execute(f"INSERT INTO {_kb_db()}.user_role (user_id, user_name, dept_code, role, is_active) "
                            "VALUES (%s,%s,%s,%s,1) ON DUPLICATE KEY UPDATE "
                            "user_name=COALESCE(VALUES(user_name), user_name), dept_code=VALUES(dept_code), "
                            "role=VALUES(role), is_active=1, updated_at=NOW()",
                            (uid, (req.user_name or None), ",".join(depts), ROLE_DEPT_ADMIN))
                # 权威全集语义：先软撤销本次【未包含】的旧授权,再 upsert 本次
                ph = ",".join(["%s"] * len(depts))
                cur.execute(f"UPDATE {_kb_db()}.dept_admin_grant SET is_active=0, updated_at=NOW() "
                            f"WHERE user_id=%s AND is_active=1 AND managed_owner_dept NOT IN ({ph})",
                            (uid, *depts))
                for owner in depts:
                    cur.execute(f"INSERT INTO {_kb_db()}.dept_admin_grant "
                                "(user_id, managed_owner_dept, granted_by, note, is_active) VALUES (%s,%s,%s,%s,1) "
                                "ON DUPLICATE KEY UPDATE is_active=1, granted_by=VALUES(granted_by), "
                                "note=VALUES(note), updated_at=NOW()",
                                (uid, owner, kb.user_id, note))
                # 同事务审计（B1）：与角色/授权变更原子提交。
                write_audit(doc_id=None, version_no=None, action_type="KB_ADMIN_GRANT",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"grant dept_admin {uid} → {','.join(depts)}", cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_admin_grant 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"授予部门管理员失败 (trace: {trace_id})")
    return KbAdminGrantResponse(user_id=uid, role=ROLE_DEPT_ADMIN, managed_owner_depts=depts, ok=True)


@router.post("/api/kb/admin-grants/revoke", response_model=KbAdminGrantResponse)
def kb_admin_grant_revoke(req: KbAdminRevokeRequest, request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 撤销部门管理员授权：owner_dept 指定→撤该一项；为空→撤全部并降级 employee。
    无活跃授权剩余时把 user_role.role 降为 employee（即时失去管理入口）。kb_admin/自身不可经此撤销。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_admin(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, ROLE_EMPLOYEE
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    uid = (req.user_id or "").strip()
    owner = (req.owner_dept or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="缺少 user_id")
    if uid == kb.user_id:
        raise HTTPException(status_code=400, detail="不能撤销自己的授权")
    assert_metadata_write_allowed("kb_admin_grant_revoke", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    demoted = False
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT role FROM {_kb_db()}.user_role WHERE user_id=%s AND is_active=1 "
                            "ORDER BY updated_at DESC, id DESC LIMIT 1", (uid,))
                row = cur.fetchone()
                if row and (row[0] or "") == ROLE_KB_ADMIN:
                    raise HTTPException(status_code=400, detail="不能经本 UI 撤销知识库管理员（kb_admin）")
                if owner:
                    cur.execute(f"UPDATE {_kb_db()}.dept_admin_grant SET is_active=0, updated_at=NOW() "
                                "WHERE user_id=%s AND managed_owner_dept=%s AND is_active=1", (uid, owner))
                else:
                    cur.execute(f"UPDATE {_kb_db()}.dept_admin_grant SET is_active=0, updated_at=NOW() "
                                "WHERE user_id=%s AND is_active=1", (uid,))
                cur.execute(f"SELECT COUNT(*) FROM {_kb_db()}.dept_admin_grant "
                            "WHERE user_id=%s AND is_active=1", (uid,))
                remaining = int(cur.fetchone()[0] or 0)
                if remaining == 0:
                    cur.execute(f"UPDATE {_kb_db()}.user_role SET role=%s, updated_at=NOW() "
                                "WHERE user_id=%s", (ROLE_EMPLOYEE, uid))
                    demoted = True
                # 同事务审计（B1）：与撤销/降级变更原子提交。
                write_audit(doc_id=None, version_no=None, action_type="KB_ADMIN_REVOKE",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"revoke {uid} owner={owner or 'ALL'} demoted={demoted}", cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_admin_grant_revoke 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"撤销部门管理员授权失败 (trace: {trace_id})")
    return KbAdminGrantResponse(user_id=uid, role=(ROLE_EMPLOYEE if demoted else "dept_admin"), ok=True)
