# -*- coding: utf-8 -*-
"""routes/ontology.py —— steward 消解工作台（PR6）。

TestClient + dependency_overrides(current_identity) + monkeypatch(_get_store/_audit/
resolve_kb_identity/managed_owner_depts)：flag 门禁、读写授权（kb_admin/dept_admin scope/
fail-closed）、case 四处置、S3 纠错、并发补偿、审计留痕。全程 MemoryOntologyStore 零 DB。
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import opensearch_pipeline.routes.ontology as onto_route
from opensearch_pipeline import api
from opensearch_pipeline.api import Identity, current_identity
from opensearch_pipeline.ontology import stewardship
from opensearch_pipeline.ontology.store import MemoryOntologyStore

_ROLES = {}          # user_id → kb role（resolve_kb_identity 假件的数据面）
_MANAGED = {}        # user_id → managed_owner_depts


@pytest.fixture()
def store(monkeypatch):
    mem = MemoryOntologyStore()
    stewardship.ensure_seeds(mem)          # product/sku/mold→pmc · material→supply · customer→marketing
    monkeypatch.setattr(onto_route, "_STORE", None)
    monkeypatch.setattr(onto_route, "_get_store", lambda: mem)
    monkeypatch.setenv("RAG_ONTOLOGY_ENABLE", "true")
    # PR-C：审计随 store 写方法同事务落库（Memory 收进 audit_rows）——不再 monkeypatch 路由层
    mem.audits = mem.audit_rows
    monkeypatch.setattr("opensearch_pipeline.dingtalk_identity.resolve_kb_identity",
                        lambda uid: SimpleNamespace(role=_ROLES.get(uid, "employee"),
                                                    user_id=uid))
    monkeypatch.setattr("opensearch_pipeline.kb_authz.managed_owner_depts",
                        lambda kb: _MANAGED.get(kb.user_id, []))
    _ROLES.clear()
    _MANAGED.clear()
    yield mem


def _client(identity):
    api.app.dependency_overrides[current_identity] = lambda: identity
    return TestClient(api.app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    api.app.dependency_overrides.clear()


def _identity(user_id="admin1", role="kb_admin", managed=(), acl=("pmc",)):
    _ROLES[user_id] = role
    _MANAGED[user_id] = list(managed)
    return Identity(user_id=user_id, acl_groups=list(acl), role="employee")


def _seed_case(store, ns="u8", raw="abc123-m", hint="product", with_candidate=True):
    obj = store.mint_object("product", "6.2口径龙虾杯", owner_dept="pmc")
    case_id = store.upsert_case(ns, raw, raw.upper(), object_type_hint=hint,
                                evidence={"source": "test"})
    if with_candidate:
        store.add_candidate(case_id, obj["object_id"], method="rule", confidence=0.85)
    return obj, case_id


# ── flag / 认证 / 读侧授权 ─────────────────────────────────────────────────────


def test_flag_off_hides_all_endpoints(store, monkeypatch):
    monkeypatch.delenv("RAG_ONTOLOGY_ENABLE", raising=False)
    c = _client(_identity())
    assert c.get("/api/ontology/workbench").status_code == 404
    assert c.post("/api/ontology/cases/x/confirm",
                  json={"target_object_id": "o"}).status_code == 404
    assert c.get("/api/ontology/coverage").status_code == 404


def test_unauthenticated_401(store):
    assert _client(None).get("/api/ontology/workbench").status_code == 401


def test_employee_403_on_read(store):
    c = _client(_identity(role="employee"))
    assert c.get("/api/ontology/workbench").status_code == 403
    assert c.get("/api/ontology/coverage").status_code == 403


@pytest.mark.parametrize("role,managed", [("kb_admin", ()), ("dept_admin", ("pmc",))])
def test_admins_can_read_queue(store, role, managed):
    _seed_case(store)
    r = _client(_identity(role=role, managed=managed)).get("/api/ontology/workbench")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["steward_dept"] == "pmc"
    assert items[0]["candidates"][0]["title"] == "6.2口径龙虾杯"
    assert items[0]["candidates"][0]["method"] == "rule"


# ── 队列 / 覆盖率 / 详情 / 搜索 ────────────────────────────────────────────────


def test_workbench_filters_and_pagination(store):
    _seed_case(store, ns="u8", raw="a1")
    _seed_case(store, ns="customer:KFC", raw="b2", hint="sku")
    c = _client(_identity())
    assert len(c.get("/api/ontology/workbench?namespace=u8").json()["items"]) == 1
    assert len(c.get("/api/ontology/workbench?object_type=sku").json()["items"]) == 1
    assert len(c.get("/api/ontology/workbench?limit=1&offset=1").json()["items"]) == 1


def test_workbench_freq_order(store):
    _, c1 = _seed_case(store, raw="rare1")
    _, c2 = _seed_case(store, raw="hot2")
    store.upsert_case("u8", "hot2", "HOT2")            # 第二次观测 → seen_count=2
    items = _client(_identity()).get("/api/ontology/workbench?order=freq").json()["items"]
    assert items[0]["case_id"] == c2 and items[0]["seen_count"] == 2


def test_coverage_with_manual_review_rate(store):
    obj, _ = _seed_case(store)
    store.insert_identifier("u8", "x1", "X1", obj["object_id"], method="seed",
                            confirmed_by="auto")
    store.insert_identifier("u8", "x2", "X2", obj["object_id"], method="manual",
                            confirmed_by="s1")
    cov = _client(_identity()).get("/api/ontology/coverage").json()
    assert cov["active_identifiers"] == 2 and cov["auto_active"] == 1
    assert cov["manual_review_rate"] == pytest.approx(0.5)


def test_case_detail_and_404(store):
    _, case_id = _seed_case(store)
    c = _client(_identity())
    d = c.get(f"/api/ontology/cases/{case_id}").json()
    assert d["case_id"] == case_id and d["evidence_json"] is not None
    assert d["steward_dept"] == "pmc" and len(d["candidates"]) == 1
    assert c.get("/api/ontology/cases/nope").status_code == 404


def test_objects_search_and_detail(store):
    obj, _ = _seed_case(store)
    c = _client(_identity())
    hits = c.get("/api/ontology/objects?object_type=product&q=龙虾").json()["items"]
    assert len(hits) == 1 and hits[0]["object_id"] == obj["object_id"]
    d = c.get(f"/api/ontology/objects/{obj['object_id']}").json()
    assert d["canonical_ref"] == obj["canonical_ref"] and "identifiers" in d
    assert c.get("/api/ontology/objects/nope").status_code == 404


# ── confirm ───────────────────────────────────────────────────────────────────


def test_confirm_happy_path_with_audit(store):
    obj, case_id = _seed_case(store)
    r = _client(_identity(role="dept_admin", managed=("pmc",))).post(
        f"/api/ontology/cases/{case_id}/confirm",
        json={"target_object_id": obj["object_id"], "target_revision": "r2", "note": "打样一致"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "resolved"
    active = store.get_active_identifier("u8", "ABC123-M")
    assert active["target_object_id"] == obj["object_id"]
    assert active["target_revision"] == "r2"
    assert active["confirmed_by"] == "admin1" and active["source_case_id"] == case_id
    assert store.get_case(case_id)["status"] == "resolved"
    assert store.audits[-1]["decision"] == "confirm"
    assert store.audits[-1]["detail"]["identifier_id"] == body["identifier_id"]


def test_confirm_scope_authz(store):
    obj, case_id = _seed_case(store)      # steward=pmc
    no_scope = _client(_identity(user_id="d2", role="dept_admin", managed=("finance",)))
    r = no_scope.post(f"/api/ontology/cases/{case_id}/confirm",
                      json={"target_object_id": obj["object_id"]})
    assert r.status_code == 404           # PR-B：scope 外与不存在同答（防存在性泄露）
    kb = _client(_identity(user_id="root", role="kb_admin"))
    assert kb.post(f"/api/ontology/cases/{case_id}/confirm",
                   json={"target_object_id": obj["object_id"]}).status_code == 200


def test_confirm_unscoped_namespace_fail_closed(store):
    """scope 未登记（lot_code 无种子）→ dept_admin 404（PR-B：scope 外同不存在）、kb_admin 放行。"""
    obj = store.mint_object("product", "托盘", owner_dept="pmc")
    case_id = store.upsert_case("lot_code", "lt1", "LT1", object_type_hint=None)
    dept = _client(_identity(user_id="d3", role="dept_admin", managed=("pmc",)))
    assert dept.post(f"/api/ontology/cases/{case_id}/confirm",
                     json={"target_object_id": obj["object_id"]}).status_code == 404
    kb = _client(_identity(user_id="root", role="kb_admin"))
    assert kb.post(f"/api/ontology/cases/{case_id}/confirm",
                   json={"target_object_id": obj["object_id"]}).status_code == 200


def test_confirm_conflicts(store):
    obj, case_id = _seed_case(store)
    c = _client(_identity())
    # 目标不存在 / 目标非 active
    assert c.post(f"/api/ontology/cases/{case_id}/confirm",
                  json={"target_object_id": "nope"}).status_code == 404
    retired = store.mint_object("product", "退役品", owner_dept="pmc")
    store.retire_object(retired["object_id"])
    assert c.post(f"/api/ontology/cases/{case_id}/confirm",
                  json={"target_object_id": retired["object_id"]}).status_code == 409
    # 已有 active 映射 → 409
    store.insert_identifier("u8", "abc123-m", "ABC123-M", obj["object_id"], method="manual")
    assert c.post(f"/api/ontology/cases/{case_id}/confirm",
                  json={"target_object_id": obj["object_id"]}).status_code == 409
    # case 已处置 → 409
    store.dismiss_case(case_id, by="x", note="test")
    assert c.post(f"/api/ontology/cases/{case_id}/confirm",
                  json={"target_object_id": obj["object_id"]}).status_code == 409
    assert c.post("/api/ontology/cases/ghost/confirm",
                  json={"target_object_id": obj["object_id"]}).status_code == 404


def test_confirm_race_atomic_no_leak(store, monkeypatch):
    """PR-C（P0-05）：并发方先处置 case → 确认在**同一事务**内失败整体回滚——
    不再有"先 commit 别名再补偿"的分叉窗口（旧补偿式已删除）。"""
    obj, case_id = _seed_case(store)
    snapshot = dict(store.get_case(case_id))          # 路由读到的"还 open"的旧快照
    store.dismiss_case(case_id, by="rival", note="并发方先处置")
    monkeypatch.setattr(store, "get_case", lambda cid: dict(snapshot))
    r = _client(_identity()).post(f"/api/ontology/cases/{case_id}/confirm",
                                  json={"target_object_id": obj["object_id"]})
    assert r.status_code == 409
    assert store.get_active_identifier("u8", "ABC123-M") is None   # 零副作用
    assert all(a["decision"] != "confirm" for a in store.audit_rows)   # 审计也零


def test_confirm_body_validation(store):
    _, case_id = _seed_case(store)
    r = _client(_identity()).post(f"/api/ontology/cases/{case_id}/confirm", json={})
    assert r.status_code == 422


# ── dismiss ───────────────────────────────────────────────────────────────────


def test_dismiss_requires_note_and_transitions(store):
    _, case_id = _seed_case(store)
    c = _client(_identity())
    assert c.post(f"/api/ontology/cases/{case_id}/dismiss",
                  json={"note": "  "}).status_code == 400
    assert c.post(f"/api/ontology/cases/{case_id}/dismiss",
                  json={"note": "废弃编号"}).status_code == 200
    assert store.get_case(case_id)["status"] == "dismissed"
    assert store.audits[-1]["decision"] == "dismiss"
    assert c.post(f"/api/ontology/cases/{case_id}/dismiss",
                  json={"note": "again"}).status_code == 409


# ── S3 纠错 ───────────────────────────────────────────────────────────────────


def test_deactivate_identifier(store):
    obj, _ = _seed_case(store, with_candidate=False)
    iid = store.insert_identifier("u8", "z1", "Z1", obj["object_id"], method="seed")
    c = _client(_identity())
    assert c.post(f"/api/ontology/identifiers/{iid}/deactivate",
                  json={"note": "误配"}).status_code == 200
    assert store.get_active_identifier("u8", "Z1") is None
    assert store.audits[-1]["decision"] == "deactivate"
    assert c.post(f"/api/ontology/identifiers/{iid}/deactivate", json={}).status_code == 409
    assert c.post("/api/ontology/identifiers/none/deactivate", json={}).status_code == 404


def test_repoint_identifier(store):
    a = store.mint_object("product", "杯A", owner_dept="pmc")
    b = store.mint_object("product", "杯B", owner_dept="pmc")
    iid = store.insert_identifier("u8", "p1", "P1", a["object_id"], method="seed")
    c = _client(_identity())
    r = c.post(f"/api/ontology/identifiers/{iid}/repoint",
               json={"target_object_id": b["object_id"], "target_revision": "r2"})
    assert r.status_code == 200
    new_id = r.json()["new_identifier_id"]
    active = store.get_active_identifier("u8", "P1")
    assert active["identifier_id"] == new_id
    assert active["target_object_id"] == b["object_id"]
    assert store.audits[-1]["decision"] == "repoint"
    assert store.audits[-1]["detail"]["old_target"] == a["object_id"]
    # 已 superseded 再改指 → 409；坏目标 → 404
    assert c.post(f"/api/ontology/identifiers/{iid}/repoint",
                  json={"target_object_id": a["object_id"]}).status_code == 409
    assert c.post(f"/api/ontology/identifiers/{new_id}/repoint",
                  json={"target_object_id": "nope"}).status_code == 404


def test_identifier_scope_authz_uses_object_type(store):
    """identifier 纠错的 scope 走目标对象类型：material→supply 的 dept_admin 可操作。"""
    mat = store.mint_object("material", "PP-1100", owner_dept="supply")
    iid = store.insert_identifier("material_grade", "pp-1100", "PP-1100",
                                  mat["object_id"], method="seed")
    supply_admin = _client(_identity(user_id="s1", role="dept_admin", managed=("supply",)))
    assert supply_admin.post(f"/api/ontology/identifiers/{iid}/deactivate",
                             json={}).status_code == 200
    mat2 = store.mint_object("material", "PP-2200", owner_dept="supply")
    iid2 = store.insert_identifier("material_grade", "pp-2200", "PP-2200",
                                   mat2["object_id"], method="seed")
    pmc_admin = _client(_identity(user_id="p1", role="dept_admin", managed=("pmc",)))
    assert pmc_admin.post(f"/api/ontology/identifiers/{iid2}/deactivate",
                          json={}).status_code == 403


def test_retire_and_mark_duplicate(store):
    a = store.mint_object("product", "重复品A", owner_dept="pmc")
    b = store.mint_object("product", "正主B", owner_dept="pmc")
    c = _client(_identity())
    assert c.post(f"/api/ontology/objects/{a['object_id']}/mark-duplicate",
                  json={"merged_into": b["object_id"]}).status_code == 200
    assert store.get_object(a["object_id"])["merged_into"] == b["object_id"]
    assert store.audits[-1]["decision"] == "mark_duplicate"
    # 已 merged 再 retire → 409（CAS 只认 active）
    assert c.post(f"/api/ontology/objects/{a['object_id']}/retire",
                  json={}).status_code == 409
    assert c.post(f"/api/ontology/objects/{b['object_id']}/retire",
                  json={"note": "停产"}).status_code == 200
    assert store.get_object(b["object_id"])["status"] == "retired"


def test_mark_duplicate_validation(store):
    a = store.mint_object("product", "甲", owner_dept="pmc")
    b = store.mint_object("product", "乙", owner_dept="pmc")
    store.retire_object(b["object_id"])
    c = _client(_identity())
    assert c.post(f"/api/ontology/objects/{a['object_id']}/mark-duplicate",
                  json={"merged_into": a["object_id"]}).status_code == 400   # 自指
    assert c.post(f"/api/ontology/objects/{a['object_id']}/mark-duplicate",
                  json={"merged_into": "nope"}).status_code == 404
    assert c.post(f"/api/ontology/objects/{a['object_id']}/mark-duplicate",
                  json={"merged_into": b["object_id"]}).status_code == 409   # 目标非 active


# ── PR-B（P0-01）：对象级 ACL / 存在性不可泄露矩阵 ─────────────────────────────


def test_existence_non_leak_matrix(store):
    """员工 / 无关 dept_admin / 相关 dept_admin / kb_admin × 队列/case 详情/对象搜索/详情。
    核心不变量：无权者拿到的响应与"资源不存在"逐位一致（404 / 空列表）。"""
    obj, case_id = _seed_case(store)                     # steward=pmc, obj internal owner=pmc
    conf = store.mint_object("material", "机密料", owner_dept="supply",
                             data_classification="confidential")
    # 员工：读侧一律 403（连队列入口都没有）
    emp = _client(_identity(user_id="e1", role="employee"))
    assert emp.get("/api/ontology/workbench").status_code == 403
    # 无关 dept_admin（finance）：队列空、case/对象 404、搜索不见
    other = _client(_identity(user_id="dx", role="dept_admin", managed=("finance",)))
    assert other.get("/api/ontology/workbench").json()["items"] == []
    assert other.get(f"/api/ontology/cases/{case_id}").status_code == 404
    assert other.get(f"/api/ontology/objects/{obj['object_id']}").status_code == 404
    assert other.get("/api/ontology/objects?object_type=material").json()["items"] == []
    # 相关 dept_admin（pmc）：本 scope 可见；跨部门 confidential 仍 404
    mine = _client(_identity(user_id="dp", role="dept_admin", managed=("pmc",)))
    assert len(mine.get("/api/ontology/workbench").json()["items"]) == 1
    assert mine.get(f"/api/ontology/cases/{case_id}").status_code == 200
    assert mine.get(f"/api/ontology/objects/{obj['object_id']}").status_code == 200
    assert mine.get(f"/api/ontology/objects/{conf['object_id']}").status_code == 404
    # kb_admin：全可见
    root = _client(_identity(user_id="root", role="kb_admin"))
    assert root.get(f"/api/ontology/objects/{conf['object_id']}").status_code == 200
    assert len(root.get("/api/ontology/workbench").json()["items"]) == 1


def test_workbench_candidate_cross_dept_target_masked(store):
    """case 可见但候选目标跨部门不可读 → target_visible=False，ref/title/type 全空。"""
    conf = store.mint_object("material", "机密牌号", owner_dept="supply",
                             data_classification="confidential")
    case_id = store.upsert_case("u8", "m9", "M9", object_type_hint="product")
    store.add_candidate(case_id, conf["object_id"], method="embedding", confidence=0.8)
    mine = _client(_identity(user_id="dp", role="dept_admin", managed=("pmc",)))
    detail = mine.get(f"/api/ontology/cases/{case_id}").json()
    cand = detail["candidates"][0]
    assert cand["target_visible"] is False
    assert cand["canonical_ref"] is None and cand["title"] is None
    assert cand["object_type"] is None


def test_confirm_cross_dept_confidential_target_denied(store):
    """P0-01 原始场景：营销 steward 不能把客户编号映射到供应链 confidential material。"""
    conf = store.mint_object("material", "机密料", owner_dept="supply",
                             data_classification="confidential")
    case_id = store.upsert_case("customer:KFC", "K1", "K1", object_type_hint=None)
    mkt = _client(_identity(user_id="mk", role="dept_admin", managed=("marketing",)))
    r = mkt.post(f"/api/ontology/cases/{case_id}/confirm",
                 json={"target_object_id": conf["object_id"]})
    assert r.status_code == 404                          # 目标不可见 = 不存在
    assert store.get_active_identifier("customer:KFC", "K1") is None


def test_confirm_type_mismatch_denied(store):
    """case 期望类型与目标不一致 → 400（product hint 不能确认到 material）。"""
    obj = store.mint_object("material", "PP料", owner_dept="pmc")
    case_id = store.upsert_case("u8", "m1", "M1", object_type_hint="product")
    r = _client(_identity()).post(f"/api/ontology/cases/{case_id}/confirm",
                                  json={"target_object_id": obj["object_id"]})
    assert r.status_code == 400 and "类型" in r.json()["detail"]


def test_repoint_cross_type_denied(store):
    """改指跨类型默认拒（旧目标 product → 新目标 material = 400）。"""
    a = store.mint_object("product", "甲", owner_dept="pmc")
    m = store.mint_object("material", "PP料", owner_dept="pmc")
    iid = store.insert_identifier("u8", "z9", "Z9", a["object_id"], method="seed")
    r = _client(_identity()).post(f"/api/ontology/identifiers/{iid}/repoint",
                                  json={"target_object_id": m["object_id"]})
    assert r.status_code == 400 and "类型" in r.json()["detail"]
    assert store.get_active_identifier("u8", "Z9")["target_object_id"] == a["object_id"]


# ── PR-C（P0-06）：工作台审计 fail-closed ──────────────────────────────────────


def test_workbench_audit_fail_closed_zero_side_effects(store):
    """审计不可写 → 5xx 且零副作用（case 仍 open、无别名、对象未退役）。"""
    obj, case_id = _seed_case(store)

    def _boom(payload):
        raise RuntimeError("audit db down")

    store.audit_hook = _boom
    # raise_server_exceptions=False：验证的是"未捕获异常 → 500"的生产语义
    api.app.dependency_overrides[current_identity] = (lambda i=_identity(): i)
    c = TestClient(api.app, raise_server_exceptions=False)
    r = c.post(f"/api/ontology/cases/{case_id}/confirm",
               json={"target_object_id": obj["object_id"]})
    assert r.status_code == 500
    assert store.get_case(case_id)["status"] == "open"
    assert store.get_active_identifier("u8", "ABC123-M") is None
    r2 = c.post(f"/api/ontology/objects/{obj['object_id']}/retire", json={})
    assert r2.status_code == 500
    assert store.get_object(obj["object_id"])["status"] == "active"
    assert store.audit_rows == []
    store.audit_hook = None
    assert c.post(f"/api/ontology/cases/{case_id}/confirm",
                  json={"target_object_id": obj["object_id"]}).status_code == 200
    assert store.audit_rows[-1]["decision"] == "confirm"
