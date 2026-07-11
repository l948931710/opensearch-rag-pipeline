# -*- coding: utf-8 -*-
"""对象级 ACL「不可读 == 结构等价于不存在」矩阵（2026-07-10 外审重评 P0-A/P0-B/P1-11）。

角色 {employee, 无关 dept_admin(finance), steward dept_admin(pmc),
backup steward dept_admin(rd), kb_admin} × 密级 {public, internal, confidential}，
钉住四个泄露面：

1. 工作台候选 enrich（P0-A①）：不可读候选只返回**白名单重建**的常量占位——
   target_object_id/features_json/method/confidence 任一出参都是跨 ACL 存在性泄露
   （旧 `{**c}` 展开后删四个字段恰漏了它们）。
2. 对象搜索（P0-A②③）：ACL 谓词下推到查询与计数——items/total/truncated 只算可见
   集合；分页在授权集合上执行，排前面的隐藏行不得饿死可见结果。
3. agent ontology_resolve receipt（P0-A）：resolved-不可读 与 candidate-全不可读
   都与「无法解析」逐字段同答（status/confidence 不泄露"高分隐藏对象存在"）。
4. agent ontology_identity_resolve 写（P0-B）：propose 与落库都以服务端注入的
   ctx.acl_groups 过 authz.can_mutate_identity（与工作台同一实现）；不可见与不存在
   同答；审批人 scope / expected_version 校验保留但只是**额外**条件。

P1-11：stewardship 行的 backup_dept 与主 steward 同权（工作台授权 + 队列可见性 +
approver_scope CSV "steward,backup"）。
"""
import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import opensearch_pipeline.agent_runtime.approval_store as approval_store
import opensearch_pipeline.routes.ontology as onto_route
from opensearch_pipeline import api
from opensearch_pipeline.agent_runtime.approval_store import _resolve_scope, resolve_scope_live
from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.api import Identity, current_identity
from opensearch_pipeline.ontology import stewardship
from opensearch_pipeline.ontology.resolve import OntologyResolver
from opensearch_pipeline.ontology.store import MemoryOntologyStore

_ROLES = {}          # user_id → kb role（resolve_kb_identity 假件数据面）
_MANAGED = {}        # user_id → managed_owner_depts


@pytest.fixture(autouse=True)
def _fresh_resolver_registry(monkeypatch):
    """approver_scope seam 全局注册表逐测隔离（identity 工具 __init__ 即注册）。"""
    monkeypatch.setattr(approval_store, "_SCOPE_RESOLVERS", {})


@pytest.fixture()
def store(monkeypatch):
    mem = MemoryOntologyStore()
    stewardship.ensure_seeds(mem)   # product/sku/mold→pmc(product backup=rd) · material→supply
    monkeypatch.setattr(onto_route, "_STORE", None)
    monkeypatch.setattr(onto_route, "_get_store", lambda: mem)
    monkeypatch.setenv("RAG_ONTOLOGY_ENABLE", "true")
    monkeypatch.setattr("opensearch_pipeline.dingtalk_identity.resolve_kb_identity",
                        lambda uid: SimpleNamespace(role=_ROLES.get(uid, "employee"),
                                                    user_id=uid))
    monkeypatch.setattr("opensearch_pipeline.kb_authz.managed_owner_depts",
                        lambda kb: _MANAGED.get(kb.user_id, []))
    _ROLES.clear()
    _MANAGED.clear()
    yield mem


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    api.app.dependency_overrides.clear()


def _client(identity):
    api.app.dependency_overrides[current_identity] = lambda: identity
    return TestClient(api.app)


def _identity(user_id, role, managed=()):
    _ROLES[user_id] = role
    _MANAGED[user_id] = list(managed)
    return Identity(user_id=user_id, acl_groups=list(managed) or ["public"],
                    role="employee")


def _ctx(acl, user="u1", roles=("employee",)):
    return ExecutionContext.create(request_id="", user_id=user, acl_groups=list(acl),
                                   roles=roles, channel="console", thread_id="t1")


def _hash_embedder(text):
    h = hashlib.sha256(text.strip().encode("utf-8")).digest()
    return [b / 255.0 * 2 - 1 for b in h]


# 五个矩阵角色（工作台侧）；agent 工具侧无角色概念，等价维度是 acl 覆盖
def _viewers():
    return {
        "employee": _identity("emp1", "employee"),
        "other_admin": _identity("fin1", "dept_admin", managed=("finance",)),
        "steward_admin": _identity("pmc1", "dept_admin", managed=("pmc",)),
        "backup_admin": _identity("rd1", "dept_admin", managed=("rd",)),
        "kb_admin": _identity("root", "kb_admin"),
    }


# ══ 1. 工作台候选 enrich（P0-A①）═══════════════════════════════════════════════

_STUB_KEYS = {"candidate_id", "case_id", "target_visible", "target_object_id",
              "target_revision", "method", "confidence", "features_json",
              "canonical_ref", "title", "object_type", "target_status",
              "owner_dept", "data_classification"}


def _seed_mixed_case(store):
    """product-hint case（steward=pmc / backup=rd）+ 三个 supply 归属候选目标
    （public/internal/confidential）。返回 (case_id, {cls: obj})。"""
    objs = {}
    for cls in ("public", "internal", "confidential"):
        objs[cls] = store.mint_object("material", f"supply料-{cls}", owner_dept="supply",
                                      data_classification=cls, _caller="test")
    case_id = store.upsert_case("u8", "mx1", "MX1", object_type_hint="product",
                                evidence={"source": "matrix"})
    conf = {"public": 0.9, "internal": 0.8, "confidential": 0.7}
    for cls, obj in objs.items():
        store.add_candidate(case_id, obj["object_id"], method="embedding",
                            confidence=conf[cls], features={"cls": cls})
    return case_id, objs


@pytest.mark.parametrize("viewer_key", ["steward_admin", "backup_admin"])
def test_candidate_enrich_restricted_is_constant_stub(store, viewer_key):
    """steward / backup steward（P1-11 可见队列）都拿不到 supply 的 internal/confidential
    候选字段：占位行 = 白名单常量形态，且 internal 与 confidential 的占位**互相不可区分**。"""
    case_id, objs = _seed_mixed_case(store)
    viewer = _viewers()[viewer_key]
    detail = _client(viewer).get(f"/api/ontology/cases/{case_id}").json()
    by_visible = {}
    for cand in detail["candidates"]:
        by_visible.setdefault(cand["target_visible"], []).append(cand)
    # public 候选可见且字段完整
    vis = by_visible[True]
    assert len(vis) == 1 and vis[0]["target_object_id"] == objs["public"]["object_id"]
    assert vis[0]["method"] == "embedding" and vis[0]["confidence"] == 0.9
    # internal/confidential → 常量占位：键集固定、除定位键外全 None
    hidden = by_visible[False]
    assert len(hidden) == 2
    for cand in hidden:
        assert set(cand.keys()) == _STUB_KEYS
        for k in _STUB_KEYS - {"candidate_id", "case_id", "target_visible"}:
            assert cand[k] is None, f"占位行泄露字段 {k}={cand[k]!r}"
        assert cand["case_id"] == case_id
    # 两条占位除 candidate_id 外逐位一致（internal vs confidential 不可区分）
    a, b = (dict(c) for c in hidden)
    a.pop("candidate_id"), b.pop("candidate_id")
    assert a == b


def test_candidate_enrich_matrix_other_roles(store):
    """employee 无入口(403)；无关 dept_admin 连 case 都 404（与不存在同答）；
    kb_admin 全候选可见带完整字段。"""
    case_id, objs = _seed_mixed_case(store)
    v = _viewers()
    assert _client(v["employee"]).get(f"/api/ontology/cases/{case_id}").status_code == 403
    assert _client(v["other_admin"]).get(f"/api/ontology/cases/{case_id}").status_code == 404
    root = _client(v["kb_admin"]).get(f"/api/ontology/cases/{case_id}").json()
    assert all(c["target_visible"] is True for c in root["candidates"])
    assert {c["target_object_id"] for c in root["candidates"]} \
        == {o["object_id"] for o in objs.values()}


# ══ 2. 对象搜索（P0-A②③）═══════════════════════════════════════════════════════


def _seed_search_pool(store):
    """3 个 supply-internal 先铸（canonical_ref 排最前，旧实现会占满页位）+ 2 个 public。"""
    hidden, public = [], []
    for i in range(3):
        hidden.append(store.mint_object("material", f"隐藏牌号{i}", owner_dept="supply", _caller="test"))
    for i in range(2):
        public.append(store.mint_object("material", f"公开牌号{i}", owner_dept="supply",
                                        data_classification="public", _caller="test"))
    return hidden, public


def test_search_matrix_items_total_truncated(store):
    _seed_search_pool(store)
    v = _viewers()
    # employee：读侧无入口
    assert _client(v["employee"]).get(
        "/api/ontology/objects?object_type=material").status_code == 403
    # 无关 / steward / backup dept_admin（都不拥有 supply）：只见 2 条 public，
    # total/truncated 只算可见集合——旧实现 total=5, truncated=True 直接泄露隐藏对象数量
    for key in ("other_admin", "steward_admin", "backup_admin"):
        r = _client(v[key]).get("/api/ontology/objects?object_type=material&limit=10").json()
        assert [o["data_classification"] for o in r["items"]] == ["public", "public"], key
        assert r["total"] == 2 and r["truncated"] is False, key
    # kb_admin：全量
    r = _client(v["kb_admin"]).get("/api/ontology/objects?object_type=material&limit=10").json()
    assert r["total"] == 5 and len(r["items"]) == 5 and r["truncated"] is False


def test_search_pagination_on_authorized_set_no_starvation(store):
    """P0-A③：limit=2 时授权集合先行——旧"先 limit 再 filter"会取到 2 条隐藏行、
    过滤成空列表（可见结果被饿死）且 truncated=True（泄露）。"""
    _seed_search_pool(store)
    r = _client(_viewers()["steward_admin"]).get(
        "/api/ontology/objects?object_type=material&limit=2").json()
    assert len(r["items"]) == 2
    assert all(o["data_classification"] == "public" for o in r["items"])
    assert r["total"] == 2 and r["truncated"] is False
    # kb_admin 同 limit：真截断如实上报
    r2 = _client(_viewers()["kb_admin"]).get(
        "/api/ontology/objects?object_type=material&limit=2").json()
    assert len(r2["items"]) == 2 and r2["total"] == 5 and r2["truncated"] is True


def test_search_all_hidden_equals_nonexistent(store):
    """只命中不可见对象的搜索响应 == 什么都不命中的搜索响应（逐位一致）。"""
    _seed_search_pool(store)
    c = _client(_viewers()["other_admin"])
    all_hidden = c.get("/api/ontology/objects?object_type=material&q=隐藏牌号").json()
    no_such = c.get("/api/ontology/objects?object_type=material&q=根本不存在").json()
    assert all_hidden == no_such == {"items": [], "total": 0, "truncated": False}


# ══ 3. agent ontology_resolve receipt（P0-A）════════════════════════════════════


def _resolve_tool(store, embedder=None):
    from opensearch_pipeline.agent_tools.ontology_resolve import OntologyResolveTool
    return OntologyResolveTool(resolver=OntologyResolver(store, embedder=embedder))


@pytest.mark.parametrize("cls,acl,readable", [
    ("public", ("pmc",), True),
    ("internal", ("pmc",), False),
    ("internal", ("supply",), True),
    ("confidential", ("pmc",), False),
    ("confidential", ("supply",), True),
])
def test_resolve_exact_hit_matrix(store, cls, acl, readable):
    """resolved-不可读 → 与「查不存在的编号」**逐字段同答**（仅 norm/raw 回显不同）。"""
    obj = store.mint_object("material", f"牌号-{cls}", owner_dept="supply",
                            data_classification=cls, _caller="test")
    store.insert_identifier("u8", "sec1", "SEC1", obj["object_id"], method="seed", _caller="test")
    tool = _resolve_tool(store)
    got = tool.run(_ctx(acl), {"namespace": "u8", "value": "sec1"})
    if readable:
        assert got.receipt["status"] == "resolved"
        assert got.receipt["object_id"] == obj["object_id"]
        return
    ghost = tool.run(_ctx(acl), {"namespace": "u8", "value": "nope9"})
    assert ghost.receipt["status"] == "unresolved"
    assert got.receipt == {**ghost.receipt, "norm_value": "SEC1"}
    assert got.content[0].text == ghost.content[0].text.replace("'nope9'", "'sec1'")
    assert obj["canonical_ref"] not in got.content[0].text
    assert obj["object_id"] not in str(got.receipt)


def test_resolve_candidate_all_hidden_equals_nonexistent(store):
    """candidate 分支漏网（复核 §3）：唯一候选不可读 → 不得保留 status=candidate 与
    池顶 confidence（那等于宣告「系统发现了某个高分隐藏对象」）——与无候选查询同答。"""
    store.mint_object("material", "内部牌号目录", owner_dept="supply",
                      data_classification="internal", _caller="test")
    tool = _resolve_tool(store, embedder=_hash_embedder)
    denied = tool.run(_ctx(("pmc",)), {"namespace": "lab_sample", "value": "内部牌号目录"})
    ghost = tool.run(_ctx(("pmc",)), {"namespace": "lab_sample", "value": "完全无关词xyz"})
    assert ghost.receipt["status"] == "unresolved" and ghost.receipt["candidates"] == []
    assert denied.receipt == {**ghost.receipt,
                              "norm_value": denied.receipt["norm_value"]}
    assert denied.receipt["confidence"] == 0.0 and denied.receipt["status"] == "unresolved"
    # 有权调用方仍是 candidate（行为只对无权者降级）
    allowed = tool.run(_ctx(("supply",)), {"namespace": "lab_sample", "value": "内部牌号目录"})
    assert allowed.receipt["status"] == "candidate"
    assert allowed.receipt["candidates"][0]["title"] == "内部牌号目录"


def test_resolve_visible_candidate_not_starved_and_top_conf_not_leaked(store):
    """过滤先于截断：排前面的隐藏候选不挤占可见位；receipt.confidence 只报可见最高分。"""
    from opensearch_pipeline.agent_tools.ontology_resolve import _format_result
    from opensearch_pipeline.ontology.resolve import Candidate, ResolveResult
    cands = [Candidate(target_object_id=f"hid{i}", method="embedding",
                       confidence=0.94 - i * 0.01, canonical_ref=f"FLP-M-90000{i}",
                       title=f"隐藏{i}", object_type="material", owner_dept="supply",
                       data_classification="internal") for i in range(4)]
    cands.append(Candidate(target_object_id="vis1", method="embedding", confidence=0.8,
                           canonical_ref="FLP-M-900009", title="可见牌号",
                           object_type="material", owner_dept="pmc",
                           data_classification="internal"))
    res = ResolveResult(status="candidate", namespace="lab_sample", raw_value="可见牌号",
                        norm_value="可见牌号", intent="read", confidence=0.94,
                        requires_hitl=True, candidates=cands)
    content, receipt = _format_result(res, {"pmc"})
    assert [c["object_id"] for c in receipt["candidates"]] == ["vis1"]
    assert receipt["confidence"] == 0.8          # 池顶 0.94 是隐藏对象的分，不得出参
    text = content[0].text
    assert "可见牌号" in text
    for i in range(4):
        assert f"隐藏{i}" not in text and f"hid{i}" not in str(receipt)
        assert f"FLP-M-90000{i}" not in text


# ══ 4. agent ontology_identity_resolve 写（P0-B）════════════════════════════════


def _identity_tool(store):
    from opensearch_pipeline.agent_tools.ontology_identity_resolve import (
        OntologyIdentityResolveTool,
    )
    return OntologyIdentityResolveTool(store=store)


@pytest.mark.parametrize("cls,acl,expect_ok", [
    ("public", ("pmc",), True),          # public 目标任何部门可确认
    ("internal", ("supply",), True),     # 归属部门可确认
    ("internal", ("pmc",), False),       # 跨部门 internal → 拒
    ("confidential", ("pmc",), False),   # 外审动态探针场景
    ("confidential", ("supply",), True),
])
def test_identity_execute_requester_acl_matrix(store, cls, acl, expect_ok):
    """P0-B①②：落库前以 ctx.acl_groups 过 can_mutate_identity；不可见目标的失败
    ToolResult 与不存在目标**整体结构一致**，且零副作用。"""
    obj = store.mint_object("material", f"牌号-{cls}", owner_dept="supply",
                            data_classification=cls, _caller="test")
    tool = _identity_tool(store)
    args = {"namespace": "material_grade", "value": f"mg-{cls}",
            "target_object_id": obj["object_id"]}
    res = tool.run(_ctx(acl, user="req1"), args)
    if expect_ok:
        assert res.status == "succeeded"
        assert store.get_active_identifier("material_grade",
                                           f"MG-{cls.upper()}") is not None
        return
    ghost = tool.run(_ctx(acl, user="req1"),
                     {"namespace": "material_grade", "value": f"mg-{cls}",
                      "target_object_id": "0" * 26})
    assert res.status == "failed"
    assert res.model_dump() == ghost.model_dump()        # 不可见 == 不存在（整体结构）
    assert obj["canonical_ref"] not in (res.error or "")
    assert store.get_active_identifier("material_grade", f"MG-{cls.upper()}") is None


def test_identity_execute_no_role_bypass(store):
    """agent 通道无 kb_admin bypass（fail-closed）：ctx.roles 带 kb_admin 但 acl 不覆盖
    → 仍拒（特权操作走工作台，工作台才有 _reader_acl 的 bypass 语义）。"""
    obj = store.mint_object("material", "机密料", owner_dept="supply",
                            data_classification="confidential", _caller="test")
    tool = _identity_tool(store)
    res = tool.run(_ctx(("finance",), user="admin9", roles=("kb_admin",)),
                   {"namespace": "material_grade", "value": "mg-x",
                    "target_object_id": obj["object_id"]})
    assert res.status == "failed"
    assert store.get_active_identifier("material_grade", "MG-X") is None


def test_identity_execute_version_and_scope_checks_still_additional(store):
    """P0-B③：requester ACL 过闸后，expected_version 钉与 approval_scope 漂移重验
    仍然生效（额外条件，未被 ACL 闸取代）。"""
    from dataclasses import replace
    obj = store.mint_object("material", "PP-1100", owner_dept="supply", _caller="test")
    tool = _identity_tool(store)
    # version 漂移
    store.update_golden(obj["object_id"], {"改": "了"}, expected_version=1, allow_missing_provenance=True)
    res = tool.run(_ctx(("supply",)), {"namespace": "material_grade", "value": "pp-1100",
                                       "target_object_id": obj["object_id"],
                                       "expected_version": 1})
    assert res.status == "failed" and "重新发起提案" in (res.error or "")
    # scope 漂移（material steward=supply 无 backup → live CSV="supply"）
    ctx = replace(_ctx(("supply",)), approval_request_id="req_" + "z" * 28,
                  approved_by="steward_9", approval_scope="pmc,rd")
    res2 = tool.run(ctx, {"namespace": "material_grade", "value": "pp-1100",
                          "target_object_id": obj["object_id"]})
    assert res2.status == "failed" and "stewardship 已变更" in (res2.error or "")
    assert store.get_active_identifier("material_grade", "PP-1100") is None


def test_identity_propose_scope_matrix(store):
    """P0-B propose 侧 + P1-11 CSV：requester 可读 → steward CSV（product 带 backup
    = "pmc,rd"；material 无 backup = "supply"）；requester 不可读 → ''（仅 kb_admin
    可审，域 steward 队列不出现无权提案）；审批时现算（ctx=None）→ 纯 steward CSV。"""
    _identity_tool(store)                                # 构造即注册 seam
    prod = store.mint_object("product", "龙虾杯", owner_dept="pmc", _caller="test")
    mat = store.mint_object("material", "PP料", owner_dept="supply", _caller="test")
    # 可读 requester：CSV 路由（发起人主属部门 marketing 不参与路由）
    assert _resolve_scope("ontology_identity_resolve", _ctx(("marketing", "pmc")),
                          {"namespace": "u8", "target_object_id": prod["object_id"]}) \
        == "pmc,rd"
    assert _resolve_scope("ontology_identity_resolve", _ctx(("supply",)),
                          {"namespace": "material_grade",
                           "target_object_id": mat["object_id"]}) == "supply"
    # 不可读 requester：'' fail-closed（对 internal 与 confidential 一致，不可区分）
    conf = store.mint_object("material", "机密料", owner_dept="supply",
                             data_classification="confidential", _caller="test")
    for target in (mat, conf):
        assert _resolve_scope("ontology_identity_resolve", _ctx(("finance",)),
                              {"namespace": "material_grade",
                               "target_object_id": target["object_id"]}) == ""
    # 目标不存在：与不可读同样收敛 ''（存在性不经 scope 侧信道泄露）
    assert _resolve_scope("ontology_identity_resolve", _ctx(("finance",)),
                          {"namespace": "material_grade",
                           "target_object_id": "0" * 26}) == ""
    # 审批时现算（resolve_scope_live 约定 ctx=None）：跳过 requester 闸 → steward CSV
    assert resolve_scope_live("ontology_identity_resolve",
                              {"namespace": "u8",
                               "target_object_id": prod["object_id"]}) == "pmc,rd"


# ══ 5. P1-11：backup steward 生效（工作台闭环）══════════════════════════════════


def test_backup_steward_sees_queue_and_dismisses(store):
    """rd 是 product scope 的 backup_dept：队列可见（粗筛+精判都认 backup）、可驳回；
    无关 finance dept_admin 恒 404/空。修复前 backup_dept 存了但授权从不认。"""
    case_id = store.upsert_case("u8", "bk1", "BK1", object_type_hint="product")
    backup = _client(_viewers()["backup_admin"])
    items = backup.get("/api/ontology/workbench").json()["items"]
    assert [c["case_id"] for c in items] == [case_id]
    assert items[0]["steward_dept"] == "pmc"             # 展示仍是主 steward
    other = _client(_viewers()["other_admin"])
    assert other.get("/api/ontology/workbench").json()["items"] == []
    assert other.post(f"/api/ontology/cases/{case_id}/dismiss",
                      json={"note": "无关部门"}).status_code == 404
    backup = _client(_viewers()["backup_admin"])     # override 是 app 级——重建再用
    assert backup.post(f"/api/ontology/cases/{case_id}/dismiss",
                       json={"note": "backup 代理处置"}).status_code == 200
    assert store.get_case(case_id)["status"] == "dismissed"


def test_backup_steward_confirm_respects_object_acl(store):
    """backup 授权 ≠ 对象读权（三分授权）：rd 可确认到自己可读的 product 目标；
    指到 pmc-internal 目标仍 404（对象级 ACL 不因 stewardship 放宽）。"""
    rd_obj = store.mint_object("product", "研发杯", owner_dept="rd", _caller="test")
    pmc_obj = store.mint_object("product", "PMC杯", owner_dept="pmc", _caller="test")
    c1 = store.upsert_case("u8", "bk2", "BK2", object_type_hint="product")
    c2 = store.upsert_case("u8", "bk3", "BK3", object_type_hint="product")
    backup = _client(_viewers()["backup_admin"])
    assert backup.post(f"/api/ontology/cases/{c1}/confirm",
                       json={"target_object_id": rd_obj["object_id"],
                             "note": "backup 确认"}).status_code == 200
    assert store.get_active_identifier("u8", "BK2")["target_object_id"] \
        == rd_obj["object_id"]
    assert backup.post(f"/api/ontology/cases/{c2}/confirm",
                       json={"target_object_id": pmc_obj["object_id"],
                             "note": "跨读权确认"}).status_code == 404
    assert store.get_active_identifier("u8", "BK3") is None


def test_effective_steward_depts_helper():
    from opensearch_pipeline.ontology.stewardship import effective_steward_depts
    assert effective_steward_depts(None) == []
    assert effective_steward_depts({"steward_dept": "pmc"}) == ["pmc"]
    assert effective_steward_depts({"steward_dept": "pmc", "backup_dept": None}) == ["pmc"]
    assert effective_steward_depts({"steward_dept": "pmc", "backup_dept": "rd"}) \
        == ["pmc", "rd"]
    assert effective_steward_depts({"steward_dept": "pmc", "backup_dept": "pmc"}) == ["pmc"]
