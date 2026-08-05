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
from opensearch_pipeline.reindex_states import ChunkIndexStatus
from opensearch_pipeline.request_context import get_request_id

# api 驻留共享件（模型/助手/依赖）。from-import 拷贝绑定在这里是安全的：
# 这些名字均不在 tests 的 api monkeypatch 清单内（见 routes/__init__.py）。
from opensearch_pipeline.api import (
    Identity,
    _enforce_rate_limit,
    _kb_can_manage,
    _kb_can_manage_doc,
    _kb_db,
    _kb_node_capability,
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
    # P3-3（2026-08-04）：本端点是**硬 LIMIT 队列**，此前截断完全不外露 —— 队列超过上限时
    # 使用者看到的「就这些」只是前 N 条，**且无从知道**。与 B8（差评复核）同族：
    # 先让截断不再静默；真分页是另一回事（需稳定排序键 + 前端翻页，另议）。
    truncated: bool = False


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
    # P3-3（2026-08-04）：本端点是**硬 LIMIT 队列**，此前截断完全不外露 —— 队列超过上限时
    # 使用者看到的「就这些」只是前 N 条，**且无从知道**。与 B8（差评复核）同族：
    # 先让截断不再静默；真分页是另一回事（需稳定排序键 + 前端翻页，另议）。
    truncated: bool = False


class KbAccessGrantCreate(BaseModel):
    """Owner 侧主动共享：文档所属部门管理员直接放行指定部门（无需对方先申请）。"""
    doc_id: str
    target_depts: List[str] = Field(default_factory=list)
    reason: Optional[str] = None


class KbAccessGrantCreateResponse(BaseModel):
    doc_id: str = ""
    granted: List[str] = Field(default_factory=list)   # 本次新放行的组码
    skipped: List[str] = Field(default_factory=list)   # 已覆盖 / 归属自身 / 伞下冗余而跳过
    ok: bool = True


# ── node-ACL：组织树节点授权（可见范围「整体替换」）─────────────────────────
class KbNodeGrantItem(BaseModel):
    dept_id: int
    subtree: bool = True          # True=含整棵子树(投影 d:) | False=仅直挂本节点(投影 dx:)


class KbNodeGrantsSave(BaseModel):
    """管理员在组织树上勾选后的【整体替换】保存。"""
    doc_id: str
    nodes: List[KbNodeGrantItem] = Field(default_factory=list)
    # 并发整体替换的 CAS:传入端上读到的 acl_revision;与库中不一致 ⇒ 409(有人先改了)
    acl_revision: Optional[int] = None
    reason: Optional[str] = None


class KbNodeGrantsSaveResponse(BaseModel):
    doc_id: str = ""
    acl_mode: str = ""
    acl_revision: int = 0
    granted: List[str] = Field(default_factory=list)   # "d:<id>" / "dx:<id>"
    revoked: List[str] = Field(default_factory=list)
    ok: bool = True


@router.post("/api/kb/doc-node-grants", response_model=KbNodeGrantsSaveResponse)
def kb_doc_node_grants_save(req: KbNodeGrantsSave, request: Request,
                            identity: Optional[Identity] = Depends(current_identity)):
    """保存文档的组织树节点可见范围 —— **整体替换,不是追加**。

    语义(设计稿 §5「保存=替换」):未勾选的节点 soft-revoke、已勾选的 upsert/复活,
    文档同时切到 `acl_mode='node'`。切换后该文档的 **legacy 组码授权对检索失效**
    (`project_doc_acl` 模式互斥:node 投影只出 `d:`/`dx:`,绝不含组码) —— 既有
    `kb_access_request` 行**保留作审计**,不删。

    同事务完成(缺一不可):
      ①`document_meta ... FOR UPDATE` 行锁 + `acl_revision` CAS —— 并发两个"整体替换"
        必须串行化,否则后提交者会把先提交者的勾选静默丢掉;
      ②切 `acl_mode` + `acl_revision+1`;③节点集整体替换;④审计;
      ⑤`enqueue_acl_projection` 持久入队(权威变更与投影意图原子提交);
      ⑥内联 `materialize_doc_allowed_depts` best-effort(失败有 outbox + reconcile 兜底)。
    提交后失效 deny 缓存(撤销即时生效)。

    硬规则(fail-closed):
      - 只 `dept_internal` 可设节点可见范围(public 本就全司可读→400;restricted 绝不外露→403);
      - 非在线文档不可改(400);
      - 必须 `_kb_can_manage(document_meta.owner_dept)` —— **管理轴仍是真实 owner**
        (node 模式只改检索投影轴,归属/管理轴不动),故此处授权判定无需等 T5;
      - 节点数超上限 → **422 拒绝**,绝不静默截断后让 UI 以为已完整保存;
      - dept_id 必须是**在册**组织节点(`dept_dim.is_active=1`)—— 授权给已消失/不存在的
        节点等于该文档对所有人不可见,且事后无从解释。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.access_grants import (
        materialize_doc_allowed_depts, record_acl_projection_invalidation,
    )
    from opensearch_pipeline.acl_policy import (
        ACL_MODE_NODE, MAX_DOC_NODES, format_node_value, normalize_node_ids,
    )
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed

    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    ids, overflow = normalize_node_ids([n.dept_id for n in req.nodes], limit=MAX_DOC_NODES)
    if overflow:
        raise HTTPException(status_code=422,
                            detail=f"授权节点数超上限 {MAX_DOC_NODES}（请改选更上层节点）")
    if len(ids) != len({n.dept_id for n in req.nodes}):
        raise HTTPException(status_code=400, detail="含非法 dept_id（须为正整数）")
    scope_by_id = {int(n.dept_id): ("subtree" if n.subtree else "exact") for n in req.nodes}

    assert_metadata_write_allowed("kb_doc_node_grants_save", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    granted: List[str] = []
    revoked: List[str] = []
    new_rev = 0
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # ① 行锁 + 前置校验(与并发退役/升版串行化)
                cur.execute(
                    f"SELECT owner_dept, permission_level, status, acl_mode, acl_revision, "
                    f"owner_dept_id FROM {_kb_db()}.document_meta WHERE doc_id=%s FOR UPDATE",
                    (req.doc_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, perm, status, cur_mode, cur_rev = (
                    row[0] or "", (row[1] or "").lower(), (row[2] or "active").lower(),
                    row[3] or "legacy", int(row[4] or 0))
                owner_dept_id = row[5]
                if status != "active":
                    raise HTTPException(status_code=400, detail="文档不在线,无法设置可见范围")
                if perm == "restricted":
                    raise HTTPException(status_code=403, detail="restricted 文档不可对外放行")
                if perm != "dept_internal":
                    raise HTTPException(status_code=400,
                                        detail="仅 dept_internal 文档可设节点可见范围")
                # 阶段 B 契约收紧（codex 共识）：本端点只做【已是 node 且归属完整】文档的
                # 可见集整体替换。legacy→node 迁移唯一入口 = doc-meta 端点（须同时给
                # owner_dept_id + visible_nodes）——阶段 A 的「保存即切 mode」产生过
                # 缺 owner 的半迁移态（acl_mode=node + owner_dept_id NULL ⇒ 仅 kb_admin
                # 可管），现网无 node 文档、无兼容负担，直接废除。
                if (cur_mode or "legacy") != ACL_MODE_NODE:
                    raise HTTPException(status_code=409,
                                        detail="该文档仍是组码授权模式，请先在「编辑信息」中迁移归属到组织树")
                if not owner_dept_id:
                    raise HTTPException(status_code=409,
                                        detail="该文档缺归属节点（半迁移态），请先在「编辑信息」中补齐归属")
                if not _kb_can_manage_doc(kb, cur_mode, owner_dept, owner_dept_id):
                    raise HTTPException(status_code=403, detail="无权管理该文档（非属主部门管理员）")
                # CAS 必填（codex major M5）：node 文档必经 register/doc-meta 创建，revision
                # 永远可读——继续允许缺省 = 并发整体替换互相丢勾选的保护形同虚设。
                if req.acl_revision is None:
                    raise HTTPException(status_code=400,
                                        detail="缺少 acl_revision（请从文档详情读取当前版本后提交）")
                if int(req.acl_revision) != cur_rev:
                    raise HTTPException(
                        status_code=409,
                        detail=f"可见范围已被他人修改（当前版本 {cur_rev}），请刷新后重试")

                # dept_id 必须在册 —— 授权给不存在/已消失的节点 = 该文档对所有人不可见
                if ids:
                    ph = ",".join(["%s"] * len(ids))
                    cur.execute(
                        f"SELECT dept_id FROM {_kb_db()}.dept_dim "
                        f"WHERE is_active=1 AND dept_id IN ({ph})", tuple(ids))
                    live = {int(r[0]) for r in cur.fetchall()}
                    missing = [i for i in ids if i not in live]
                    if missing:
                        raise HTTPException(
                            status_code=400,
                            detail=f"节点不存在或已停用: {missing}（组织快照可能过期，请稍后重试）")

                # ③ 整体替换:未勾选的 soft-revoke、勾选的 upsert/复活
                cur.execute(
                    f"SELECT dept_id, scope FROM {_kb_db()}.kb_doc_node_grant "
                    "WHERE doc_id=%s AND revoked_at IS NULL", (req.doc_id,))
                before = {(int(r[0]), str(r[1] or "subtree")) for r in cur.fetchall()}
                after = {(i, scope_by_id[i]) for i in ids}
                for dept_id, scope in sorted(before - after):
                    cur.execute(
                        f"UPDATE {_kb_db()}.kb_doc_node_grant SET revoked_at=NOW(), revoked_by=%s "
                        "WHERE doc_id=%s AND dept_id=%s AND scope=%s AND revoked_at IS NULL",
                        (kb.user_id, req.doc_id, dept_id, scope))
                    revoked.append(format_node_value(dept_id, exact=(scope == "exact")))
                for dept_id, scope in sorted(after - before):
                    cur.execute(
                        f"INSERT INTO {_kb_db()}.kb_doc_node_grant "
                        "(doc_id, dept_id, scope, granted_by, note) VALUES (%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE revoked_at=NULL, revoked_by=NULL, "
                        "granted_by=VALUES(granted_by), granted_at=NOW(), note=VALUES(note)",
                        (req.doc_id, dept_id, scope, kb.user_id, (req.reason or "")[:255]))
                    granted.append(format_node_value(dept_id, exact=(scope == "exact")))

                # ② 切模式 + CAS 版本 +1(即便节点集未变也推进,便于端上察觉并发)
                new_rev = cur_rev + 1
                cur.execute(
                    f"UPDATE {_kb_db()}.document_meta SET acl_mode=%s, acl_revision=%s, "
                    "updated_at=NOW() WHERE doc_id=%s",
                    (ACL_MODE_NODE, new_rev, req.doc_id))

                # ⑤⑥ 投影:持久入队(不吞异常)+ 内联标脏 best-effort
                record_acl_projection_invalidation(cur, req.doc_id, reason="node_grants_save")
                try:
                    materialize_doc_allowed_depts(cur, req.doc_id)
                except Exception as _pe:   # noqa: BLE001 — outbox + reconcile 兜底
                    logger.warning("node 授权内联标脏失败（outbox 兜底）doc=%s: %s", req.doc_id, _pe)

                write_audit(doc_id=req.doc_id, version_no=None, action_type="ACL_NODE_GRANTS_SAVE",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=(f"mode {cur_mode}→node rev {cur_rev}→{new_rev} "
                                     f"+{granted} -{revoked}"), cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_doc_node_grants_save 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"保存失败 (trace: {trace_id})")

    # 撤销即时生效:提交后失效查询侧授权复核缓存
    try:
        from opensearch_pipeline.retriever import invalidate_deny_cache
        invalidate_deny_cache(req.doc_id)
    except Exception as _ce:   # noqa: BLE001
        logger.warning("失效 deny 缓存失败（TTL 兜底）doc=%s: %s", req.doc_id, _ce)

    return KbNodeGrantsSaveResponse(
        doc_id=req.doc_id, acl_mode=ACL_MODE_NODE, acl_revision=new_rev,
        granted=granted, revoked=revoked, ok=True)


# ── Phase F：成员/角色管理（kb_admin 维护 dept_admin 写授权；三分授权 读≠管理≠授权）──
class KbAdminNodeRoot(BaseModel):
    """阶段 B 管理轴：一条管辖根（覆盖该节点及全部后代）。"""
    dept_id: int = 0
    name: str = ""                                            # dept_dim 现名（失活节点回 id 串）
    source: str = "manual"                                    # auto | manual
    active: bool = True


class KbAdminItem(BaseModel):
    user_id: str = ""
    user_name: str = ""
    role: str = ""                                            # dept_admin / kb_admin
    managed_owner_depts: List[str] = Field(default_factory=list)  # dept_admin 显式授权；kb_admin=全部(空数组表示全量)
    managed_node_roots: List[KbAdminNodeRoot] = Field(default_factory=list)  # 阶段 B：node 管辖根（两轴独立）


class KbAdminListResponse(BaseModel):
    items: List[KbAdminItem] = Field(default_factory=list)
    grantable_owner_depts: List[str] = Field(default_factory=list)  # 表单可选项（写白名单单一来源）


class KbAdminGrantRequest(BaseModel):
    user_id: str = ""                                         # 钉钉 staffId
    user_name: str = ""
    owner_depts: List[str] = Field(default_factory=list)      # 授予可管理的 owner_dept（权威全集，提交即覆盖）
    # 阶段 B：node 管辖根（manual）。None=**不动**节点轴；[]=清空该轴；[ids]=该轴权威全集
    # （覆盖语义按轴隔离——覆盖 legacy 不动 node，反之亦然，codex major M3）。
    node_roots: Optional[List[int]] = None
    note: str = ""


class KbAdminRevokeRequest(BaseModel):
    user_id: str = ""
    owner_dept: str = ""                                      # 撤单条 legacy 授权
    node_root: int = 0                                        # 阶段 B：撤单条 node 管辖根
    # 两者都空 = 撤两轴全部授权；降级 employee 的判定看**两表**剩余（node-only 管理员不误降）


class KbAdminGrantResponse(BaseModel):
    user_id: str = ""
    role: str = ""
    managed_owner_depts: List[str] = Field(default_factory=list)
    managed_node_roots: List[int] = Field(default_factory=list)
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
    # 审批人驳回时填写的原因（decision_note）：反馈闭环——原先申请人只看到按钮变回
    # 「申请授权」，被驳回这件事本身和原因都无从得知。
    decision_note: str = ""


class MyAccessRequestListResponse(BaseModel):
    items: List[MyAccessRequestItem] = Field(default_factory=list)
    # P3-3（2026-08-04）：硬 LIMIT 100，此前截断不外露。申请人提交超过 100 条时，
    # 更早的申请会静默消失在「我的申请」里 —— 同 B8 家族，先让截断不再静默。
    truncated: bool = False


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
                # 阶段 B（M6）：node 文档禁止**新增** legacy 组码授权——申请/共享/审批的组码
                # 通道对 node 文档全部关死（隐形组码会在 node→legacy 回滚时突然复活）；
                # 收权动作（reject/revoke）仍允许（decide 端点放行）。
                _cap = _kb_node_capability(cur)
                _mc = ", acl_mode" if _cap == "present" else ""
                cur.execute(f"SELECT owner_dept, permission_level, status{_mc} "
                            f"FROM {_kb_db()}.document_meta WHERE doc_id=%s LIMIT 1", (req.doc_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, perm, status = (row[0] or ""), (row[1] or ""), (row[2] or "active")
                if len(row) > 3 and (row[3] or "legacy") == "node":
                    raise HTTPException(status_code=400,
                                        detail="该文档为组织树授权模式，请联系属主管理员在可见范围中添加你的部门")
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
    # 钉钉工作通知（RAG_ADMIN_NOTIFY 门控，best-effort no-raise）：归属部门管理员即时知晓待办
    from opensearch_pipeline.admin_notify import notify_access_request
    notify_access_request(owner_dept=owner_dept, doc_id=req.doc_id, requester_depts=requester_depts)
    return KbAccessRequestSubmitResponse(id=str(new_id), status="pending", already=False)


@router.get("/api/kb/access-requests", response_model=KbAccessRequestListResponse)
def kb_access_requests_list(request: Request,
                            identity: Optional[Identity] = Depends(current_identity)):
    """审批方待办：列出【我有权审批】的 pending 申请（owner_dept ∈ 我 managed；kb_admin 全部）。只读。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import (
        ROLE_KB_ADMIN, expand_managed_owner_depts, managed_owner_depts,
    )
    clause, params = "", []
    if kb.role != ROLE_KB_ADMIN:
        # production 伞组展开：子线 owner 的文档也归 production 管理员审批。
        owners = expand_managed_owner_depts(managed_owner_depts(kb))
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
                    LIMIT 101   -- 100+1 探针行（P3-3）
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
    _truncated = len(rows) > 100
    rows = rows[:100]
    items = [
        KbAccessRequestItem(
            id=str(r[0]), doc_id=r[1] or "", doc_title=r[2] or "", owner_dept=r[3] or "",
            requester_dept=r[4] or "", requester_name=r[5] or "",
            permission_level=r[6] or "dept_internal", reason=r[7] or "",
            created_at=str(r[8]) if r[8] else "",
        )
        for r in rows
    ]
    return KbAccessRequestListResponse(items=items, truncated=_truncated)


@router.get("/api/kb/access-grants", response_model=KbAccessGrantListResponse)
def kb_access_grants_list(request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """审批方侧：列出【我可管理】文档上现行有效（status='approved'）的跨部门检索授权，供撤销。

    owner_dept ∈ 我 managed（kb_admin 全部）。与待审批队列（pending）区分：此处是已放行的【存量】，
    撤销动作走 POST /api/kb/access-requests/revoke。只读。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import (
        ROLE_KB_ADMIN, expand_managed_owner_depts, managed_owner_depts,
    )
    clause, params = "", []
    if kb.role != ROLE_KB_ADMIN:
        # production 伞组展开：子线 owner 文档的存量授权也归 production 管理员管辖。
        owners = expand_managed_owner_depts(managed_owner_depts(kb))
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
                    LIMIT 201   -- 200+1 探针行（P3-3）
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
    _truncated = len(rows) > 200
    rows = rows[:200]
    items = [
        KbAccessGrantItem(
            id=str(r[0]), doc_id=r[1] or "", doc_title=r[2] or "", owner_dept=r[3] or "",
            requester_dept=r[4] or "", requester_name=r[5] or "",
            permission_level=r[6] or "dept_internal", reason=r[7] or "",
            decided_at=str(r[8]) if r[8] else "",
        )
        for r in rows
    ]
    return KbAccessGrantListResponse(items=items, truncated=_truncated)


@router.post("/api/kb/access-grants", response_model=KbAccessGrantCreateResponse)
def kb_access_grant_create(req: KbAccessGrantCreate, request: Request,
                           identity: Optional[Identity] = Depends(current_identity)):
    """Owner 侧【主动共享】：文档所属部门管理员（或 kb_admin）直接把 dept_internal 文档
    放行给指定部门检索——被动申请流（submit→approve）的主动式对偶，复用同一张表同一状态机。

    实现 = 直接 INSERT status='approved' 的 kb_access_request 行（requester=授权人自己、
    decided_by=自己、decided_at=NOW），随后与 approve 完全同款：flag 开则同事务
    enqueue_acl_projection + materialize 标脏（stage-3 推 HA3）、审计入同事务、
    commit 后失效 deny 缓存。撤销/清单/审批历史零改动即可用（就是 approved 行）。

    硬规则（fail-closed，与 submit/decide 同口径）：
    - 只 dept_internal 可共享（public 本就全司可读→400；restricted 绝不外露→403）；
    - 非在线文档不可共享（400）；
    - 授权人必须 _kb_can_manage(owner_dept)（403）；
    - 目标组码过 sanitize 白名单；「目标组本就可读该 owner」→ 冗余 skipped——判定唯一权威是
      retriever._expand_groups_to_owners 闭集 taxonomy（production 伞 + marketing 共享面），
      绝不 startswith：闭集外的 production_*（如 papercup 双拼）检索 fail-closed，对它们
      "共享给 production" 恰是唯一放行通道，前缀判定会把这救济误吞成冗余；
    - 已被既有 approved 行覆盖的目标 → skipped（幂等，可重复提交）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import sanitize_owner_depts
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    targets = sanitize_owner_depts(req.target_depts)
    if not targets:
        raise HTTPException(status_code=400, detail="无有效目标部门（须为合法组码）")
    assert_metadata_write_allowed("kb_access_grant_create", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    reason = (req.reason or "管理员主动共享")[:512]
    granted: List[str] = []
    skipped: List[str] = []
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 阶段 B：锁内读齐三元组、mode 隔离授权；node 文档禁止新增 legacy 共享
                # （组码通道对 node 文档关死，可见范围走节点授权——M6/设计稿 :326）。
                _cap = _kb_node_capability(cur)
                _mc = ", acl_mode, owner_dept_id" if _cap == "present" else ""
                cur.execute(f"SELECT owner_dept, permission_level, status{_mc} "
                            f"FROM {_kb_db()}.document_meta WHERE doc_id=%s FOR UPDATE", (req.doc_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, perm, status = (row[0] or ""), (row[1] or ""), (row[2] or "active")
                _mode, _oid = ((row[3] or "legacy"), row[4]) if _cap == "present" else ("legacy", None)
                if _mode == "node":
                    raise HTTPException(status_code=400,
                                        detail="该文档为组织树授权模式，请在「可见范围」中按组织节点共享")
                if not _kb_can_manage_doc(kb, _mode, owner_dept, _oid):
                    raise HTTPException(status_code=403, detail="无权共享该文档（非文档所属部门管理员）")
                if str(status).lower() != "active":
                    raise HTTPException(status_code=400, detail="该文档非在线状态，无法共享")
                if perm == "public":
                    raise HTTPException(status_code=400, detail="公开文档全公司可检索，无需共享")
                if perm != "dept_internal":
                    raise HTTPException(status_code=403, detail="受限文档不可共享")
                # 既有 approved 覆盖集（含被动审批流写入的 CSV 行）→ 幂等 skip
                cur.execute(f"SELECT requester_depts FROM {_kb_db()}.kb_access_request "
                            "WHERE doc_id=%s AND status='approved'", (req.doc_id,))
                covered = set()
                for (csv,) in (cur.fetchall() or []):
                    covered.update(p.strip() for p in str(csv or "").split(",") if p.strip())
                from opensearch_pipeline.retriever import _expand_groups_to_owners
                for t in targets:
                    # 归属自身 / 目标组读者本就覆盖该 owner（闭集 taxonomy：production 伞 +
                    # marketing 共享面）→ 冗余，跳过。闭集外 owner 不算覆盖（检索 fail-closed）。
                    if t == owner_dept or owner_dept in _expand_groups_to_owners([t]) or t in covered:
                        skipped.append(t)
                        continue
                    cur.execute(
                        f"INSERT INTO {_kb_db()}.kb_access_request "
                        "(doc_id, owner_dept, requester_id, requester_name, requester_depts, reason, "
                        " status, decided_by, decided_at, decision_note) "
                        "VALUES (%s,%s,%s,%s,%s,%s,'approved',%s,NOW(),%s)",
                        (req.doc_id, owner_dept, kb.user_id, kb.name, t, reason, kb.user_id, reason),
                    )
                    granted.append(t)
                if granted:
                    # 与 _kb_access_decide 同款投影：outbox 同事务持久入队 + 内联标脏 best-effort
                    from opensearch_pipeline.access_grants import (
                        materialize_doc_allowed_depts, record_acl_projection_invalidation,
                    )
                    # C3′/062（Sam 2026-08-03 拍板）：**bump+入队不受 flag 门控** —— flag 关闭期间
                    # 发生的权威变更若不 bump，水位永久丢失，开 flag 后这批文档永远判 unchanged
                    # = 原样复现 C3。materialize（消费侧）仍按 flag 门控。
                    record_acl_projection_invalidation(cur, req.doc_id, reason="direct_grant")
                    if get_config().rag.allowed_depts_acl:
                        try:
                            materialize_doc_allowed_depts(cur, req.doc_id)
                        except Exception as _pe:
                            logger.warning("direct_grant allowed_depts 内联标脏失败（outbox+reconciler 兜底）doc=%s: %s",
                                           req.doc_id, _pe)
                    write_audit(doc_id=req.doc_id, version_no=None, action_type="ACCESS_GRANT_DIRECT",
                                operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                                message=f"owner={owner_dept} targets={','.join(granted)}", cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_access_grant_create 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"共享失败 (trace: {trace_id})")
    if granted:
        # 与 decide 同款：主动失效检索侧 deny 缓存（TTL>0 时保「共享即时生效」；失败只记日志）
        try:
            from opensearch_pipeline.retriever import invalidate_deny_cache
            invalidate_deny_cache(req.doc_id or None)
        except Exception as _ie:   # noqa: BLE001
            logger.warning("deny 缓存失效失败（忽略，TTL 兜底）doc=%s: %s", req.doc_id, _ie)
    return KbAccessGrantCreateResponse(doc_id=req.doc_id, granted=granted, skipped=skipped, ok=True)


class KbVisibilityReader(BaseModel):
    """有效可见范围里的一个读者组：dept=组码/节点名（前端 deptLabel 对组码转中文，
    节点名直出），via=来源。"""
    dept: str = ""
    via: str = "owner"        # owner=归属部门 / umbrella=生产伞组 / shared_policy=营销共享面 /
    #                           grant=跨部门授权 / node_subtree=节点(含下级) / node_exact=节点(仅本级)


class KbVisibilityExplainResponse(BaseModel):
    doc_id: str = ""
    owner_dept: str = ""
    permission_level: str = "dept_internal"
    acl_mode: str = "legacy"  # 阶段 B：node 文档的可见范围来自组织树授权，不再有组码语义
    everyone: bool = False    # public：全公司可检索
    nobody: bool = False      # restricted / 非在线 / 隔离：不进检索
    quarantined: bool = False # 安全隔离（chunk 停用；唯一出路=脱敏重灌）
    active: bool = True
    readers: List[KbVisibilityReader] = Field(default_factory=list)


@router.get("/api/kb/visibility-explain", response_model=KbVisibilityExplainResponse)
def kb_visibility_explain(request: Request, doc_id: str = "",
                          identity: Optional[Identity] = Depends(current_identity)):
    """「谁能看到这篇文档」解释器（只读）：把 基础级别 + 组语义（production 伞组 /
    marketing 共享面）+ 跨部门授权（approved 行）折叠成一份有效可见范围清单。

    判定与检索侧同源：逐组用 retriever._expand_groups_to_owners 反查「哪些用户组的
    owner 扩展覆盖本文档 owner_dept」——绝不在这里手写第二份伞组/共享面规则
    （闭集 taxonomy 变了这里自动跟上）。授权：_kb_can_manage（文档归属部门管理员 /
    kb_admin）——授权清单含其他部门名单，不对只读浏览者外露。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    if not doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    trace_id = get_request_id()
    grant_depts: List[str] = []
    node_readers: List[KbVisibilityReader] = []
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                _cap = _kb_node_capability(cur)
                _mc = ", acl_mode, owner_dept_id" if _cap == "present" else ""
                cur.execute(f"SELECT owner_dept, permission_level, status, current_version_no{_mc} "
                            f"FROM {_kb_db()}.document_meta WHERE doc_id=%s LIMIT 1", (doc_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, perm = (row[0] or ""), (row[1] or "dept_internal")
                status, cur_ver = (row[2] or "active"), int(row[3] or 1)
                _mode, _oid = ((row[4] or "legacy"), row[5]) if _cap == "present" else ("legacy", None)
                if not _kb_can_manage_doc(kb, _mode, owner_dept, _oid):
                    raise HTTPException(status_code=403, detail="无权查看该文档的可见范围明细")
                cur.execute(f"SELECT publish_status, gate_status FROM {_kb_db()}.document_version "
                            "WHERE doc_id=%s AND version_no=%s", (doc_id, cur_ver))
                vrow = cur.fetchone()
                quarantined = bool(vrow and (str(vrow[0] or "").upper() == "QUARANTINED"
                                             or str(vrow[1] or "").lower() == "quarantined"))
                if perm == "dept_internal" and _mode != "node":
                    cur.execute(f"SELECT requester_depts FROM {_kb_db()}.kb_access_request "
                                "WHERE doc_id=%s AND status='approved'", (doc_id,))
                    _seen_g = set()
                    for (csv,) in (cur.fetchall() or []):
                        for p in str(csv or "").split(","):
                            p = p.strip()
                            if p and p not in _seen_g:
                                _seen_g.add(p)
                                grant_depts.append(p)
                elif perm == "dept_internal" and _mode == "node":
                    # 阶段 B（codex 缺口②）：node 文档解释节点授权——LEFT JOIN dept_dim 取现名，
                    # **含失活节点并明确标注**（active INNER JOIN 会把授权意图悄悄隐藏——
                    # 授权还在、节点没了，正是「文档不可见且无从解释」要解释的那种情况）。
                    cur.execute(
                        f"SELECT g.dept_id, g.scope, d.name, d.is_active "
                        f"FROM {_kb_db()}.kb_doc_node_grant g "
                        f"LEFT JOIN {_kb_db()}.dept_dim d ON d.dept_id = g.dept_id "
                        "WHERE g.doc_id=%s AND g.revoked_at IS NULL ORDER BY g.dept_id", (doc_id,))
                    for r in cur.fetchall():
                        _name = r[2] or str(r[0])
                        if r[3] is None or not r[3]:
                            _name += "（节点已失效，无人经此可见）"
                        node_readers.append(KbVisibilityReader(
                            dept=_name,
                            via=("node_subtree" if (r[1] or "subtree") == "subtree" else "node_exact")))
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_visibility_explain 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"可见范围查询失败 (trace: {trace_id})")

    active = str(status).lower() == "active"
    resp = KbVisibilityExplainResponse(doc_id=doc_id, owner_dept=owner_dept,
                                       permission_level=perm, active=active,
                                       acl_mode=_mode, quarantined=quarantined)
    if quarantined or not active or perm == "restricted":
        resp.nobody = True        # 隔离/退役/受限：不在检索中（优先级最高，覆盖一切授权）
        return resp
    if perm == "public":
        resp.everyone = True
        return resp
    if _mode == "node":
        # node 文档：可见范围 = 节点授权全集（组码语义不存在；授权空集 = 无人可见，如实）
        resp.readers = node_readers
        resp.nobody = not node_readers
        return resp
    # dept_internal：与检索同源反查——owner 组自身 / production 伞 / marketing 共享面
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS, _expand_groups_to_owners
    readers: List[KbVisibilityReader] = []
    covered = set()
    for g in sorted(_VALID_ACL_GROUPS, key=lambda x: (x != owner_dept, x)):   # 归属组排最前
        if owner_dept in _expand_groups_to_owners([g]):
            via = ("owner" if g == owner_dept
                   else "umbrella" if g == "production" else "shared_policy")
            readers.append(KbVisibilityReader(dept=g, via=via))
            covered.add(g)
    for g in grant_depts:
        if g not in covered:
            readers.append(KbVisibilityReader(dept=g, via="grant"))
            covered.add(g)
    resp.readers = readers
    return resp


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


def _mask_legacy_target(tok: str) -> str:
    """存量未掩码行的展示兜底：token 开头 ≥9 位连续数字段照写侧同款首4…尾4呈现。

    写侧掩码（audit_log.mask_staff_id）2026-08-04 才上线，之前写入的行 message 里是
    完整 staffId——若照原样返回，剥前缀修复会让它首次出现在 title/subject。新行
    （'9999…2233'）开头数字段只有 4 位不受影响；seed 行 '8888…2277(张三)' 保留括注。
    """
    import re as _re
    m = _re.match(r"\d{9,}", tok)
    if not m:
        return tok
    from opensearch_pipeline.audit_log import mask_staff_id
    return mask_staff_id(m.group(0)) + tok[m.end():]


def _parse_admin_target(msg: str) -> str:
    """从 KB_ADMIN_GRANT/REVOKE 审计 message 抽目标用户 id（best-effort，格式由我方代码固定）。

    grant：'grant dept_admin <uid> → <depts>'；revoke：'revoke <uid> owner=<..> demoted=..'。
    端点写的行经 _audit_params 盖了 '[acl_policy=<ver>] ' 前缀（KB_ADMIN_* 在
    _ACL_AUDIT_ACTIONS 内），先剥掉再解析；seed 脚本裸 SQL 写的行无前缀。
    uid 自 2026-08-04 起为首4…尾4掩码（写侧 mask_staff_id）；更早的存量行经
    _mask_legacy_target 兜底，任何一代格式都不把完整 staffId 送进 title/subject。
    """
    try:
        parts = (msg or "").split()
        if parts and parts[0].startswith("[acl_policy="):
            parts = parts[1:]
        if len(parts) >= 3 and parts[0] == "grant":
            return _mask_legacy_target(parts[2])
        if len(parts) >= 2 and parts[0] == "revoke":
            return _mask_legacy_target(parts[1])
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
    from opensearch_pipeline.kb_authz import (
        ROLE_KB_ADMIN, expand_managed_owner_depts, managed_owner_depts,
    )
    is_admin = (kb.role == ROLE_KB_ADMIN)
    scope_owner, scope_owner_params = "", []
    scope_cat, scope_cat_params = "", []
    if not is_admin:
        # production 伞组展开：子线 owner 文档的审批历史归 production 管理员可见。
        # category_dept 侧复用同一集合是无害超集（贡献类目恒为伞组码，子线值匹配不到行）。
        owners = expand_managed_owner_depts(managed_owner_depts(kb))
        if not owners:
            return KbApprovalHistoryResponse(items=[])   # 无管理部门 → 空（fail-closed，绝不当全量）
        ph = ",".join(["%s"] * len(owners))
        scope_owner = f"AND r.owner_dept IN ({ph})"
        scope_owner_params = list(owners)
        scope_cat = f"AND category_dept IN ({ph})"
        scope_cat_params = list(owners)
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
                fails += 1
                logger.warning("approval_history access 失败: %s", e)
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
                fails += 1
                logger.warning("approval_history contribution 失败: %s", e)
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
                    fails += 1
                    logger.warning("approval_history upload 失败: %s", e)
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
                    fails += 1
                    logger.warning("approval_history admin_grant 失败: %s", e)
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

    E#40：同步态派生批量化——原实现逐行 2 条 SQL（最多 100 行 × 2 的 N+1），现先收集全部
    (doc_id, version_no)，1 条 GROUP BY 拿计数 + 1 条 IN 拿 allowed_depts（Python 端并集），
    循环内纯内存判定；round-trip 常数 3 条。批量结果为空/异常 → 回退逐行派生（行为不变）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    items: List[MyAccessRequestItem] = []
    _truncated = False   # P3-3：与 items 同初始化——早退/降级路径下也要有确定值
    try:
        from opensearch_pipeline.db import _get_db_conn
        from opensearch_pipeline.access_grants import current_allowed_for_doc
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT r.id, r.doc_id, m.title, r.owner_dept, r.requester_depts, r.status,
                           r.reason, r.created_at, r.decided_at, m.current_version_no, r.decision_note
                    FROM {_kb_db()}.kb_access_request r
                    LEFT JOIN {_kb_db()}.document_meta m ON m.doc_id = r.doc_id
                    WHERE r.requester_id = %s
                    ORDER BY r.created_at DESC
                    LIMIT 101   -- 100+1 探针行（P3-3）
                    """,
                    (kb.user_id,),
                )
                rows = cur.fetchall()
                _truncated = len(rows) > 100      # P3-3：探针行命中 ⇒ 还有更早的申请
                rows = rows[:100]
                # ── E#40 批量预取：approved 行的 (doc_id, current_version) 去重对 ──
                import json as _json
                pairs: List[tuple] = []
                _seen_pairs = set()
                for r in rows:
                    if (r[5] or "") == "approved" and (r[1] or ""):
                        try:
                            p = (r[1], int(r[9] or 1))
                        except (TypeError, ValueError):
                            continue   # 坏版本号：留给逐行 try 降级 n/a（与旧行为一致）
                        if p not in _seen_pairs:
                            _seen_pairs.add(p)
                            pairs.append(p)
                counts: Dict[tuple, tuple] = {}      # (doc_id, ver) → (cnt, n_idx)
                allowed_map: Dict[tuple, set] = {}   # (doc_id, ver) → allowed_depts 并集
                batch_ok = False
                if pairs:
                    try:
                        ph = ",".join(["(%s,%s)"] * len(pairs))
                        flat = [v for p in pairs for v in p]
                        cur.execute(
                            f"SELECT doc_id, version_no, COUNT(*), SUM(index_status='{ChunkIndexStatus.INDEXED}') "
                            f"FROM {_kb_db()}.chunk_meta "
                            f"WHERE (doc_id, version_no) IN ({ph}) AND is_active=1 "
                            "GROUP BY doc_id, version_no",
                            tuple(flat),
                        )
                        cnt_rows = cur.fetchall() or ()
                        if cnt_rows:
                            for cr in cnt_rows:
                                counts[(cr[0], int(cr[1]))] = (int(cr[2] or 0), int(cr[3] or 0))
                            # allowed_depts 并集：解析语义与 access_grants.current_allowed_for_doc
                            # 一致——单行坏 JSON 跳过+告警，不连累整篇 doc（少计只会显 pending_sync，
                            # 朝对账重投影自愈方向，绝不虚报 projected）。
                            cur.execute(
                                "SELECT DISTINCT doc_id, version_no, allowed_depts "
                                f"FROM {_kb_db()}.chunk_meta "
                                f"WHERE (doc_id, version_no) IN ({ph}) AND is_active=1",
                                tuple(flat),
                            )
                            for ar in cur.fetchall() or ():
                                key = (ar[0], int(ar[1]))
                                vals = allowed_map.setdefault(key, set())
                                ad = ar[2]
                                if not ad:
                                    continue
                                if isinstance(ad, list):
                                    vals.update(ad)
                                    continue
                                try:
                                    vals.update(_json.loads(ad) or [])
                                except (ValueError, TypeError):
                                    logger.warning(
                                        "my-access 跳过 doc=%s v=%s 的坏 allowed_depts JSON: %r",
                                        ar[0], ar[1], str(ad)[:80])
                            batch_ok = True
                        # cnt_rows 为空 = 全部 doc 无 active chunk（罕见）→ 走逐行回退，
                        # 逐行 COUNT 同样得 0 → pending_sync，结果一致（也兼容按单 doc
                        # 参数分发的测试桩游标）。
                    except Exception as _be:   # noqa: BLE001 — 批量失败绝不连累列表，回退逐行
                        logger.warning("my-access 批量派生失败，回退逐行: %s", _be)
                        batch_ok = False
                for r in rows:
                    doc_id = r[1] or ""
                    rdepts = r[4] or ""
                    status = r[5] or ""
                    sync = "n/a"
                    if status == "approved" and doc_id:
                        try:
                            ver = int(r[9] or 1)
                            if batch_ok:
                                # 纯内存判定：GROUP BY 缺席 ≡ 无 active chunk ≡ (0,0)
                                cnt, n_idx = counts.get((doc_id, ver), (0, 0))
                                allowed = allowed_map.get((doc_id, ver), set())
                            else:
                                cur.execute(
                                    f"SELECT COUNT(*), SUM(index_status='{ChunkIndexStatus.INDEXED}') "
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
                        created_at=str(r[7]) if r[7] else "", decided_at=str(r[8]) if r[8] else "",
                        decision_note=(str(r[10])[:200] if (status == "rejected" and len(r) > 10 and r[10]) else "")))
        finally:
            conn.close()
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_my_access_requests 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"我的授权申请查询失败 (trace: {trace_id})")
    return MyAccessRequestListResponse(items=items, truncated=_truncated)


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
                # 阶段 B（codex 缺口③）：审批权按**当前文档三元组**现裁，不再只信申请行的
                # owner_dept 快照——归属迁移后旧属主管理员经快照仍能操作正是要堵的洞。
                # 快照列降级为纯展示/审计。文档行被删（极端）→ 回退快照判定（收权动作仍可执行）。
                _cap = _kb_node_capability(cur)
                _mc = ", acl_mode, owner_dept_id" if _cap == "present" else ""
                cur.execute(f"SELECT owner_dept, status{_mc} FROM {_kb_db()}.document_meta "
                            "WHERE doc_id=%s FOR UPDATE", (doc_id,))
                drow = cur.fetchone()
                if drow:
                    _d_owner = drow[0] or ""
                    _mode, _oid = ((drow[2] or "legacy"), drow[3]) if _cap == "present" else ("legacy", None)
                    if not _kb_can_manage_doc(kb, _mode, _d_owner, _oid):
                        raise HTTPException(status_code=403, detail="无权操作该申请（非文档所属部门管理员）")
                    # M6：node 文档禁止**新增/扩大** legacy 授权（approve）；收权（reject/revoke）放行
                    if _mode == "node" and decision == "approved":
                        raise HTTPException(status_code=400,
                                            detail="该文档已迁组织树授权模式，组码申请不可批准（请驳回并改用可见范围）")
                elif not _kb_can_manage(kb, owner_dept):
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
                if doc_id:
                    from opensearch_pipeline.access_grants import (
                        materialize_doc_allowed_depts, record_acl_projection_invalidation,
                    )
                    # 持久入队（同事务、不吞异常）：权威变更与投影意图原子提交——enqueue 失败则整笔回滚，
                    # 绝不出现「权威已改而无 outbox 行」的撕裂。stage-3 outbox drain 据此定向幂等重试至成功。
                    # C3′/062（Sam 2026-08-03 拍板）：**bump+入队不受 flag 门控** —— flag 关闭期间
                    # 发生的权威变更若不 bump，水位永久丢失，开 flag 后这批文档永远判 unchanged
                    # = 原样复现 C3。materialize（消费侧）仍按 flag 门控。
                    record_acl_projection_invalidation(cur, doc_id, reason=decision)
                    # 内联标脏 = best-effort 快路径：成功则本轮 stage-3 即可重推；抛/skipped_locked → 上面
                    # 的 outbox 行兜底（+ allowed_depts_reconcile 全扫）。失败只记日志、**不回滚 status**。
                    # ⚠️ 消费侧（materialize）仍按 flag 门控 —— 只有上面的 bump+入队不受门控。
                    if get_config().rag.allowed_depts_acl:
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
    # E#39：授权决策已提交（approve/reject/revoke 均经此唯一状态机）→ 主动失效检索侧
    # _deny_revoked_cross_dept 的 (doc_id → approved 组码) TTL 缓存，RAG_ACL_DENY_CACHE_TTL_S>0
    # 时保住「撤销即时生效」语义（默认 TTL=0 缓存恒空，本调用为廉价 no-op）。惰性 import：
    # 依赖方向 retriever ↛ api/routes（无环）；fail-open——失效失败只记日志，绝不影响已提交结果。
    try:
        from opensearch_pipeline.retriever import invalidate_deny_cache
        invalidate_deny_cache(doc_id or None)
    except Exception as _ie:   # noqa: BLE001
        logger.warning("deny 缓存失效失败（忽略，TTL 兜底）doc=%s: %s", doc_id, _ie)
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
                # 阶段 B：node 管辖根（LEFT JOIN dept_dim 取现名，含失活节点——不悄悄隐藏授权意图）。
                # 独立 try：060 未 apply 时只让节点轴为空，legacy 名单照常。
                node_roots: Dict[str, List[KbAdminNodeRoot]] = {}
                try:
                    cur.execute(
                        f"SELECT g.user_id, g.managed_dept_id, g.source, d.name, d.is_active "
                        f"FROM {_kb_db()}.dept_admin_node_grant g "
                        f"LEFT JOIN {_kb_db()}.dept_dim d ON d.dept_id = g.managed_dept_id "
                        "WHERE g.is_active=1")
                    for r in cur.fetchall():
                        if r and r[0]:
                            node_roots.setdefault(r[0], []).append(KbAdminNodeRoot(
                                dept_id=int(r[1] or 0), source=r[2] or "manual",
                                name=(r[3] or str(r[1] or "")) + ("" if (r[4] is None or r[4]) else "（已失效节点）"),
                                active=bool(r[4]) if r[4] is not None else False))
                except Exception as ne:   # noqa: BLE001
                    logger.debug("dept_admin_node_grant 名单读取失败（节点轴按空展示）: %s", ne)
                for r in roles:
                    uid = r[0] or ""
                    items.append(KbAdminItem(
                        user_id=uid, user_name=r[1] or "", role=r[3] or "",
                        managed_owner_depts=sorted(grants.get(uid, [])),
                        managed_node_roots=sorted(node_roots.get(uid, []),
                                                  key=lambda n: n.dept_id)))
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
    from opensearch_pipeline.audit_log import mask_staff_id, write_audit
    uid = (req.user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="缺少 user_id（钉钉 staffId）")
    if uid == kb.user_id:
        raise HTTPException(status_code=400, detail="不能修改自己的角色/授权")
    depts = sanitize_owner_depts(req.owner_depts)   # 净化 + 写白名单（fail-closed 丢非法）
    # 阶段 B：node_roots=None 不动节点轴 / []=清空 / [ids]=该轴权威全集（覆盖按轴隔离）
    roots: Optional[List[int]] = None
    if req.node_roots is not None:
        roots = []
        for v in req.node_roots:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"非法管辖节点 id：{v!r}")
            if iv <= 1:
                raise HTTPException(status_code=400, detail="管辖根不得是钉钉根节点（全库语义归 kb_admin）")
            if iv not in roots:
                roots.append(iv)
    if not depts and not roots and roots is None:
        # 两轴都没给任何授权（node_roots=None 且 depts 空）——与既有 400 语义一致
        raise HTTPException(status_code=400, detail="可管理部门为空或全不在白名单（无法授予）")
    if not depts and roots == []:
        raise HTTPException(status_code=400, detail="两条授权轴均为空（如需撤权请走撤销接口）")
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
                # 管辖根必须在册且 active（授权给已消失节点 = 管辖面为空且无从解释）
                if roots:
                    ph = ",".join(["%s"] * len(roots))
                    cur.execute(f"SELECT dept_id FROM {_kb_db()}.dept_dim "
                                f"WHERE dept_id IN ({ph}) AND is_active=1", tuple(roots))
                    alive = {int(r[0]) for r in cur.fetchall()}
                    dead = [r for r in roots if r not in alive]
                    if dead:
                        raise HTTPException(status_code=400,
                                            detail=f"管辖节点不在组织快照中或已失效：{dead}")
                # 角色 → dept_admin。dept_code（兼作读组）仅在 legacy 轴非空时同步为组 CSV——
                # node-only 授权绝不覆写 dept_code：写空串会把该用户的**读组**清成仅 public。
                if depts:
                    cur.execute(f"INSERT INTO {_kb_db()}.user_role (user_id, user_name, dept_code, role, is_active) "
                                "VALUES (%s,%s,%s,%s,1) ON DUPLICATE KEY UPDATE "
                                "user_name=COALESCE(VALUES(user_name), user_name), dept_code=VALUES(dept_code), "
                                "role=VALUES(role), is_active=1, updated_at=NOW()",
                                (uid, (req.user_name or None), ",".join(depts), ROLE_DEPT_ADMIN))
                else:
                    cur.execute(f"INSERT INTO {_kb_db()}.user_role (user_id, user_name, dept_code, role, is_active) "
                                "VALUES (%s,%s,'',%s,1) ON DUPLICATE KEY UPDATE "
                                "user_name=COALESCE(VALUES(user_name), user_name), "
                                "role=VALUES(role), is_active=1, updated_at=NOW()",
                                (uid, (req.user_name or None), ROLE_DEPT_ADMIN))
                # legacy 轴：权威全集语义（先软撤销未包含的旧授权,再 upsert）。
                # ⚠️ 仅在 owner_depts 明确非空时覆盖——纯 node 授权请求不得清空既有 legacy 轴。
                if depts:
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
                # node 轴：manual 权威全集（manual 存在 ⇒ auto 失效，设计规则 2——
                # 撤停范围含 auto 行；[] = 清空整轴）
                if roots is not None:
                    if roots:
                        ph = ",".join(["%s"] * len(roots))
                        cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_grant SET is_active=0, updated_at=NOW() "
                                    f"WHERE user_id=%s AND is_active=1 AND managed_dept_id NOT IN ({ph})",
                                    (uid, *roots))
                        for root in roots:
                            cur.execute(f"INSERT INTO {_kb_db()}.dept_admin_node_grant "
                                        "(user_id, managed_dept_id, source, granted_by, note, is_active) "
                                        "VALUES (%s,%s,'manual',%s,%s,1) "
                                        "ON DUPLICATE KEY UPDATE is_active=1, granted_by=VALUES(granted_by), "
                                        "note=VALUES(note), updated_at=NOW()",
                                        (uid, root, kb.user_id, note))
                    else:
                        cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_grant SET is_active=0, updated_at=NOW() "
                                    "WHERE user_id=%s AND is_active=1", (uid,))
                # 同事务审计（B1）：与角色/授权变更原子提交。
                # staffId 掩码写入（首4…尾4）：展示侧 approval-history 对 message 无条件
                # redact_text，完整 16-19 位 staffId 会撞 bank_card 规则整段变占位符。
                write_audit(doc_id=None, version_no=None, action_type="KB_ADMIN_GRANT",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"grant dept_admin {mask_staff_id(uid)} → depts={','.join(depts) or '-'} "
                                    f"nodes={','.join(str(r) for r in (roots or [])) if roots is not None else 'unchanged'}",
                            cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_admin_grant 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"授予部门管理员失败 (trace: {trace_id})")
    return KbAdminGrantResponse(user_id=uid, role=ROLE_DEPT_ADMIN, managed_owner_depts=depts,
                                managed_node_roots=list(roots or []), ok=True)


@router.post("/api/kb/admin-grants/revoke", response_model=KbAdminGrantResponse)
def kb_admin_grant_revoke(req: KbAdminRevokeRequest, request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 撤销部门管理员授权：owner_dept 指定→撤该一项；为空→撤全部并降级 employee。
    无活跃授权剩余时把 user_role.role 降为 employee（即时失去管理入口）。kb_admin/自身不可经此撤销。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_admin(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, ROLE_EMPLOYEE
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import mask_staff_id, write_audit
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
                node_root = int(getattr(req, "node_root", 0) or 0)
                if owner:
                    cur.execute(f"UPDATE {_kb_db()}.dept_admin_grant SET is_active=0, updated_at=NOW() "
                                "WHERE user_id=%s AND managed_owner_dept=%s AND is_active=1", (uid, owner))
                elif node_root:
                    cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_grant SET is_active=0, updated_at=NOW() "
                                "WHERE user_id=%s AND managed_dept_id=%s AND is_active=1", (uid, node_root))
                else:
                    # 全撤 = 两轴一起（node 轴独立 try：060 未 apply 环境 legacy 撤销照常）
                    cur.execute(f"UPDATE {_kb_db()}.dept_admin_grant SET is_active=0, updated_at=NOW() "
                                "WHERE user_id=%s AND is_active=1", (uid,))
                    try:
                        cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_grant SET is_active=0, updated_at=NOW() "
                                    "WHERE user_id=%s AND is_active=1", (uid,))
                    except Exception as ne:   # noqa: BLE001
                        logger.debug("dept_admin_node_grant 全撤跳过（表缺失?）: %s", ne)
                cur.execute(f"SELECT COUNT(*) FROM {_kb_db()}.dept_admin_grant "
                            "WHERE user_id=%s AND is_active=1", (uid,))
                remaining = int(cur.fetchone()[0] or 0)
                # 阶段 B：降级判定看**两表**——node-only 管理员在 legacy 表恒 0 行，
                # 只数一张表会把还持有节点管辖的管理员误降 employee（codex major M3）。
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {_kb_db()}.dept_admin_node_grant "
                                "WHERE user_id=%s AND is_active=1", (uid,))
                    remaining += int(cur.fetchone()[0] or 0)
                except Exception as ne:   # noqa: BLE001 — 060 未 apply：节点轴按 0 计
                    logger.debug("dept_admin_node_grant 剩余计数跳过（表缺失?）: %s", ne)
                if remaining == 0:
                    cur.execute(f"UPDATE {_kb_db()}.user_role SET role=%s, updated_at=NOW() "
                                "WHERE user_id=%s", (ROLE_EMPLOYEE, uid))
                    demoted = True
                # 同事务审计（B1）：与撤销/降级变更原子提交。staffId 掩码同 grant 侧。
                write_audit(doc_id=None, version_no=None, action_type="KB_ADMIN_REVOKE",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"revoke {mask_staff_id(uid)} owner={owner or '-'} node={node_root or '-'} "
                                    f"demoted={demoted}", cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_admin_grant_revoke 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"撤销部门管理员授权失败 (trace: {trace_id})")
    return KbAdminGrantResponse(user_id=uid, role=(ROLE_EMPLOYEE if demoted else "dept_admin"), ok=True)


# ═══════════════════════════════════════════════════════════════
# 阶段 B T5 — 管辖根候选确认（kb_admin 专属）
#   org_sync 每日派生把「中心级/超规模/换根」的 auto 根写进 dept_admin_node_candidate
#   （权威表绝不进待确认态）；本组端点 = 确认队列的读与裁决。
#   TOCTOU 关死：确认动作事务内比对 derived_snapshot_rev 与当前快照 rev——不符 ⇒ 409 +
#   按当前快照重派生返回最新候选（codex 阶段 B 评审共识），绝不确认陈旧候选。
# ═══════════════════════════════════════════════════════════════
class KbNodeCandidateItem(BaseModel):
    id: int = 0
    user_id: str = ""
    user_name: str = ""
    dept_id: int = 0
    dept_name: str = ""
    risk_reason: str = ""
    derived_snapshot_rev: int = 0
    created_at: str = ""


class KbNodeCandidateListResponse(BaseModel):
    items: List[KbNodeCandidateItem] = Field(default_factory=list)
    # P3-3（2026-08-04）：本端点是**硬 LIMIT 队列**，此前截断完全不外露 —— 队列超过上限时
    # 使用者看到的「就这些」只是前 N 条，**且无从知道**。与 B8（差评复核）同族：
    # 先让截断不再静默；真分页是另一回事（需稳定排序键 + 前端翻页，另议）。
    truncated: bool = False


class KbNodeCandidateDecideRequest(BaseModel):
    candidate_id: int = 0
    action: str = ""                       # confirm | reject


class KbNodeCandidateDecideResponse(BaseModel):
    ok: bool = True
    status: str = ""                       # confirmed | rejected | superseded
    latest: Optional[KbNodeCandidateItem] = None   # 409 时携带按当前快照重派生的候选


@router.get("/api/kb/admin-node-candidates", response_model=KbNodeCandidateListResponse)
def kb_node_candidates_list(request: Request,
                            identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 查看待确认的管辖根候选。只读。"""
    _enforce_rate_limit(request, identity, scope="aux")
    _require_kb_admin(identity)
    items: List[KbNodeCandidateItem] = []
    _truncated = False   # P3-3：与 items 同初始化——早退/降级路径下也要有确定值
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT c.id, c.user_id, COALESCE(u.user_name,''), c.proposed_dept_id, "
                    f"COALESCE(d.name,''), c.risk_reason, c.derived_snapshot_rev, c.created_at "
                    f"FROM {_kb_db()}.dept_admin_node_candidate c "
                    f"LEFT JOIN {_kb_db()}.dept_dim d ON d.dept_id = c.proposed_dept_id "
                    f"LEFT JOIN {_kb_db()}.user_role u ON u.user_id = c.user_id AND u.is_active=1 "
                    "WHERE c.status='pending' ORDER BY c.created_at ASC LIMIT 201")   # 200+1 探针行（P3-3）
                _rows = cur.fetchall()
                _truncated = len(_rows) > 200
                for r in _rows[:200]:
                    items.append(KbNodeCandidateItem(
                        id=int(r[0]), user_id=r[1] or "", user_name=r[2] or "",
                        dept_id=int(r[3] or 0), dept_name=r[4] or str(r[3] or ""),
                        risk_reason=r[5] or "", derived_snapshot_rev=int(r[6] or 0),
                        created_at=str(r[7]) if r[7] else ""))
        finally:
            conn.close()
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_node_candidates_list 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"候选队列查询失败 (trace: {trace_id})")
    return KbNodeCandidateListResponse(items=items, truncated=_truncated)


def _candidate_risk(cur, dept_id: int) -> Optional[str]:
    """事务内按当前快照评估候选风险（与 org_sync.derive_admin_node_roots 同口径）。
    返回 None=无风险；节点失活/缺行按 'root_gone' 处理（不可确认）。"""
    cur.execute(f"SELECT depth FROM {_kb_db()}.dept_dim WHERE dept_id=%s AND is_active=1",
                (dept_id,))
    row = cur.fetchone()
    if not row:
        return "root_gone"
    if int(row[0] or 0) == 1:
        return "center_level"
    cur.execute(f"SELECT dept_id, parent_id FROM {_kb_db()}.dept_dim WHERE is_active=1")
    children: Dict[int, List[int]] = {}
    for r in cur.fetchall():
        children.setdefault(int(r[1]), []).append(int(r[0]))
    from opensearch_pipeline.dept_ancestry import resolve_descendant_ids
    from opensearch_pipeline.org_sync import _auto_max_subtree
    got, ok = resolve_descendant_ids(children, [dept_id])
    if not ok:
        return "root_gone"
    return "oversized" if len(got) > _auto_max_subtree() else None


@router.post("/api/kb/admin-node-candidates/decide", response_model=KbNodeCandidateDecideResponse)
def kb_node_candidate_decide(req: KbNodeCandidateDecideRequest, request: Request,
                             identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 确认/驳回一条管辖根候选。

    confirm 事务内五重校验（缺一不可，全部对当前状态现查）：
      ①候选仍 pending（FOR UPDATE 串行化并发裁决）；②derived_snapshot_rev == 当前快照 rev
      （不符 ⇒ 409 + 按当前快照重派生返回最新候选——同 (user,root) 直接刷新本行，挂靠已变
      则标 superseded）；③目标用户仍是 active dept_admin；④无 manual override（manual 存在
      ⇒ auto 失效，规则 2——候选直接 superseded）；⑤根节点仍在册 active。
    确认落地 = 撤停该用户其它 auto 行 + upsert 权威表 source='auto' + 候选 confirmed。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_admin(identity)
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import mask_staff_id, write_audit
    cid = int(req.candidate_id or 0)
    action = (req.action or "").strip().lower()
    if not cid or action not in ("confirm", "reject"):
        raise HTTPException(status_code=400, detail="缺少 candidate_id 或非法 action")
    assert_metadata_write_allowed("kb_node_candidate_decide", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT user_id, proposed_dept_id, derived_snapshot_rev, status "
                            f"FROM {_kb_db()}.dept_admin_node_candidate WHERE id=%s FOR UPDATE", (cid,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="候选不存在")
                uid, root, c_rev, status = row[0] or "", int(row[1] or 0), int(row[2] or 0), row[3] or ""
                if status != "pending":
                    raise HTTPException(status_code=409, detail=f"候选已处于 {status} 状态")
                if action == "reject":
                    cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_candidate "
                                "SET status='rejected', confirmed_by=%s, confirmed_at=NOW() "
                                "WHERE id=%s", (kb.user_id, cid))
                    write_audit(doc_id=None, version_no=None, action_type="KB_NODE_ROOT_REJECT",
                                operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                                message=f"reject candidate#{cid} {mask_staff_id(uid)} → node {root}",
                                cursor=cur)
                    conn.commit()
                    return KbNodeCandidateDecideResponse(ok=True, status="rejected")
                # ── confirm ──
                cur.execute(f"SELECT COALESCE(MAX(snapshot_rev),0) FROM {_kb_db()}.dept_dim "
                            "WHERE is_active=1")
                cur_rev = int((cur.fetchone() or (0,))[0] or 0)
                if cur_rev != c_rev:
                    # TOCTOU：快照已翻——按当前快照重派生。挂靠仍指向同一根 ⇒ 刷新本行
                    # （rev+risk）回 409 让 kb_admin 基于最新事实再按一次；挂靠已变 ⇒ superseded。
                    cur.execute(f"SELECT dept_ids FROM {_kb_db()}.staff_dim "
                                "WHERE staff_id=%s AND is_active=1", (uid,))
                    srow = cur.fetchone()
                    direct = [int(x) for x in str((srow or ("",))[0] or "").split(",") if x.strip().isdigit()]
                    if len(direct) == 1 and direct[0] == root:
                        risk = _candidate_risk(cur, root) or "root_changed"
                        cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_candidate "
                                    "SET derived_snapshot_rev=%s, risk_reason=%s, updated_at=NOW() "
                                    "WHERE id=%s", (cur_rev, risk, cid))
                        conn.commit()
                        return_latest = KbNodeCandidateItem(
                            id=cid, user_id=uid, dept_id=root, risk_reason=risk,
                            derived_snapshot_rev=cur_rev)
                        raise HTTPException(status_code=409, detail={
                            "message": "组织快照已更新，候选已按当前快照重派生，请确认最新候选",
                            "latest": return_latest.model_dump()})
                    cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_candidate "
                                "SET status='superseded', updated_at=NOW() WHERE id=%s", (cid,))
                    conn.commit()
                    raise HTTPException(status_code=409,
                                        detail="该用户挂靠已变化，候选失效（等下次同步或在成员面板手动指定）")
                # ③ 用户仍是 active dept_admin
                cur.execute(f"SELECT role FROM {_kb_db()}.user_role WHERE user_id=%s AND is_active=1 "
                            "ORDER BY updated_at DESC, id DESC LIMIT 1", (uid,))
                rrow = cur.fetchone()
                if not rrow or (rrow[0] or "") != "dept_admin":
                    cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_candidate "
                                "SET status='superseded', updated_at=NOW() WHERE id=%s", (cid,))
                    conn.commit()
                    raise HTTPException(status_code=409, detail="该用户已不是部门管理员，候选失效")
                # ④ manual override ⇒ auto 失效（规则 2）
                cur.execute(f"SELECT 1 FROM {_kb_db()}.dept_admin_node_grant "
                            "WHERE user_id=%s AND source='manual' AND is_active=1 LIMIT 1", (uid,))
                if cur.fetchone():
                    cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_candidate "
                                "SET status='superseded', updated_at=NOW() WHERE id=%s", (cid,))
                    conn.commit()
                    raise HTTPException(status_code=409, detail="该用户已有手动管辖授权（manual 覆盖 auto），候选失效")
                # ⑤ 根仍在册 active
                cur.execute(f"SELECT 1 FROM {_kb_db()}.dept_dim WHERE dept_id=%s AND is_active=1", (root,))
                if not cur.fetchone():
                    cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_candidate "
                                "SET status='superseded', updated_at=NOW() WHERE id=%s", (cid,))
                    conn.commit()
                    raise HTTPException(status_code=409, detail="目标节点已从组织架构消失，候选失效")
                # 落地：撤其它 auto 行 + upsert 权威 + 候选 confirmed（同事务）
                cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_grant SET is_active=0, updated_at=NOW() "
                            "WHERE user_id=%s AND source='auto' AND is_active=1 AND managed_dept_id<>%s",
                            (uid, root))
                cur.execute(f"INSERT INTO {_kb_db()}.dept_admin_node_grant "
                            "(user_id, managed_dept_id, source, granted_by, note, is_active) "
                            "VALUES (%s,%s,'auto',%s,%s,1) "
                            "ON DUPLICATE KEY UPDATE is_active=1, granted_by=VALUES(granted_by), "
                            "note=VALUES(note), updated_at=NOW()",
                            (uid, root, kb.user_id, f"confirmed candidate#{cid}"))
                cur.execute(f"UPDATE {_kb_db()}.dept_admin_node_candidate "
                            "SET status='confirmed', confirmed_by=%s, confirmed_at=NOW() "
                            "WHERE id=%s", (kb.user_id, cid))
                write_audit(doc_id=None, version_no=None, action_type="KB_NODE_ROOT_CONFIRM",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"confirm candidate#{cid} {mask_staff_id(uid)} → node {root}",
                            cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_node_candidate_decide 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"候选裁决失败 (trace: {trace_id})")
    return KbNodeCandidateDecideResponse(ok=True, status="confirmed")
