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
import re as _re
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


def _steward_scope_for(store, *, namespace: Optional[str] = None,
                       object_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """stewardship 裁决命中的**整行**（含 steward_dept/backup_dept，P1-11 授权要用后者）；
    表故障 → None（scope 未知，fail-closed 到 kb_admin）。"""
    from opensearch_pipeline.ontology.stewardship import resolve_steward
    try:
        rows = store.list_stewardship()
    except Exception:   # noqa: BLE001 — 表故障 → scope 未知 → fail-closed 到 kb_admin
        logger.warning("stewardship 读取失败（授权收敛到 kb_admin）", exc_info=True)
        return None
    return resolve_steward(rows, namespace=namespace, object_type=object_type)


def _steward_dept_for(store, *, namespace: Optional[str] = None,
                      object_type: Optional[str] = None) -> Optional[str]:
    """主 steward 部门（展示用；授权一律走 _authorize_steward 的整行裁决）。"""
    hit = _steward_scope_for(store, namespace=namespace, object_type=object_type)
    return hit["steward_dept"] if hit else None


def _authorize_steward(identity: Identity, kb, scope: Optional[Dict[str, Any]]) -> None:
    """写侧门：kb_admin 恒可；dept_admin 须 scope 命中且 managed 覆盖
    steward_dept **或 backup_dept**（P1-11：backup 代理与主 steward 同权——
    此前只认 steward_dept，seeds 里的 backup_dept 形同虚设）；
    scope 未命中（None）→ 仅 kb_admin（fail-closed）。"""
    from opensearch_pipeline.kb_authz import ROLE_DEPT_ADMIN, ROLE_KB_ADMIN, managed_owner_depts
    from opensearch_pipeline.ontology.stewardship import effective_steward_depts
    if kb.role == ROLE_KB_ADMIN:
        return
    depts = effective_steward_depts(scope)
    if kb.role == ROLE_DEPT_ADMIN and depts \
            and set(depts) & set(managed_owner_depts(kb)):
        return
    steward = (scope or {}).get("steward_dept")
    raise HTTPException(
        status_code=403,
        detail=f"无权处置该条目（steward={steward or '未登记'}，需 kb_admin 或对应 dept_admin）")


# ── 对象级 ACL（PR-B，P0-01）：读侧世界观 = kb_admin bypass / dept_admin 管辖组码 ──
def _reader_acl(kb):
    """(acl, bypass)：对象可见性判定输入（ontology.authz 同一世界观）。"""
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    if kb.role == ROLE_KB_ADMIN:
        return set(), True
    return set(managed_owner_depts(kb)), False


def _can_manage_case(kb, store, case: Dict[str, Any]) -> bool:
    """case 可见性 = 其 steward scope 是否归调用方管辖（evidence 属处置人，读写同域；
    P1-11：backup_dept 与 steward_dept 同权——代理管家也能看到并处置队列）。"""
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    from opensearch_pipeline.ontology.stewardship import effective_steward_depts
    if kb.role == ROLE_KB_ADMIN:
        return True
    scope = _steward_scope_for(store, namespace=case.get("namespace"),
                               object_type=case.get("object_type_hint"))
    depts = effective_steward_depts(scope)
    return bool(depts) and bool(set(depts) & set(managed_owner_depts(kb)))


def _managed_scope_filter(kb, store) -> Optional[Dict[str, List[str]]]:
    """dept_admin 的队列 SQL 粗过滤集（kb_admin → None=不过滤）。

    从 stewardship 表反算调用方管辖的 scope：namespace 全键 / namespace 冒号前缀 /
    object_type。SQL 层先按并集收窄（跨部门行不出库），返回后仍逐条
    `_can_manage_case` 精判（attribute > namespace > object_type 的裁决优先级可能
    在并集内又否掉个别行）——粗筛+精判双层，fail-closed。"""
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    from opensearch_pipeline.ontology.stewardship import effective_steward_depts
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
        # P1-11：backup_dept 与 steward_dept 同权（粗筛与 _can_manage_case 精判同口径）
        if not (set(effective_steward_depts(r)) & managed):
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
                       top_n: int = 3):
    """候选目标对象信息按对象级 ACL 出参（PR-B / P0-A① + C3 复核批次6）。

    C3「先 ACL 后截断」：旧实现 `list_candidates()[:top_n]` 先截断再逐行 ACL——
    top-N 全被遮蔽时可见候选被静默挤出（有可评估候选却给处置人看空列表）。
    现在扫描全部候选，**可见者填满 top-N**（保持置信度序），不可读者不占预算、
    聚合为 hidden_count 出参（拍板记录：弃逐行常量 stub 改聚合计数——计数同样保留
    HITL 告警价值「有 N 个候选被 ACL 遮蔽」，且不可区分性更强：连 candidate_id/
    排序位都不再暴露；跨部门 confidential 候选正是 P0-01 的泄露面）。

    Returns: (visible_rows, hidden_count)。"""
    from opensearch_pipeline.ontology.authz import can_read_object, visible_title
    out: List[Dict[str, Any]] = []
    hidden = 0
    for c in store.list_candidates(case_id):
        obj = store.get_object(c["target_object_id"]) or {}
        if not can_read_object(obj, acl=acl, bypass_acl=bypass):
            hidden += 1
            continue
        if len(out) >= top_n:
            continue            # 可见预算已满：只继续数 hidden，不再出行
        out.append({**c, "target_visible": True,
                    "canonical_ref": obj.get("canonical_ref"),
                    "title": visible_title(obj.get("title"), obj.get("data_classification"),
                                           obj.get("owner_dept"), acl=acl, bypass_acl=bypass),
                    "object_type": obj.get("object_type"), "target_status": obj.get("status"),
                    # PR-F HITL：处置人须看见目标归属/密级再确认（P0-06 UI 验收⑥）
                    "owner_dept": obj.get("owner_dept"),
                    "data_classification": obj.get("data_classification")})
    return out, hidden


def _decode_cursor(raw: Optional[str], order: str) -> Optional[Dict[str, Any]]:
    """PR-I（P2 cursor 分页）：游标=上一页末行排序键 'seen|last_seen|case_id'
    （recent 序省 seen）。畸形游标按无游标处理（重新从头，宁可重看不丢行）。"""
    if not raw:
        return None
    try:
        parts = raw.split("|")
        if order == "freq":
            return {"seen_count": int(parts[0]), "last_seen_at": parts[1],
                    "case_id": parts[2]}
        return {"last_seen_at": parts[0], "case_id": parts[1]}
    except Exception:   # noqa: BLE001
        return None


def _encode_cursor(case: Dict[str, Any], order: str) -> str:
    if order == "freq":
        return f"{case['seen_count']}|{case['last_seen_at']}|{case['case_id']}"
    return f"{case['last_seen_at']}|{case['case_id']}"


@router.get("/api/ontology/workbench")
def ontology_workbench(request: Request, namespace: Optional[str] = None,
                       object_type: Optional[str] = None, order: str = "freq",
                       limit: int = 50, offset: int = 0, cursor: Optional[str] = None,
                       identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    store = _get_store()
    acl, bypass = _reader_acl(kb)
    scope = _managed_scope_filter(kb, store)   # None=kb_admin 不过滤
    raw_cases = store.list_open_cases(namespace=namespace, object_type_hint=object_type,
                                      order=order, limit=limit, offset=offset,
                                      scope_filter=scope,
                                      cursor=_decode_cursor(cursor, order))
    items = []
    for case in raw_cases:
        if not _can_manage_case(kb, store, case):   # 粗筛后的精判（裁决优先级，fail-closed）
            continue
        cands, hidden = _enrich_candidates(store, case["case_id"], acl=acl, bypass=bypass)
        items.append({**case,
                      "candidates": cands,
                      "candidates_hidden": hidden,
                      "steward_dept": _steward_dept_for(
                          store, namespace=case.get("namespace"),
                          object_type=case.get("object_type_hint"))})
    # PR-I：keyset 游标——满页才给 next_cursor（不满页=到底）；游标按**未精判前**的
    # 末行编码（精判滤掉的行也要翻过去，否则会卡在同一页）
    next_cursor = (_encode_cursor(raw_cases[-1], order)
                   if len(raw_cases) >= max(1, min(int(limit), 200)) else None)
    return {"items": items, "limit": limit, "offset": offset, "next_cursor": next_cursor}


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
    cands, hidden = _enrich_candidates(store, case_id, acl=acl, bypass=bypass, top_n=10)
    return {**case,
            "candidates": cands,
            "candidates_hidden": hidden,
            "steward_dept": _steward_dept_for(store, namespace=case.get("namespace"),
                                              object_type=case.get("object_type_hint"))}


@router.get("/api/ontology/objects")
def ontology_objects_search(request: Request, object_type: str, q: Optional[str] = None,
                            limit: int = 20,
                            identity: Optional[Identity] = Depends(current_identity)):
    """确认/改指时的目标对象选择器（PR-B / P0-A②③：对象级 ACL **谓词下推**到 SQL 查询
    与计数——分页在授权集合上执行（不可见行不挤占页位、不饿死可见结果），total/truncated
    只计可见集合（未授权全量计数会泄露不可见对象的数量）；行级 can_read_object 复核为
    纵深防御。PR-H P1：total/truncated 出参——截断必须可见，且精确同名命中排最前）。"""
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    from opensearch_pipeline.ontology.authz import can_read_object
    acl, bypass = _reader_acl(kb)
    store = _get_store()
    rows = store.search_objects_authorized(object_type, acl=acl, bypass_acl=bypass,
                                           title_like=q, limit=limit)
    # 纵深防御：谓词生成与行级判定同源（authz 单模块），此处复核防两者语义漂移
    items = [o for o in rows if can_read_object(o, acl=acl, bypass_acl=bypass)]
    if q:   # 精确同名优先（原按 canonical_ref 铸序——最匹配的可能沉底）
        items.sort(key=lambda o: (o.get("title") != q,))
    try:
        total = store.count_objects_authorized(object_type, acl=acl, bypass_acl=bypass,
                                               title_like=q)
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
# PR-I（P2）：请求字段长度/格式约束——超长 note/revision 此前直穿到 DB 1406/截断。
# ⚠️ 刻意**不用** pydantic Field 约束：FastAPI 的 body 校验先于 handler 跑，畸形 body
# 会在 flag-off 时返回 422 → 泄露隐藏端点的存在性。约束改在 handler 内、
# `_require_enabled_identity` **之后**手动校验（flag-off 恒 404 不破）。
_ULID_RE = _re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_REV_RE = _re.compile(r"^[A-Za-z0-9._-]{1,16}$")
_RELATIONS = ("alias", "variant", "equivalent")


def _check_fields(*, ulid: Optional[Dict[str, str]] = None,
                  revision: Optional[str] = None,
                  relation: Optional[str] = None,
                  notes: Optional[Dict[str, Optional[str]]] = None) -> None:
    """门后字段校验（422）：ulid={字段名: 值}、notes={字段名: 值}。"""
    problems = []
    for name, v in (ulid or {}).items():
        if not _ULID_RE.match(v or ""):
            problems.append(f"{name} 须为 26 位 ULID")
    if revision is not None and not _REV_RE.match(revision):
        problems.append("target_revision 非法（1-16 位字母数字._-）")
    if relation is not None and relation not in _RELATIONS:
        problems.append(f"relation 非法（合法：{_RELATIONS}）")
    for name, v in (notes or {}).items():
        if v is not None and len(v) > 255:
            problems.append(f"{name} 超长（≤255 字符）")
    if problems:
        raise HTTPException(status_code=422, detail="；".join(problems))


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
    _check_fields(ulid={"target_object_id": req.target_object_id},
                  revision=req.target_revision, relation=req.relation,
                  notes={"note": req.note})
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
    _authorize_steward(identity, kb, _steward_scope_for(
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
    _check_fields(notes={"note": req.note})
    case = store.get_case(case_id)
    if case is None or not _can_manage_case(kb, store, case):   # PR-B：scope 外同答 404
        raise HTTPException(status_code=404, detail="case 不存在")
    _authorize_steward(identity, kb, _steward_scope_for(
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


class BatchDismissRequest(BaseModel):
    case_ids: List[str]
    note: str


_BATCH_MAX = 50


@router.post("/api/ontology/cases/batch-dismiss")
def ontology_cases_batch_dismiss(req: BatchDismissRequest, request: Request,
                                 identity: Optional[Identity] = Depends(current_identity)):
    """PR-I（P2「无批量处置」）：批量驳回——大积压最常见的处置。逐条独立结果
    （单条失败不掀翻整批；驳回天然幂等：已处置 → conflict）；每条各自过
    可见性/steward 授权/同事务审计。**刻意不做批量 confirm**：确认=铸身份映射，
    每条目标各不相同，批量确认等于放弃逐条核对——与 HITL 纪律相悖。"""
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    _check_fields(notes={"note": req.note})
    if not (req.note or "").strip():
        raise HTTPException(status_code=400, detail="批量驳回必须填写处置理由（note）")
    ids = list(dict.fromkeys(req.case_ids))          # 去重保序
    if not ids:
        raise HTTPException(status_code=400, detail="case_ids 不能为空")
    if len(ids) > _BATCH_MAX:
        raise HTTPException(status_code=400, detail=f"单批最多 {_BATCH_MAX} 条（分批提交）")
    store = _get_store()
    results: List[Dict[str, Any]] = []
    for case_id in ids:
        case = store.get_case(case_id)
        if case is None or not _can_manage_case(kb, store, case):
            results.append({"case_id": case_id, "status": "not_found"})   # scope 外同不存在
            continue
        try:
            _authorize_steward(identity, kb, _steward_scope_for(
                store, namespace=case["namespace"],
                object_type=case.get("object_type_hint")))
        except HTTPException:
            results.append({"case_id": case_id, "status": "not_found"})
            continue
        try:
            ok = store.dismiss_case(
                case_id, by=identity.user_id, note=req.note,
                audit=_audit_payload(action=f"case:{case_id}", decision="dismiss",
                                     by=identity.user_id,
                                     detail={"namespace": case["namespace"],
                                             "norm_value": case["norm_value"],
                                             "batch": True, "note": req.note[:200]}))
        except ValueError:
            results.append({"case_id": case_id, "status": "error"})
            continue
        except Exception:   # noqa: BLE001 — 审计 fail-closed 等：单条失败不掀批
            logger.warning("批量驳回单条失败：%s", case_id, exc_info=True)
            results.append({"case_id": case_id, "status": "error"})
            continue
        results.append({"case_id": case_id,
                        "status": "dismissed" if ok else "conflict"})
    return {"results": results,
            "dismissed": sum(1 for r in results if r["status"] == "dismissed")}


# ── 写侧：S3 最小纠错 ─────────────────────────────────────────────────────────
class NoteRequest(BaseModel):
    note: Optional[str] = None


def _load_identifier_scope(store, identifier_id: str):
    row = store.get_identifier(identifier_id)
    if row is None:
        raise HTTPException(status_code=404, detail="identifier 不存在")
    obj = store.get_object(row["target_object_id"]) or {}
    return row, obj, _steward_scope_for(store, namespace=row["namespace"],
                                        object_type=obj.get("object_type"))


def _require_old_target_visible(kb, old_obj: Dict[str, Any]) -> None:
    """P0-04：identifier 写动作（deactivate/repoint）对**旧目标对象**的可见性闸。

    steward scope 由 (namespace, object_type) 裁决，与对象 owner_dept 是**正交轴**——
    PMC 管理员可以是某 namespace 的 steward，却对 supply 的 confidential 对象无读权；
    此前旧目标 `_obj` 加载后即丢弃，停用/改指其 identifier 的路径没有闭合。不可见 →
    404 与「identifier 不存在」同答（identifier 指向即泄露对象存在性）。dangling
    identifier（目标行缺失 → obj={}）对非 bypass 同样 404，kb_admin（bypass）可处置。"""
    from opensearch_pipeline.ontology.authz import can_read_object
    acl, bypass = _reader_acl(kb)
    # dangling（obj={}）传空 dict 而非 None：非 bypass 缺列 fail-closed 拒，
    # bypass（kb_admin）放行——悬空身份行必须有人能收尾
    if not can_read_object(old_obj if old_obj is not None else {},
                           acl=acl, bypass_acl=bypass):
        raise HTTPException(status_code=404, detail="identifier 不存在")


@router.post("/api/ontology/identifiers/{identifier_id}/deactivate")
def ontology_identifier_deactivate(identifier_id: str, req: NoteRequest, request: Request,
                                   identity: Optional[Identity] = Depends(current_identity)):
    identity = _require_enabled_identity(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    kb = _require_reader(identity)
    _check_fields(notes={"note": req.note})
    store = _get_store()
    row, old_obj, steward = _load_identifier_scope(store, identifier_id)
    # P0-04：先旧目标可见性（404 防存在性泄露），再 steward 授权（403）
    _require_old_target_visible(kb, old_obj)
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
    _check_fields(ulid={"target_object_id": req.target_object_id},
                  revision=req.target_revision, notes={"note": req.note})
    store = _get_store()
    row, old_obj, steward = _load_identifier_scope(store, identifier_id)
    # P0-04：旧目标可见性闸（steward scope 与 owner_dept 正交——scope 命中 ≠ 旧目标可读）
    _require_old_target_visible(kb, old_obj)
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
    _check_fields(notes={"note": req.note})
    store = _get_store()
    obj = store.get_object(object_id)
    from opensearch_pipeline.ontology.authz import can_read_object
    acl, bypass = _reader_acl(kb)
    # PR-B（P0-01）：不可读对象与不存在同答（先可见性后 steward，防 403 泄露存在性）
    if obj is None or not can_read_object(obj, acl=acl, bypass_acl=bypass):
        raise HTTPException(status_code=404, detail="对象不存在")
    _authorize_steward(identity, kb,
                       _steward_scope_for(store, object_type=obj["object_type"]))
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
    _check_fields(ulid={"merged_into": req.merged_into}, notes={"note": req.note})
    store = _get_store()
    obj = store.get_object(object_id)
    from opensearch_pipeline.ontology.authz import can_mutate_identity, can_read_object
    acl, bypass = _reader_acl(kb)
    if obj is None or not can_read_object(obj, acl=acl, bypass_acl=bypass):
        raise HTTPException(status_code=404, detail="对象不存在")
    _authorize_steward(identity, kb,
                       _steward_scope_for(store, object_type=obj["object_type"]))
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
