# -*- coding: utf-8 -*-
"""ontology/store.py —— Memory/RDS 双后端契约测试。

同一套场景 parametrize 到两个后端（Fake 不许说谎——语义由本套契约钉住）；RDS 侧
host-pin 本地 MySQL（照 test_agent_runtime_e2e_local_db.py 模板），无 027-029 表则 skip。
RDS-only 并发三例验证 uk/发号/case 聚合在真锁下的行为。
"""
import threading
import uuid

import pytest

from opensearch_pipeline.ontology import attribute_source, stewardship
from opensearch_pipeline.ontology.store import (
    DuplicateActiveIdentifier,
    MemoryOntologyStore,
    RDSOntologyStore,
)

# ── RDS 可用性探测（host-pin + 表就位）────────────────────────────────────────────


def _rds_ready() -> bool:
    try:
        import pymysql

        from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
        cfg = get_config()
        if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
            return False
        conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                               password=cfg.rds.password, database=cfg.rds.ontology_database,
                               autocommit=True, connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name IN "
                "('ontology_object','ontology_identifier','ontology_resolution_case',"
                "'ontology_resolution_candidate','ontology_ref_seq')")
            ok = cur.fetchone()[0] == 5
        conn.close()
        return ok
    except Exception:   # noqa: BLE001
        return False


_RDS_OK = _rds_ready()
_MARK = "__pyt_"           # RDS 测试数据统一打标，teardown 按标清扫


def _cleanup_rds(_retry: bool = True):
    import pymysql

    from opensearch_pipeline.config import get_config
    cfg = get_config()
    conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.ontology_database,
                           autocommit=True, connect_timeout=3)
    like = _MARK + "%"
    with conn.cursor() as cur:
        # FK 后清扫顺序（PR-A）：candidate → case → identifier → link → object（子先父后）
        cur.execute("DELETE FROM ontology_resolution_candidate WHERE case_id IN "
                    "(SELECT case_id FROM ontology_resolution_case WHERE namespace LIKE %s)",
                    (like,))
        cur.execute("DELETE FROM ontology_resolution_case WHERE namespace LIKE %s", (like,))
        cur.execute("DELETE FROM ontology_identifier WHERE namespace LIKE %s", (like,))
        cur.execute("DELETE l FROM ontology_link l JOIN ontology_object o "
                    "ON o.object_id IN (l.src_object_id, l.dst_object_id) "
                    "WHERE o.title LIKE %s", (like,))
        cur.execute("DELETE FROM ontology_object WHERE title LIKE %s", (like,))
    try:
        conn.close()
    except Exception:   # noqa: BLE001
        pass


def _cleanup_rds_safe():
    """FK 后并行 worker 清扫可能偶发 1213 死锁——重试一次（PR-C flake 治理）。"""
    try:
        _cleanup_rds()
    except Exception:   # noqa: BLE001
        import time as _t
        _t.sleep(0.2)
        _cleanup_rds()


@pytest.fixture(params=["memory", "rds"])
def store(request):
    if request.param == "memory":
        yield MemoryOntologyStore()
        return
    if not _RDS_OK:
        pytest.skip("本地 MySQL 无 ontology 表（先 apply schema 027-029）")
    yield RDSOntologyStore()
    _cleanup_rds_safe()


@pytest.fixture()
def ns():
    """每测试唯一命名空间（RDS 侧跨测试隔离 + teardown 按前缀清扫）。"""
    return f"{_MARK}{uuid.uuid4().hex[:10]}"


def _mint(store, object_type="product", title=None, **kw):
    return store.mint_object(object_type, title or f"{_MARK}对象-{uuid.uuid4().hex[:6]}",
                             owner_dept="pmc", **kw)


# ── 对象与发号 ────────────────────────────────────────────────────────────────────


def test_mint_object_ref_format_and_increment(store):
    a = _mint(store)
    b = _mint(store)
    assert a["canonical_ref"].startswith("FLP-P-")
    assert a["object_id"] != b["object_id"]
    assert int(a["canonical_ref"].rsplit("-", 1)[1]) < int(b["canonical_ref"].rsplit("-", 1)[1])
    got = store.get_object(a["object_id"])
    assert got["title"] == a["title"] if "title" in a else True
    assert got["status"] == "active" and got["version"] == 1


def test_mint_object_unknown_type_raises(store):
    with pytest.raises(ValueError, match="未在 ontology_ref_seq 登记"):
        _mint(store, object_type="starship")


def test_find_objects_title_like(store):
    tag = uuid.uuid4().hex[:8]
    _mint(store, title=f"{_MARK}龙虾杯-{tag}")
    _mint(store, title=f"{_MARK}方形杯-{tag}")
    hits = store.find_objects("product", title_like=f"龙虾杯-{tag}")
    assert len(hits) == 1 and f"龙虾杯-{tag}" in hits[0]["title"]


def test_update_golden_cas(store):
    obj = _mint(store)
    assert store.update_golden(obj["object_id"], {"口径": "6.2"}, expected_version=1) is True
    # 旧版本号再写 → 冲突
    assert store.update_golden(obj["object_id"], {"口径": "7.0"}, expected_version=1) is False
    assert store.get_object(obj["object_id"])["version"] == 2


def test_retire_and_mark_duplicate(store):
    a, b, c = _mint(store), _mint(store), _mint(store)
    assert store.retire_object(a["object_id"]) is True
    assert store.retire_object(a["object_id"]) is False          # 已非 active，CAS 让位
    # P0-03：merge 目标必须 active——往退役对象里合并=把身份指进坟墓，拒
    with pytest.raises(ValueError, match="非 active"):
        store.mark_duplicate(b["object_id"], a["object_id"])
    assert store.mark_duplicate(b["object_id"], c["object_id"]) is True
    got = store.get_object(b["object_id"])
    assert got["status"] == "merged" and got["merged_into"] == c["object_id"]
    with pytest.raises(ValueError, match="自身"):
        store.mark_duplicate(c["object_id"], c["object_id"])


def test_retire_cascades_identifiers_and_links(store, ns):
    """P0-03：退役同事务级联——active 别名→retired、双向 link→retired，不留 active 引用。"""
    a, b = _mint(store), _mint(store)
    iid = store.insert_identifier(ns, "abc", "ABC", a["object_id"], method="seed")
    lid = store.add_link(a["object_id"], b["object_id"], "sku_of_product")
    assert store.retire_object(a["object_id"]) is True
    assert store.get_active_identifier(ns, "ABC") is None
    ident = [r for r in store.list_identifiers_for_target(a["object_id"])
             if r["identifier_id"] == iid][0]
    assert ident["status"] == "retired"
    links = store.list_links(src_object_id=a["object_id"], status=None)
    assert [r["status"] for r in links if r["link_id"] == lid] == ["retired"]


def test_mark_duplicate_repoints_identifiers_same_tx(store, ns):
    """P0-03：merge 同事务把 source 的 active 别名改指 target（身份连续），source link 退场。"""
    a, b = _mint(store), _mint(store)
    old_id = store.insert_identifier(ns, "abc", "ABC", a["object_id"], method="seed")
    assert store.mark_duplicate(a["object_id"], b["object_id"], by="steward_1") is True
    active = store.get_active_identifier(ns, "ABC")
    assert active is not None and active["target_object_id"] == b["object_id"]
    assert active["identifier_id"] != old_id
    olds = [r for r in store.list_identifiers_for_target(a["object_id"])
            if r["identifier_id"] == old_id]
    assert olds[0]["status"] == "superseded"
    assert olds[0]["superseded_by"] == active["identifier_id"]


def test_mark_duplicate_rejects_cross_type(store):
    """P0-03：跨类型合并=语义脊柱污染，拒（product 不能 merge 进 material）。"""
    p = _mint(store, object_type="product")
    m = _mint(store, object_type="material")
    with pytest.raises(ValueError, match="跨类型"):
        store.mark_duplicate(p["object_id"], m["object_id"])
    assert store.get_object(p["object_id"])["status"] == "active"   # 原子：闸拒后无半状态


def test_mark_duplicate_no_cycle(store):
    """P0-03 防环：A merged→B 后 B 再 merge→A 必拒（A 非 active）——active 目标链长恒 0。"""
    a, b = _mint(store), _mint(store)
    assert store.mark_duplicate(a["object_id"], b["object_id"]) is True
    with pytest.raises(ValueError, match="非 active"):
        store.mark_duplicate(b["object_id"], a["object_id"])


def test_insert_identifier_dangling_target_rejected(store, ns):
    """P0-03 FK：悬空目标拒绝（RDS=1452→ValueError，Memory=显式存在性校验，同契约）。"""
    with pytest.raises(ValueError, match="不存在"):
        store.insert_identifier(ns, "x", "X", "0" * 26, method="seed")
    with pytest.raises(ValueError, match="不存在"):
        store.add_link("0" * 26, "1" * 26, "sku_of_product")


def test_identifier_binary_collation_case_sensitive(store, ns):
    """P0-04：customer 归一保大小写 → 唯一键必须 binary 口径——'A(高钙)' 与 'a(高钙)'
    是两个身份，两后端都必须可同时 active（修复前 RDS unicode_ci 会撞 uk 而 Memory 不撞）。"""
    obj1, obj2 = _mint(store), _mint(store)
    cns = f"{ns}:kfc"   # customer 前缀语义：保大小写
    i_upper = store.insert_identifier(cns, "A(高钙)", "A(高钙)", obj1["object_id"],
                                      method="manual")
    i_lower = store.insert_identifier(cns, "a(高钙)", "a(高钙)", obj2["object_id"],
                                      method="manual")
    assert i_upper != i_lower
    assert store.get_active_identifier(cns, "A(高钙)")["target_object_id"] == obj1["object_id"]
    assert store.get_active_identifier(cns, "a(高钙)")["target_object_id"] == obj2["object_id"]
    # 同大小写重插仍然撞（唯一键语义不因 binary 而放松）
    with pytest.raises(DuplicateActiveIdentifier):
        store.insert_identifier(cns, "A(高钙)", "A(高钙)", obj1["object_id"], method="manual")


# ── 别名：至多一行 active / 纠错 ──────────────────────────────────────────────────


def test_identifier_single_active_and_reinsert_after_deactivate(store, ns):
    obj = _mint(store)
    i1 = store.insert_identifier(ns, "abc123", "ABC123", obj["object_id"], method="seed")
    with pytest.raises(DuplicateActiveIdentifier):
        store.insert_identifier(ns, "abc123", "ABC123", obj["object_id"], method="manual")
    assert store.get_active_identifier(ns, "ABC123")["identifier_id"] == i1
    # 纠错：deactivate 后可重新确认
    assert store.deactivate_identifier(i1) is True
    assert store.deactivate_identifier(i1) is False               # 二次 CAS 让位
    assert store.get_active_identifier(ns, "ABC123") is None
    i2 = store.insert_identifier(ns, "abc123", "ABC123", obj["object_id"], method="manual")
    assert store.get_active_identifier(ns, "ABC123")["identifier_id"] == i2


def test_deactivate_rejects_bad_status(store, ns):
    obj = _mint(store)
    iid = store.insert_identifier(ns, "x", "X", obj["object_id"], method="seed")
    with pytest.raises(ValueError, match="终态"):
        store.deactivate_identifier(iid, status="active")


def test_repoint_identifier_atomic(store, ns):
    a, b = _mint(store), _mint(store)
    old_id = store.insert_identifier(ns, "abc123-m", "ABC123-M", a["object_id"], method="rule",
                                     confidence=0.97)
    new_id = store.repoint_identifier(old_id, b["object_id"], by="steward_1",
                                      new_target_revision="r2")
    active = store.get_active_identifier(ns, "ABC123-M")
    assert active["identifier_id"] == new_id
    assert active["target_object_id"] == b["object_id"]
    assert active["target_revision"] == "r2"
    assert active["confirmed_by"] == "steward_1"
    olds = [r for r in store.list_identifiers_for_target(a["object_id"])
            if r["identifier_id"] == old_id]
    assert olds[0]["status"] == "superseded" and olds[0]["superseded_by"] == new_id
    # 非 active 再改指 → 拒绝
    with pytest.raises(ValueError, match="非 active"):
        store.repoint_identifier(old_id, a["object_id"], by="steward_1")


def test_list_identifiers_for_target(store, ns):
    obj = _mint(store)
    store.insert_identifier(ns, "a", "A", obj["object_id"], method="seed")
    store.insert_identifier(f"{ns}:kfc", "b", "B", obj["object_id"], method="manual")
    rows = store.list_identifiers_for_target(obj["object_id"])
    assert {r["norm_value"] for r in rows} == {"A", "B"}
    assert all(r["status"] == "active" for r in rows)


# ── 消解 case / 候选 ──────────────────────────────────────────────────────────────


def test_get_open_case_lookup(store, ns):
    cid = store.upsert_case(ns, "u z", "U Z")
    hit = store.get_open_case(ns, "U Z")
    assert hit and hit["case_id"] == cid
    assert store.get_open_case(ns, "NOPE") is None
    store.dismiss_case(cid, by="s1", note="废弃")
    assert store.get_open_case(ns, "U Z") is None        # 只认 open


def test_upsert_case_aggregates_observations(store, ns):
    c1 = store.upsert_case(ns, "u z", "U Z", object_type_hint="sku",
                           evidence={"source": "pmc_query"})
    c2 = store.upsert_case(ns, "u z", "U Z")
    assert c1 == c2                                              # 同 (ns,norm) 聚合到同一 open case
    case = store.get_case(c1)
    assert case["seen_count"] == 2 and case["status"] == "open"
    assert case["evidence_json"] is not None                     # 首次证据快照保留不覆盖


def test_case_resolve_and_dismiss_cas(store, ns):
    obj = _mint(store)
    c1 = store.upsert_case(ns, "abc", "ABC")
    iid = store.insert_identifier(ns, "abc", "ABC", obj["object_id"], method="manual",
                                  confirmed_by="steward_1", source_case_id=c1)
    assert store.resolve_case(c1, identifier_id=iid, by="steward_1") is True
    assert store.resolve_case(c1, identifier_id=iid, by="steward_1") is False   # 二次让位
    # dismiss 必须给理由
    c2 = store.upsert_case(ns, "xyz", "XYZ")
    with pytest.raises(ValueError, match="理由"):
        store.dismiss_case(c2, by="steward_1", note="  ")
    assert store.dismiss_case(c2, by="steward_1", note="确认为废弃编号") is True
    # 处置后可重开新 case（历史留档）
    c3 = store.upsert_case(ns, "xyz", "XYZ")
    assert c3 != c2 and store.get_case(c3)["seen_count"] == 1


def test_candidates_dedupe_and_max_confidence(store, ns):
    obj = _mint(store)
    cid = store.upsert_case(ns, "abc-m", "ABC-M")
    k1 = store.add_candidate(cid, obj["object_id"], method="rule", confidence=0.80)
    k2 = store.add_candidate(cid, obj["object_id"], method="rule", confidence=0.90,
                             features={"hint": "剥后缀"})
    assert k1 == k2                                              # 同 (case,target,method) 幂等
    k3 = store.add_candidate(cid, obj["object_id"], method="embedding", confidence=0.70)
    rows = store.list_candidates(cid)
    assert len(rows) == 2 and k3 in {r["candidate_id"] for r in rows}
    rule_row = [r for r in rows if r["method"] == "rule"][0]
    assert float(rule_row["confidence"]) == pytest.approx(0.90)  # 置信取大
    assert rows[0]["method"] == "rule"                           # 按置信降序


def test_list_open_cases_filter_and_order(store, ns):
    store.upsert_case(ns, "a", "A", object_type_hint="product")
    cb = store.upsert_case(ns, "b", "B", object_type_hint="sku")
    store.upsert_case(ns, "b", "B")                              # B 观测两次 → freq 序在前
    rows = store.list_open_cases(namespace=ns, order="freq")
    assert [r["case_id"] for r in rows][0] == cb
    only_sku = store.list_open_cases(namespace=ns, object_type_hint="sku")
    assert len(only_sku) == 1 and only_sku[0]["case_id"] == cb


# ── 溯源目录 / stewardship 种子 ───────────────────────────────────────────────────


def test_attribute_source_seeds_idempotent(store):
    n1 = attribute_source.ensure_seeds(store)
    n2 = attribute_source.ensure_seeds(store)
    assert n1 == n2 == len(attribute_source.SEED_ROWS)
    rows = store.list_attribute_sources()
    keys = {(r["object_type"], r["attribute"]) for r in rows}
    assert ("sku", "箱规") in keys and ("sku", "香规") in keys


def test_stewardship_seeds_and_resolution(store):
    stewardship.ensure_seeds(store)
    rows = store.list_stewardship()
    # 优先级：namespace 全名 > 前缀 > object_type
    assert stewardship.resolve_steward(rows, namespace="customer:KFC")["steward_dept"] \
        == "marketing"
    assert stewardship.resolve_steward(rows, namespace="lab_sample")["steward_dept"] == "rd"
    assert stewardship.resolve_steward(rows, object_type="material")["steward_dept"] == "supply"
    assert stewardship.resolve_steward(
        rows, namespace="u8", object_type="product")["steward_dept"] == "pmc"
    # 未命中 → None（调用方 fail-closed 到 kb_admin）
    assert stewardship.resolve_steward(rows, namespace="unknown_ns") is None
    # attribute 最高优先
    store.upsert_stewardship([{"scope_type": "attribute", "scope_key": "sku.箱规",
                               "steward_dept": "quality"}])
    rows = store.list_stewardship()
    assert stewardship.resolve_steward(
        rows, attribute="sku.箱规", object_type="sku")["steward_dept"] == "quality"


def test_stewardship_seed_depts_within_acl_whitelist():
    """S5 风险护栏：种子 steward 组码必须 ⊆ 既有 ACL 白名单（新组码=独立灰度 PR）。"""
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS
    for row in stewardship.SEED_SCOPES:
        assert row["steward_dept"] in _VALID_ACL_GROUPS, row
        if row.get("backup_dept"):
            assert row["backup_dept"] in _VALID_ACL_GROUPS, row


# ── 覆盖率（memory-only：RDS 侧全局计数受并行测试污染）────────────────────────────


def test_coverage_math_memory():
    store = MemoryOntologyStore()
    obj = store.mint_object("product", "杯", owner_dept="pmc")
    store.insert_identifier("u8", "a", "A", obj["object_id"], method="seed",
                            confirmed_by="auto")
    store.insert_identifier("u8", "b", "B", obj["object_id"], method="manual",
                            confirmed_by="steward_1")
    store.upsert_case("u8", "c", "C", object_type_hint="product")
    cov = store.coverage()
    assert cov["active_identifiers"] == 2 and cov["auto_active"] == 1
    assert cov["open_cases"] == 1
    assert cov["resolution_coverage"] == pytest.approx(2 / 3)
    # 按类型过滤：sku 维度无数据
    empty = store.coverage(object_type="sku")
    assert empty["active_identifiers"] == 0 and empty["resolution_coverage"] is None


# ── RDS-only 并发（真锁/真唯一键）────────────────────────────────────────────────

rds_only = pytest.mark.skipif(not _RDS_OK, reason="本地 MySQL 无 ontology 表")


@rds_only
def test_rds_concurrent_mint_unique_refs():
    store = RDSOntologyStore()
    refs, errors = [], []

    def _work():
        try:
            for _ in range(5):
                refs.append(_mint(store)["canonical_ref"])
        except Exception as e:   # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_work) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    try:
        assert not errors and len(refs) == 10 and len(set(refs)) == 10
    finally:
        _cleanup_rds_safe()


@rds_only
def test_rds_concurrent_case_upsert_single_open():
    store = RDSOntologyStore()
    ns_v = f"{_MARK}{uuid.uuid4().hex[:10]}"
    ids, errors = [], []

    def _work():
        try:
            ids.append(store.upsert_case(ns_v, "raw", "NORM"))
        except Exception as e:   # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    try:
        assert not errors and len(set(ids)) == 1
        assert store.get_case(ids[0])["seen_count"] == 4
    finally:
        _cleanup_rds_safe()


@rds_only
def test_rds_concurrent_insert_identifier_single_winner():
    store = RDSOntologyStore()
    ns_v = f"{_MARK}{uuid.uuid4().hex[:10]}"
    obj = _mint(store)
    outcomes = []

    def _work():
        try:
            store.insert_identifier(ns_v, "x", "X", obj["object_id"], method="manual")
            outcomes.append("ok")
        except DuplicateActiveIdentifier:
            outcomes.append("dup")
        except Exception as e:   # noqa: BLE001
            outcomes.append(f"err:{e}")

    threads = [threading.Thread(target=_work) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    try:
        assert sorted(outcomes) == ["dup", "ok"]
    finally:
        _cleanup_rds_safe()


@rds_only
def test_rds_global_unique_ref_and_type_code():
    """P0-04（RDS-only，DB 层强制）：canonical_ref 全局 UNIQUE、ref_seq.type_code 全局 UNIQUE。"""
    import pymysql

    from opensearch_pipeline.config import get_config
    cfg = get_config()
    conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.ontology_database,
                           autocommit=True, connect_timeout=3)
    store = RDSOntologyStore()
    obj = _mint(store)
    try:
        with conn.cursor() as cur:
            # 同 canonical_ref 换个 object_type 直插 → 撞 uk_ref（旧 uk 只在 (type,ref) 上拦不住）
            with pytest.raises(Exception) as exc1:
                cur.execute(
                    "INSERT INTO ontology_object (object_id, object_type, canonical_ref, "
                    "title, golden_json, owner_dept) VALUES (%s,'material',%s,%s,'{}','pmc')",
                    ("Z" * 26, obj["canonical_ref"], f"{_MARK}撞号"))
            assert exc1.value.args[0] == 1062
            # 同 type_code 换个 object_type → 撞 uk_type_code
            with pytest.raises(Exception) as exc2:
                cur.execute("INSERT INTO ontology_ref_seq (object_type, type_code, next_no) "
                            "VALUES (%s,'P',0)", (f"{_MARK}fake_type",))
            assert exc2.value.args[0] == 1062
    finally:
        conn.close()
        _cleanup_rds_safe()


@rds_only
def test_rds_concurrent_retire_vs_resolve_no_zombie():
    """P0-03 并发：retire 与 exact-resolve 竞态——resolve 要么在退役前 resolved，
    要么退役后 unresolved，绝不返回指向 retired 对象的 resolved。"""
    from opensearch_pipeline.ontology.resolve import OntologyResolver
    store = RDSOntologyStore()
    ns_v = f"{_MARK}{uuid.uuid4().hex[:10]}"
    obj = _mint(store)
    store.insert_identifier(ns_v, "x1", "X1", obj["object_id"], method="seed")
    resolver = OntologyResolver(store, embedder=None)
    results, errors = [], []

    def _retire():
        try:
            store.retire_object(obj["object_id"])
        except Exception as e:   # noqa: BLE001
            errors.append(e)

    def _resolve():
        try:
            for _ in range(6):
                results.append(resolver.resolve(ns_v, "x1"))
        except Exception as e:   # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_retire), threading.Thread(target=_resolve)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    try:
        assert not errors
        for r in results:
            if r.status == "resolved":
                continue   # 退役前窗口的合法命中
            assert r.status == "unresolved"
        # 终态：退役完成后必 unresolved
        final = resolver.resolve(ns_v, "x1")
        assert final.status == "unresolved" and final.requires_hitl is True
    finally:
        _cleanup_rds_safe()


# ── PR-C（P0-05/06）：原子复合写 + 同事务审计 ────────────────────────────────────


def test_mint_object_with_alias_atomic_no_orphan(store, ns):
    """别名撞 uk → 整体回滚：对象数零增长（P0-05 孤儿对象根除）。"""
    winner = _mint(store)
    store.insert_identifier(ns, "x", "X", winner["object_id"], method="seed")
    before = len(store.find_objects("product", limit=200))
    with pytest.raises(DuplicateActiveIdentifier):
        store.mint_object_with_alias(
            "product", f"{_MARK}撞名者-{uuid.uuid4().hex[:6]}", owner_dept="pmc",
            namespace=ns, raw_value="x", norm_value="X")
    assert len(store.find_objects("product", limit=200)) == before   # 零孤儿


def test_mint_object_with_alias_closes_open_case(store, ns):
    obj_before = store.upsert_case(ns, "y", "Y")
    res = store.mint_object_with_alias(
        "product", f"{_MARK}新品-{uuid.uuid4().hex[:6]}", owner_dept="pmc",
        namespace=ns, raw_value="y", norm_value="Y", confirmed_by="auto")
    assert res["closed_case_id"] == obj_before
    assert store.get_open_case(ns, "Y") is None
    active = store.get_active_identifier(ns, "Y")
    assert active["target_object_id"] == res["object_id"]
    assert active["source_case_id"] == obj_before


def test_confirm_case_with_identifier_atomic(store, ns):
    obj = _mint(store)
    case_id = store.upsert_case(ns, "z", "Z")
    iid = store.confirm_case_with_identifier(case_id, target_object_id=obj["object_id"],
                                             by="steward_1", note="确认")
    case = store.get_case(case_id)
    assert case["status"] == "resolved" and case["resolved_identifier_id"] == iid
    assert store.get_active_identifier(ns, "Z")["source_case_id"] == case_id
    # 已处置 case 再确认 → ValueError（事务内校验，无半状态）
    with pytest.raises(ValueError, match="非 open"):
        store.confirm_case_with_identifier(case_id, target_object_id=obj["object_id"],
                                           by="steward_1")


def test_confirm_case_duplicate_alias_rolls_back_case(store, ns):
    """铸别名撞 uk → case 保持 open（整体回滚，不出现 case 关了别名没生效）。"""
    a, b = _mint(store), _mint(store)
    store.insert_identifier(ns, "w", "W", a["object_id"], method="seed")
    case_id = store.upsert_case(ns, "w", "W")
    with pytest.raises(DuplicateActiveIdentifier):
        store.confirm_case_with_identifier(case_id, target_object_id=b["object_id"],
                                           by="steward_1")
    assert store.get_case(case_id)["status"] == "open"


def test_insert_identifier_closing_case_atomic(store, ns):
    obj = _mint(store)
    case_id = store.upsert_case(ns, "v", "V")
    iid, closed = store.insert_identifier_closing_case(
        ns, "v", "V", obj["object_id"], confirmed_by="approver_1",
        approval_request_id="req_" + "0" * 28)
    assert closed == case_id
    assert store.get_case(case_id)["status"] == "resolved"
    row = store.get_active_identifier(ns, "V")
    assert row["identifier_id"] == iid
    assert row["confirmed_by"] == "approver_1"
    assert row["approval_request_id"] == "req_" + "0" * 28   # 025↔028 回链（P0-06）


def test_memory_audit_failure_zero_side_effects():
    """P0-06：审计不可写 → 变更零副作用（Memory hook 模拟 agent_audit_log 故障）。"""
    store = MemoryOntologyStore()
    obj = store.mint_object("product", "审计目标", owner_dept="pmc")
    store.insert_identifier("u8", "a1", "A1", obj["object_id"], method="seed")

    def _boom(payload):
        raise RuntimeError("audit db down")

    store.audit_hook = _boom
    payload = {"action": "object:x", "decision": "retire", "by": "u1"}
    with pytest.raises(RuntimeError):
        store.retire_object(obj["object_id"], audit=payload)
    assert store.get_object(obj["object_id"])["status"] == "active"     # 未退役
    assert store.get_active_identifier("u8", "A1") is not None          # 别名未级联
    assert store.audit_rows == []
    store.audit_hook = None
    assert store.retire_object(obj["object_id"], audit=payload) is True
    assert store.audit_rows[-1]["decision"] == "retire"


@rds_only
def test_rds_audit_row_lands_with_confirm():
    """RDS：confirm 的审计行随事务落 fuling_operation.agent_audit_log（同连接跨库）。"""
    import pymysql

    from opensearch_pipeline.config import get_config
    cfg = get_config()
    store = RDSOntologyStore()
    ns_v = f"{_MARK}{uuid.uuid4().hex[:10]}"
    obj = _mint(store)
    case_id = store.upsert_case(ns_v, "q", "Q")
    marker = f"{_MARK}{uuid.uuid4().hex[:8]}"
    try:
        store.confirm_case_with_identifier(
            case_id, target_object_id=obj["object_id"], by="steward_1",
            audit={"event_type": "ontology_workbench", "action": f"case:{case_id}",
                   "decision": "confirm", "by": marker, "detail": {}})
        conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                               password=cfg.rds.password,
                               database=cfg.rds.operation_database,
                               autocommit=True, connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SELECT decision, detail_json FROM agent_audit_log "
                        "WHERE user_id=%s", (marker,))
            rows = cur.fetchall()
            assert len(rows) == 1 and rows[0][0] == "confirm"
            assert "identifier_id" in (rows[0][1] or "")
            cur.execute("DELETE FROM agent_audit_log WHERE user_id=%s", (marker,))
        conn.close()
    finally:
        _cleanup_rds_safe()


# ── PR-C：不变量 reaper ───────────────────────────────────────────────────────


def test_invariant_scan_clean_and_dirty(store, ns):
    from opensearch_pipeline.ontology.invariants import scan_invariants
    obj = _mint(store)
    store.insert_identifier(ns, "h", "H", obj["object_id"], method="seed")
    report = scan_invariants(store)
    ours = {(v.get("namespace"), v.get("norm_value")) for vs in report.values() for v in vs}
    assert (ns, "H") not in ours                       # 健康态：本测试数据无违例
    # 制造 alias_open_case 尸案：active 别名与 open case 并存（绕过原子入口直插）
    case_id = store.upsert_case(ns, "h", "H")
    report2 = scan_invariants(store)
    assert any(v["case_id"] == case_id for v in report2["alias_open_case"])
