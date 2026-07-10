# -*- coding: utf-8 -*-
"""
routes/ontology.py — steward 消解工作台（本体 P0 PR6；docs/ontology_p0_plan_2026-07-10.md）。

**门 B**：工作台批量处置在 resolution_case / ontology_identifier 自身状态机上进行
（kb_access 范式），**不写 approval_request**——25 表的 run_id 锚定 agent_run，且 B6
对账 reaper 会重驱 decided-but-not-resumed，无 run 的合成审批单会被误重驱。会话内的
受治理 Action（门 A）另走 v2 Approval（PR8）。

授权（S5）：kb_admin 恒可；dept_admin 须 stewardship scope 裁决出的 steward_dept ∈
managed_owner_depts（resolve_kb_identity **DB 权威现查**，与 routes/agent.py 审批授权
同纪律）；scope 未命中 → 仅 kb_admin（fail-closed）。普通员工无入口。

留痕：所有变更动作写 agent_audit_log（event_type='ontology_workbench'，fail-open——
审计抖动不阻断治理操作；这与 HIGH_WRITE 工具的 fail-closed 语义不同，工作台动作
本身就是人审）。flag `RAG_ONTOLOGY_ENABLE` 默认 off → 全部端点 404（镜像 agent.py）。
"""
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from opensearch_pipeline.api import (  # noqa: E402  顶层 from-import api 共享件（同 agent.py 惯例）
    Identity,
    _enforce_rate_limit,
    current_identity,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_STORE = None


def _ontology_enabled() -> bool:
    return os.environ.get("RAG_ONTOLOGY_ENABLE", "").strip().lower() in ("1", "true", "yes", "on")


def _get_store():
    """惰性单例（flag-off 时永不建）；测试 monkeypatch 本函数。"""
    global _STORE
    if _STORE is None:
        from opensearch_pipeline.ontology.store import RDSOntologyStore
        _STORE = RDSOntologyStore()
    return _STORE


def _audit(*, action: str, decision: str, by: str, detail: Optional[Dict[str, Any]] = None):
    """治理留痕（fail-open）；测试 monkeypatch 本函数断言留痕。"""
    try:
        from opensearch_pipeline.agent_runtime.audit import RDSAuditLog
        RDSAuditLog().record(None, event_type="ontology_workbench", action=action,
                             decision=decision, detail={"by": by, **(detail or {})})
    except Exception:   # noqa: BLE001
        logger.warning("ontology 工作台审计写失败（fail-open）", exc_info=True)


# ── 门禁 ─────────────────────────────────────────────────────────────────────
def _require_enabled_identity(identity: Optional[Identity]) -> Identity:
    if not _ontology_enabled():
        raise HTTPException(status_code=404, detail="Not Found")   # 隐藏入口
    if identity is None:
        raise HTTPException(status_code=401, detail="需要登录")
    return identity


def _kb_identity(identity: Identity):
    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    return resolve_kb_identity(identity.user_id)


def _require_reader(identity: Identity):
    """读侧门：kb_admin / dept_admin（DB 现查）。返回 kb 身份供写侧复用。"""
    from opensearch_pipeline.kb_authz import ROLE_DEPT_ADMIN, ROLE_KB_ADMIN
    kb = _kb_identity(identity)
    if kb.role not in (ROLE_KB_ADMIN, ROLE_DEPT_ADMIN):
        raise HTTPException(status_code=403, detail="无权访问消解工作台（需 dept_admin / kb_admin）")
    return kb


def _steward_dept_for(store, *, namespace: Optional[str] = None,
                      object_type: Optional[str] = None) -> Optional[str]:
    from opensearch_pipeline.ontology.stewardship import resolve_steward
    try:
        rows = store.list_stewardship()
    except Exception:   # noqa: BLE001 — 表故障 → scope 未知 → fail-closed 到 kb_admin
        logger.warning("stewardship 读取失败（授权收敛到 kb_admin）", exc_info=True)
        return None
    hit = resolve_steward(rows, namespace=namespace, object_type=object_type)
    return hit["steward_dept"] if hit else None


def _authorize_steward(identity: Identity, kb, steward_dept: Optional[str]) -> None:
    """写侧门：kb_admin 恒可；dept_admin 须 scope 命中且 steward_dept ∈ managed；
    scope 未命中（steward_dept=None）→ 仅 kb_admin（fail-closed）。"""
    from opensearch_pipeline.kb_authz import ROLE_DEPT_ADMIN, ROLE_KB_ADMIN, managed_owner_depts
    if kb.role == ROLE_KB_ADMIN:
        return
    if kb.role == ROLE_DEPT_ADMIN and steward_dept \
            and steward_dept in set(managed_owner_depts(kb)):
        return
    raise HTTPException(
        status_code=403,
        detail=f"无权处置该条目（steward={steward_dept or '未登记'}，需 kb_admin 或对应 dept_admin）")


# ── 读侧：队列 / 覆盖率 / 详情 / 对象搜索 ─────────────────────────────────────
def _enrich_candidates(store, case_id: str, top_n: int = 3) -> List[Dict[str, Any]]:
    out = []
    for c in store.list_candidates(case_id)[:top_n]:
        obj = store.get_object(c["target_object_id"]) or {}
        out.append({**c, "canonical_ref": obj.get("canonical_ref"), "title": obj.get("title"),
                    "object_type": obj.get("object_type"), "target_status": obj.get("status")})
    return out


@router.get("/api/ontology/workbench")
def ontology_workbench(request: Request, namespace: Optional[str] = None,
                       object_type: Optional[str] = None, order: str = "freq",
                       limit: int = 50, offset: int = 0,
                       identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    _require_reader(identity)
    store = _get_store()
    items = []
    for case in store.list_open_cases(namespace=namespace, object_type_hint=object_type,
                                      order=order, limit=limit, offset=offset):
        items.append({**case,
                      "candidates": _enrich_candidates(store, case["case_id"]),
                      "steward_dept": _steward_dept_for(
                          store, namespace=case.get("namespace"),
                          object_type=case.get("object_type_hint"))})
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/api/ontology/coverage")
def ontology_coverage(request: Request, object_type: Optional[str] = None,
                      identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    _require_reader(identity)
    cov = _get_store().coverage(object_type=object_type)
    active, auto = cov["active_identifiers"], cov["auto_active"]
    cov["manual_review_rate"] = (1 - auto / active) if active else None   # 人工审核率（S9）
    return cov


@router.get("/api/ontology/cases/{case_id}")
def ontology_case_detail(case_id: str, request: Request,
                         identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    _require_reader(identity)
    store = _get_store()
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case 不存在")
    return {**case, "candidates": _enrich_candidates(store, case_id, top_n=10),
            "steward_dept": _steward_dept_for(store, namespace=case.get("namespace"),
                                              object_type=case.get("object_type_hint"))}


@router.get("/api/ontology/objects")
def ontology_objects_search(request: Request, object_type: str, q: Optional[str] = None,
                            limit: int = 20,
                            identity: Optional[Identity] = Depends(current_identity)):
    """确认/改指时的目标对象选择器。"""
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    _require_reader(identity)
    return {"items": _get_store().find_objects(object_type, title_like=q, limit=limit)}


@router.get("/api/ontology/objects/{object_id}")
def ontology_object_detail(object_id: str, request: Request,
                           identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    _require_reader(identity)
    store = _get_store()
    obj = store.get_object(object_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="对象不存在")
    return {**obj, "identifiers": store.list_identifiers_for_target(object_id)}


# ── 写侧：case 处置 ───────────────────────────────────────────────────────────
class ConfirmRequest(BaseModel):
    target_object_id: str
    target_revision: Optional[str] = None
    relation: str = "alias"
    note: Optional[str] = None


@router.post("/api/ontology/cases/{case_id}/confirm")
def ontology_case_confirm(case_id: str, req: ConfirmRequest, request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """确认（含改指到非候选目标）：铸 active 别名 + case→resolved。"""
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    store = _get_store()
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case 不存在")
    if case["status"] != "open":
        raise HTTPException(status_code=409, detail=f"case 非 open（{case['status']}）")
    _authorize_steward(identity, kb, _steward_dept_for(
        store, namespace=case["namespace"], object_type=case.get("object_type_hint")))
    target = store.get_object(req.target_object_id)
    if target is None:
        raise HTTPException(status_code=404, detail="目标对象不存在")
    if target["status"] != "active":
        raise HTTPException(status_code=409, detail=f"目标对象非 active（{target['status']}）")
    from opensearch_pipeline.ontology.store import DuplicateActiveIdentifier
    try:
        identifier_id = store.insert_identifier(
            case["namespace"], case["raw_value"], case["norm_value"], req.target_object_id,
            method="manual", relation=req.relation, target_revision=req.target_revision,
            confidence=1.0, confirmed_by=identity.user_id, source_case_id=case_id)
    except DuplicateActiveIdentifier:
        raise HTTPException(status_code=409,
                            detail="该编号已有正式映射——请先在对象详情里纠错（改指/退役）")
    if not store.resolve_case(case_id, identifier_id=identifier_id, by=identity.user_id,
                              note=req.note):
        # 并发方先处置了 case：补偿刚铸的别名，绝不留「case 已关但别名悄悄生效」
        store.deactivate_identifier(identifier_id, status="rejected")
        raise HTTPException(status_code=409, detail="case 已被并发处置（本次确认已回滚）")
    _audit(action=f"case:{case_id}", decision="confirm", by=identity.user_id,
           detail={"namespace": case["namespace"], "norm_value": case["norm_value"],
                   "target_object_id": req.target_object_id,
                   "target_revision": req.target_revision,
                   "identifier_id": identifier_id, "note": (req.note or "")[:200]})
    return {"case_id": case_id, "status": "resolved", "identifier_id": identifier_id}


class DismissRequest(BaseModel):
    note: str


@router.post("/api/ontology/cases/{case_id}/dismiss")
def ontology_case_dismiss(case_id: str, req: DismissRequest, request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    store = _get_store()
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case 不存在")
    _authorize_steward(identity, kb, _steward_dept_for(
        store, namespace=case["namespace"], object_type=case.get("object_type_hint")))
    try:
        ok = store.dismiss_case(case_id, by=identity.user_id, note=req.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=409, detail=f"case 非 open（{case['status']}）")
    _audit(action=f"case:{case_id}", decision="dismiss", by=identity.user_id,
           detail={"namespace": case["namespace"], "norm_value": case["norm_value"],
                   "note": req.note[:200]})
    return {"case_id": case_id, "status": "dismissed"}


# ── 写侧：S3 最小纠错 ─────────────────────────────────────────────────────────
class NoteRequest(BaseModel):
    note: Optional[str] = None


def _load_identifier_scope(store, identifier_id: str):
    row = store.get_identifier(identifier_id)
    if row is None:
        raise HTTPException(status_code=404, detail="identifier 不存在")
    obj = store.get_object(row["target_object_id"]) or {}
    return row, _steward_dept_for(store, namespace=row["namespace"],
                                  object_type=obj.get("object_type"))


@router.post("/api/ontology/identifiers/{identifier_id}/deactivate")
def ontology_identifier_deactivate(identifier_id: str, req: NoteRequest, request: Request,
                                   identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    store = _get_store()
    row, steward = _load_identifier_scope(store, identifier_id)
    _authorize_steward(identity, kb, steward)
    if not store.deactivate_identifier(identifier_id, status="rejected"):
        raise HTTPException(status_code=409, detail=f"identifier 非 active（{row['status']}）")
    _audit(action=f"identifier:{identifier_id}", decision="deactivate", by=identity.user_id,
           detail={"namespace": row["namespace"], "norm_value": row["norm_value"],
                   "note": (req.note or "")[:200]})
    return {"identifier_id": identifier_id, "status": "rejected"}


class RepointRequest(BaseModel):
    target_object_id: str
    target_revision: Optional[str] = None
    note: Optional[str] = None


@router.post("/api/ontology/identifiers/{identifier_id}/repoint")
def ontology_identifier_repoint(identifier_id: str, req: RepointRequest, request: Request,
                                identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    store = _get_store()
    row, steward = _load_identifier_scope(store, identifier_id)
    _authorize_steward(identity, kb, steward)
    target = store.get_object(req.target_object_id)
    if target is None:
        raise HTTPException(status_code=404, detail="目标对象不存在")
    if target["status"] != "active":
        raise HTTPException(status_code=409, detail=f"目标对象非 active（{target['status']}）")
    try:
        new_id = store.repoint_identifier(identifier_id, req.target_object_id,
                                          by=identity.user_id,
                                          new_target_revision=req.target_revision)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    _audit(action=f"identifier:{identifier_id}", decision="repoint", by=identity.user_id,
           detail={"namespace": row["namespace"], "norm_value": row["norm_value"],
                   "old_target": row["target_object_id"], "new_target": req.target_object_id,
                   "new_identifier_id": new_id, "note": (req.note or "")[:200]})
    return {"identifier_id": identifier_id, "new_identifier_id": new_id, "status": "superseded"}


@router.post("/api/ontology/objects/{object_id}/retire")
def ontology_object_retire(object_id: str, req: NoteRequest, request: Request,
                           identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    store = _get_store()
    obj = store.get_object(object_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="对象不存在")
    _authorize_steward(identity, kb,
                       _steward_dept_for(store, object_type=obj["object_type"]))
    if not store.retire_object(object_id):
        raise HTTPException(status_code=409, detail=f"对象非 active（{obj['status']}）")
    _audit(action=f"object:{object_id}", decision="retire", by=identity.user_id,
           detail={"canonical_ref": obj.get("canonical_ref"), "note": (req.note or "")[:200]})
    return {"object_id": object_id, "status": "retired"}


class MarkDuplicateRequest(BaseModel):
    merged_into: str
    note: Optional[str] = None


@router.post("/api/ontology/objects/{object_id}/mark-duplicate")
def ontology_object_mark_duplicate(object_id: str, req: MarkDuplicateRequest, request: Request,
                                   identity: Optional[Identity] = Depends(current_identity)):
    """S3 仅标记（merged_into），不做关系/黄金记录传播——全量 merge/split 是 P2。"""
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    store = _get_store()
    obj = store.get_object(object_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="对象不存在")
    _authorize_steward(identity, kb,
                       _steward_dept_for(store, object_type=obj["object_type"]))
    target = store.get_object(req.merged_into)
    if target is None:
        raise HTTPException(status_code=404, detail="merged_into 对象不存在")
    if target["status"] != "active":
        raise HTTPException(status_code=409, detail=f"merged_into 非 active（{target['status']}）")
    try:
        ok = store.mark_duplicate(object_id, req.merged_into)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=409, detail=f"对象非 active（{obj['status']}）")
    _audit(action=f"object:{object_id}", decision="mark_duplicate", by=identity.user_id,
           detail={"merged_into": req.merged_into, "note": (req.note or "")[:200]})
    return {"object_id": object_id, "status": "merged", "merged_into": req.merged_into}
