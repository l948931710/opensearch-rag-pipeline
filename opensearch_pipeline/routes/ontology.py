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

留痕（PR-C，P0-06 收口）：所有变更动作的审计行与事实变更**同一事务**落
agent_audit_log（event_type='ontology_workbench'，**fail-closed**——审计不可写 →
整笔回滚 5xx，绝不出现"身份脊柱已改而审计无痕"）。审计载荷经 `_audit_payload`
构造、由 store 写方法在事务内落库。flag `RAG_ONTOLOGY_ENABLE` 默认 off →
全部端点 404（镜像 agent.py）。
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


def _audit_payload(*, action: str, decision: str, by: str,
                   detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """治理留痕载荷（PR-C fail-closed）：交给 store 写方法在**同一事务**内落
    agent_audit_log——审计不可写即整笔回滚，本函数只构造不落库。"""
    return {"event_type": "ontology_workbench", "action": action,
            "decision": decision, "by": by, "detail": detail or {}}


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


# ── 对象级 ACL（PR-B，P0-01）：读侧世界观 = kb_admin bypass / dept_admin 管辖组码 ──
def _reader_acl(kb):
    """(acl, bypass)：对象可见性判定输入（ontology.authz 同一世界观）。"""
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    if kb.role == ROLE_KB_ADMIN:
        return set(), True
    return set(managed_owner_depts(kb)), False


def _can_manage_case(kb, store, case: Dict[str, Any]) -> bool:
    """case 可见性 = 其 steward scope 是否归调用方管辖（evidence 属处置人，读写同域）。"""
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    if kb.role == ROLE_KB_ADMIN:
        return True
    steward = _steward_dept_for(store, namespace=case.get("namespace"),
                                object_type=case.get("object_type_hint"))
    return bool(steward) and steward in set(managed_owner_depts(kb))


def _managed_scope_filter(kb, store) -> Optional[Dict[str, List[str]]]:
    """dept_admin 的队列 SQL 粗过滤集（kb_admin → None=不过滤）。

    从 stewardship 表反算调用方管辖的 scope：namespace 全键 / namespace 冒号前缀 /
    object_type。SQL 层先按并集收窄（跨部门行不出库），返回后仍逐条
    `_can_manage_case` 精判（attribute > namespace > object_type 的裁决优先级可能
    在并集内又否掉个别行）——粗筛+精判双层，fail-closed。"""
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    if kb.role == ROLE_KB_ADMIN:
        return None
    managed = set(managed_owner_depts(kb))
    namespaces, prefixes, otypes = [], [], []
    try:
        rows = store.list_stewardship()
    except Exception:   # noqa: BLE001 — 表故障 → 空集（fail-closed：什么都看不见）
        logger.warning("stewardship 读取失败（队列过滤收敛为空集）", exc_info=True)
        rows = []
    for r in rows:
        if r.get("steward_dept") not in managed:
            continue
        key = r.get("scope_key") or ""
        if r.get("scope_type") == "namespace":
            (namespaces if ":" in key else prefixes).append(key)
        elif r.get("scope_type") == "object_type":
            otypes.append(key)
    return {"namespaces": namespaces, "namespace_prefixes": prefixes,
            "object_types": otypes}


def _raise_mutation_denied(reason: str) -> None:
    """authz.can_mutate_identity 的拒绝原因 → HTTP 语义（不可见=404 防存在性泄露）。"""
    if "不可见" in reason or "不存在" in reason:
        raise HTTPException(status_code=404, detail="目标对象不存在")
    if "非 active" in reason:
        raise HTTPException(status_code=409, detail=reason)
    raise HTTPException(status_code=400, detail=reason)


# ── 读侧：队列 / 覆盖率 / 详情 / 对象搜索 ─────────────────────────────────────
def _enrich_candidates(store, case_id: str, *, acl: set, bypass: bool,
                       top_n: int = 3) -> List[Dict[str, Any]]:
    """候选目标对象信息按对象级 ACL 出参（PR-B）：不可读目标只给 target_visible=False，
    不泄 ref/title/type——跨部门 confidential 候选正是 P0-01 的泄露面。"""
    from opensearch_pipeline.ontology.authz import can_read_object, visible_title
    out = []
    for c in store.list_candidates(case_id)[:top_n]:
        obj = store.get_object(c["target_object_id"]) or {}
        if not can_read_object(obj, acl=acl, bypass_acl=bypass):
            out.append({**c, "target_visible": False, "canonical_ref": None,
                        "title": None, "object_type": None, "target_status": None})
            continue
        out.append({**c, "target_visible": True,
                    "canonical_ref": obj.get("canonical_ref"),
                    "title": visible_title(obj.get("title"), obj.get("data_classification"),
                                           obj.get("owner_dept"), acl=acl, bypass_acl=bypass),
                    "object_type": obj.get("object_type"), "target_status": obj.get("status"),
                    # PR-F HITL：处置人须看见目标归属/密级再确认（P0-06 UI 验收⑥）
                    "owner_dept": obj.get("owner_dept"),
                    "data_classification": obj.get("data_classification")})
    return out


@router.get("/api/ontology/workbench")
def ontology_workbench(request: Request, namespace: Optional[str] = None,
                       object_type: Optional[str] = None, order: str = "freq",
                       limit: int = 50, offset: int = 0,
                       identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    store = _get_store()
    acl, bypass = _reader_acl(kb)
    scope = _managed_scope_filter(kb, store)   # None=kb_admin 不过滤
    items = []
    for case in store.list_open_cases(namespace=namespace, object_type_hint=object_type,
                                      order=order, limit=limit, offset=offset,
                                      scope_filter=scope):
        if not _can_manage_case(kb, store, case):   # 粗筛后的精判（裁决优先级，fail-closed）
            continue
        items.append({**case,
                      "candidates": _enrich_candidates(store, case["case_id"],
                                                       acl=acl, bypass=bypass),
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
    kb = _require_reader(identity)
    store = _get_store()
    case = store.get_case(case_id)
    # PR-B（P0-01）：scope 外的 case 与不存在同答 404——evidence_json 属处置人
    if case is None or not _can_manage_case(kb, store, case):
        raise HTTPException(status_code=404, detail="case 不存在")
    acl, bypass = _reader_acl(kb)
    return {**case,
            "candidates": _enrich_candidates(store, case_id, acl=acl, bypass=bypass,
                                             top_n=10),
            "steward_dept": _steward_dept_for(store, namespace=case.get("namespace"),
                                              object_type=case.get("object_type_hint"))}


@router.get("/api/ontology/objects")
def ontology_objects_search(request: Request, object_type: str, q: Optional[str] = None,
                            limit: int = 20,
                            identity: Optional[Identity] = Depends(current_identity)):
    """确认/改指时的目标对象选择器（PR-B：结果按对象级 ACL 过滤，不可读对象整行不出；
    PR-H P1：返回 total/truncated——截断必须可见，且精确同名命中排最前）。"""
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    from opensearch_pipeline.ontology.authz import can_read_object
    acl, bypass = _reader_acl(kb)
    store = _get_store()
    items = [o for o in store.find_objects(object_type, title_like=q, limit=limit)
             if can_read_object(o, acl=acl, bypass_acl=bypass)]
    if q:   # 精确同名优先（原按 canonical_ref 铸序——最匹配的可能沉底）
        items.sort(key=lambda o: (o.get("title") != q,))
    try:
        total = store.count_objects(object_type, title_like=q)
    except Exception:   # noqa: BLE001 — 计数失败不影响主结果
        total = None
    truncated = bool(total is not None and total > len(items))
    return {"items": items, "total": total, "truncated": truncated}


@router.get("/api/ontology/objects/{object_id}")
def ontology_object_detail(object_id: str, request: Request,
                           identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    store = _get_store()
    obj = store.get_object(object_id)
    from opensearch_pipeline.ontology.authz import can_read_object
    acl, bypass = _reader_acl(kb)
    # PR-B（P0-01）：不可读对象与不存在同答 404（identifier 列表随对象门禁）
    if obj is None or not can_read_object(obj, acl=acl, bypass_acl=bypass):
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
    # PR-F（P0-06 HITL）：confirm 与 dismiss 同纪律——理由必填（"确认按钮"不许无凭据落身份）
    if not (req.note or "").strip():
        raise HTTPException(status_code=400, detail="confirm 必须填写确认理由（note）")
    store = _get_store()
    case = store.get_case(case_id)
    # PR-B（P0-01）：scope 外 case 与不存在同答 404（先可见性再状态，防 409 泄露存在性）
    if case is None or not _can_manage_case(kb, store, case):
        raise HTTPException(status_code=404, detail="case 不存在")
    if case["status"] != "open":
        raise HTTPException(status_code=409, detail=f"case 非 open（{case['status']}）")
    _authorize_steward(identity, kb, _steward_dept_for(
        store, namespace=case["namespace"], object_type=case.get("object_type_hint")))
    # PR-B（P0-01）：目标侧三闸——可读 / active / 与 case 期望类型一致（authz 单一实现）
    from opensearch_pipeline.ontology.authz import can_mutate_identity
    acl, bypass = _reader_acl(kb)
    target = store.get_object(req.target_object_id)
    reason = can_mutate_identity(target, acl=acl, bypass_acl=bypass,
                                 expected_object_type=case.get("object_type_hint"))
    if reason:
        _raise_mutation_denied(reason)
    from opensearch_pipeline.ontology.store import DuplicateActiveIdentifier
    # PR-C（P0-05）：铸别名 + case→resolved + 审计 **一个事务**（store 内 FOR UPDATE），
    # 并发被抢在事务内即失败整体回滚——不再有"先 commit 别名再补偿 deactivate"的分叉窗口
    try:
        identifier_id = store.confirm_case_with_identifier(
            case_id, target_object_id=req.target_object_id, by=identity.user_id,
            note=req.note, relation=req.relation, target_revision=req.target_revision,
            audit=_audit_payload(
                action=f"case:{case_id}", decision="confirm", by=identity.user_id,
                detail={"namespace": case["namespace"], "norm_value": case["norm_value"],
                        "target_object_id": req.target_object_id,
                        "target_revision": req.target_revision,
                        "note": (req.note or "")[:200]}))
    except DuplicateActiveIdentifier:
        raise HTTPException(status_code=409,
                            detail="该编号已有正式映射——请先在对象详情里纠错（改指/退役）")
    except ValueError:
        raise HTTPException(status_code=409, detail="case 已被并发处置（本次确认未生效）")
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
    if case is None or not _can_manage_case(kb, store, case):   # PR-B：scope 外同答 404
        raise HTTPException(status_code=404, detail="case 不存在")
    _authorize_steward(identity, kb, _steward_dept_for(
        store, namespace=case["namespace"], object_type=case.get("object_type_hint")))
    try:
        ok = store.dismiss_case(case_id, by=identity.user_id, note=req.note,
                                audit=_audit_payload(
                                    action=f"case:{case_id}", decision="dismiss",
                                    by=identity.user_id,
                                    detail={"namespace": case["namespace"],
                                            "norm_value": case["norm_value"],
                                            "note": req.note[:200]}))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=409, detail=f"case 非 open（{case['status']}）")
    return {"case_id": case_id, "status": "dismissed"}


# ── 写侧：S3 最小纠错 ─────────────────────────────────────────────────────────
class NoteRequest(BaseModel):
    note: Optional[str] = None


def _load_identifier_scope(store, identifier_id: str):
    row = store.get_identifier(identifier_id)
    if row is None:
        raise HTTPException(status_code=404, detail="identifier 不存在")
    obj = store.get_object(row["target_object_id"]) or {}
    return row, obj, _steward_dept_for(store, namespace=row["namespace"],
                                       object_type=obj.get("object_type"))


@router.post("/api/ontology/identifiers/{identifier_id}/deactivate")
def ontology_identifier_deactivate(identifier_id: str, req: NoteRequest, request: Request,
                                   identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    store = _get_store()
    row, _obj, steward = _load_identifier_scope(store, identifier_id)
    _authorize_steward(identity, kb, steward)
    if not store.deactivate_identifier(
            identifier_id, status="rejected",
            audit=_audit_payload(action=f"identifier:{identifier_id}", decision="deactivate",
                                 by=identity.user_id,
                                 detail={"namespace": row["namespace"],
                                         "norm_value": row["norm_value"],
                                         "note": (req.note or "")[:200]})):
        raise HTTPException(status_code=409, detail=f"identifier 非 active（{row['status']}）")
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
    row, old_obj, steward = _load_identifier_scope(store, identifier_id)
    _authorize_steward(identity, kb, steward)
    # PR-B（P0-01）：新目标三闸——可读 / active / 与旧目标同类型（跨类型改指默认拒）
    from opensearch_pipeline.ontology.authz import can_mutate_identity
    acl, bypass = _reader_acl(kb)
    target = store.get_object(req.target_object_id)
    reason = can_mutate_identity(target, acl=acl, bypass_acl=bypass,
                                 expected_object_type=old_obj.get("object_type"))
    if reason:
        _raise_mutation_denied(reason)
    try:
        new_id = store.repoint_identifier(
            identifier_id, req.target_object_id, by=identity.user_id,
            new_target_revision=req.target_revision,
            audit=_audit_payload(action=f"identifier:{identifier_id}", decision="repoint",
                                 by=identity.user_id,
                                 detail={"namespace": row["namespace"],
                                         "norm_value": row["norm_value"],
                                         "old_target": row["target_object_id"],
                                         "new_target": req.target_object_id,
                                         "note": (req.note or "")[:200]}))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"identifier_id": identifier_id, "new_identifier_id": new_id, "status": "superseded"}


@router.post("/api/ontology/objects/{object_id}/retire")
def ontology_object_retire(object_id: str, req: NoteRequest, request: Request,
                           identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    store = _get_store()
    obj = store.get_object(object_id)
    from opensearch_pipeline.ontology.authz import can_read_object
    acl, bypass = _reader_acl(kb)
    # PR-B（P0-01）：不可读对象与不存在同答（先可见性后 steward，防 403 泄露存在性）
    if obj is None or not can_read_object(obj, acl=acl, bypass_acl=bypass):
        raise HTTPException(status_code=404, detail="对象不存在")
    _authorize_steward(identity, kb,
                       _steward_dept_for(store, object_type=obj["object_type"]))
    if not store.retire_object(
            object_id,
            audit=_audit_payload(action=f"object:{object_id}", decision="retire",
                                 by=identity.user_id,
                                 detail={"canonical_ref": obj.get("canonical_ref"),
                                         "note": (req.note or "")[:200]})):
        raise HTTPException(status_code=409, detail=f"对象非 active（{obj['status']}）")
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
    from opensearch_pipeline.ontology.authz import can_mutate_identity, can_read_object
    acl, bypass = _reader_acl(kb)
    if obj is None or not can_read_object(obj, acl=acl, bypass_acl=bypass):
        raise HTTPException(status_code=404, detail="对象不存在")
    _authorize_steward(identity, kb,
                       _steward_dept_for(store, object_type=obj["object_type"]))
    # PR-B（P0-01）：merge 目标三闸——可读 / active / 与 source 同类型（store 同事务再兜一层）
    target = store.get_object(req.merged_into)
    reason = can_mutate_identity(target, acl=acl, bypass_acl=bypass,
                                 expected_object_type=obj["object_type"])
    if reason:
        _raise_mutation_denied(reason)
    try:
        ok = store.mark_duplicate(
            object_id, req.merged_into, by=identity.user_id,
            audit=_audit_payload(action=f"object:{object_id}", decision="mark_duplicate",
                                 by=identity.user_id,
                                 detail={"merged_into": req.merged_into,
                                         "note": (req.note or "")[:200]}))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=409, detail=f"对象非 active（{obj['status']}）")
    return {"object_id": object_id, "status": "merged", "merged_into": req.merged_into}
