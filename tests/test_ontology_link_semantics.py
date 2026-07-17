# -*- coding: utf-8 -*-
"""P1-8/P1-9/P2（2026-07-11 外评收口）——link 语义完整性 + 裸写调用方闸 + 白名单 fail-closed。

- P1-8：add_link 端点存在/active/类型匹配（LINK_TYPE_SPECS）+ single 基数拒双活
  （服务层双后端契约 + RDS 侧 schema/033 生成列唯一键 DB 兜底）；
- P1-9：mint_object / insert_identifier / add_link 裸写原语须显式 _caller 声明；
- P2：owner_dept 白名单加载失败 → fail-closed 拒写（不再告警放行）。

RDS 侧 host-pin 本地 MySQL（照 test_ontology_store.py 模板），无表则 skip；
独立打标前缀 __pyl_（与其他 ontology 测试文件各扫各的标——但打标只隔数据不隔锁，
本文件已随 ontology 真库族并入 conftest 串行组，2026-07-17 flake 根治）。
"""
import os
import uuid

import pytest

from opensearch_pipeline.ontology.store import (
    LINK_TYPE_SPECS,
    LinkCardinalityViolation,
    MemoryOntologyStore,
    RDSOntologyStore,
    SEM_PROJECTIONS,
    VALID_LINK_TYPES,
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
                "('ontology_object','ontology_identifier','ontology_link','ontology_ref_seq')")
            ok = cur.fetchone()[0] == 4
        conn.close()
        return ok
    except Exception:   # noqa: BLE001
        return False


_RDS_OK = _rds_ready()

if not _RDS_OK and os.environ.get("RAG_ONTOLOGY_TESTS_REQUIRE_RDS", "").strip() == "1":
    raise RuntimeError(
        "RAG_ONTOLOGY_TESTS_REQUIRE_RDS=1 但本地 MySQL/ontology 表不可用——"
        "真库契约族不许静默 skip（P0-08）；检查 ci_load_schema 与连接配置")

_MARK = "__pyl_"


def _cleanup_rds():
    import pymysql

    from opensearch_pipeline.config import get_config
    cfg = get_config()
    conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.ontology_database,
                           autocommit=True, connect_timeout=3)
    like = _MARK + "%"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ontology_identifier WHERE namespace LIKE %s", (like,))
        cur.execute("DELETE l FROM ontology_link l JOIN ontology_object o "
                    "ON o.object_id IN (l.src_object_id, l.dst_object_id) "
                    "WHERE o.title LIKE %s", (like,))
        cur.execute("UPDATE ontology_object SET merged_into=NULL "
                    "WHERE merged_into IS NOT NULL AND title LIKE %s", (like,))
        cur.execute("DELETE FROM ontology_object WHERE title LIKE %s", (like,))
    try:
        conn.close()
    except Exception:   # noqa: BLE001
        pass


def _cleanup_rds_safe():
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
        pytest.skip("本地 MySQL 无 ontology 表（先 apply schema 027-029+033）")
    yield RDSOntologyStore()
    _cleanup_rds_safe()


def _mint(store, object_type="product", **kw):
    return store.mint_object(object_type, f"{_MARK}{object_type}-{uuid.uuid4().hex[:6]}",
                             owner_dept="pmc", _caller="test", **kw)


rds_only = pytest.mark.skipif(not _RDS_OK, reason="本地 MySQL 无 ontology 表")


# ── P1-8：LINK_TYPE_SPECS 契约本身 ─────────────────────────────────────────────────


def test_link_type_specs_shape_and_sem_consistency():
    """每个白名单 link 型都声明端点与基数；sem 投影消费的 link 型与 SPECS 一致。"""
    assert set(VALID_LINK_TYPES) == set(LINK_TYPE_SPECS)
    for t, spec in LINK_TYPE_SPECS.items():
        assert spec["src_type"] and spec["dst_type"], t
        assert spec["src_cardinality"] in ("single", "multi"), t
    for proj in SEM_PROJECTIONS.values():
        spec = LINK_TYPE_SPECS[proj["link_type"]]
        assert spec["src_type"] == proj["spec_type"]      # spec --link--> sku
        assert spec["dst_type"] == "sku"
        assert spec["src_cardinality"] == "single"        # 一份 spec 只描述一个 SKU


# ── P1-8：端点校验（双后端契约）───────────────────────────────────────────────────


def test_add_link_rejects_type_mismatch(store):
    """product --sku_of_product--> product：src 类型错配 → 拒（sem 串味根源）。"""
    a, b = _mint(store), _mint(store)
    with pytest.raises(ValueError, match="要求 src=sku"):
        store.add_link(a["object_id"], b["object_id"], "sku_of_product", _caller="test")
    sku1, sku2 = _mint(store, object_type="sku"), _mint(store, object_type="sku")
    with pytest.raises(ValueError, match="要求 dst=product"):
        store.add_link(sku1["object_id"], sku2["object_id"], "sku_of_product", _caller="test")


def test_add_link_rejects_non_active_endpoint(store):
    """退役对象不得再建关系（此前只有 FK 存在性校验，retired 也能挂新边）。"""
    sku = _mint(store, object_type="sku")
    prod = _mint(store)
    assert store.retire_object(prod["object_id"]) is True
    with pytest.raises(ValueError, match="非 active"):
        store.add_link(sku["object_id"], prod["object_id"], "sku_of_product", _caller="test")


def test_add_link_rejects_self_link(store):
    a = _mint(store, object_type="sku")
    with pytest.raises(ValueError, match="自指"):
        store.add_link(a["object_id"], a["object_id"], "sku_of_product", _caller="test")


# ── P1-8：single 基数（双后端契约 + 释放语义）─────────────────────────────────────


def test_single_cardinality_rejects_second_active(store):
    """外评原文场景：一个 SKU 出现两条 active sku_of_product → 第二条必须拒。"""
    sku = _mint(store, object_type="sku")
    p1, p2 = _mint(store), _mint(store)
    lid = store.add_link(sku["object_id"], p1["object_id"], "sku_of_product", _caller="test")
    # 同关系重建=幂等返回原行
    assert store.add_link(sku["object_id"], p1["object_id"], "sku_of_product",
                          _caller="test") == lid
    with pytest.raises(LinkCardinalityViolation, match="single"):
        store.add_link(sku["object_id"], p2["object_id"], "sku_of_product", _caller="test")
    active = store.list_links(src_object_id=sku["object_id"], link_type="sku_of_product")
    assert len(active) == 1 and active[0]["dst_object_id"] == p1["object_id"]


def test_single_cardinality_frees_slot_after_retire(store):
    """旧关系随对象退役级联 retire 后，关系位释放——可改挂新目标（S3 纠错闭环）。"""
    sku = _mint(store, object_type="sku")
    p1, p2 = _mint(store), _mint(store)
    store.add_link(sku["object_id"], p1["object_id"], "sku_of_product", _caller="test")
    assert store.retire_object(p1["object_id"]) is True          # 级联 retire 该 link
    lid2 = store.add_link(sku["object_id"], p2["object_id"], "sku_of_product", _caller="test")
    active = store.list_links(src_object_id=sku["object_id"], link_type="sku_of_product")
    assert [r["link_id"] for r in active] == [lid2]


def test_multi_cardinality_allows_fanout(store):
    """material_of_product 声明 multi：同一牌号供多产品不受限。"""
    mat = _mint(store, object_type="material")
    p1, p2 = _mint(store), _mint(store)
    store.add_link(mat["object_id"], p1["object_id"], "material_of_product", _caller="test")
    store.add_link(mat["object_id"], p2["object_id"], "material_of_product", _caller="test")
    assert len(store.list_links(src_object_id=mat["object_id"],
                                link_type="material_of_product")) == 2


def test_spec_fanin_multiple_specs_per_sku_still_allowed(store):
    """single 限的是 src 侧（一份 spec 只描述一个 SKU）；同 SKU 挂多份 spec 合法
    （verified/draft 并存由 sem 挑行）——别把基数闸做反。"""
    sku = _mint(store, object_type="sku")
    s1 = _mint(store, object_type="packing_spec")
    s2 = _mint(store, object_type="packing_spec")
    store.add_link(s1["object_id"], sku["object_id"], "packing_spec_of_sku", _caller="test")
    store.add_link(s2["object_id"], sku["object_id"], "packing_spec_of_sku", _caller="test")
    assert len(store.list_links(dst_object_id=sku["object_id"],
                                link_type="packing_spec_of_sku")) == 2


@rds_only
def test_rds_db_backstop_generated_unique_key():
    """schema/033 DB 兜底：绕过服务层直插第二条 active single link → 1062。
    multi 型（material_of_product）不受生成列约束。"""
    import pymysql

    from opensearch_pipeline.config import get_config
    cfg = get_config()
    conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.ontology_database,
                           autocommit=True, connect_timeout=3)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='ontology_link' "
                    "AND COLUMN_NAME='active_single_key'")
        if cur.fetchone()[0] != 1:
            conn.close()
            pytest.skip("schema/033 未 apply（无 active_single_key 生成列）")
    store = RDSOntologyStore()
    sku = _mint(store, object_type="sku")
    p1, p2 = _mint(store), _mint(store)
    mat = _mint(store, object_type="material")
    try:
        with conn.cursor() as cur:
            def _raw_insert(src, dst, ltype):
                cur.execute(
                    "INSERT INTO ontology_link (link_id, src_object_id, dst_object_id, "
                    "link_type) VALUES (%s,%s,%s,%s)",
                    (uuid.uuid4().hex[:26].upper(), src, dst, ltype))

            _raw_insert(sku["object_id"], p1["object_id"], "sku_of_product")
            with pytest.raises(Exception) as exc:
                _raw_insert(sku["object_id"], p2["object_id"], "sku_of_product")
            assert exc.value.args[0] == 1062                      # uk_link_active_single
            _raw_insert(mat["object_id"], p1["object_id"], "material_of_product")
            _raw_insert(mat["object_id"], p2["object_id"], "material_of_product")  # multi 放行
    finally:
        conn.close()
        _cleanup_rds_safe()


# ── P1-9：裸写原语调用方闸（双后端契约）────────────────────────────────────────────


def test_bare_writes_require_explicit_caller(store):
    """mint_object / insert_identifier / add_link 未声明 _caller → PermissionError，
    零副作用；非法值同拒。"""
    with pytest.raises(PermissionError, match="_caller"):
        store.mint_object("product", f"{_MARK}闸-{uuid.uuid4().hex[:6]}", owner_dept="pmc")
    with pytest.raises(PermissionError, match="_caller"):
        store.mint_object("product", f"{_MARK}闸-{uuid.uuid4().hex[:6]}", owner_dept="pmc",
                          _caller="random_service")
    obj = _mint(store)
    ns = f"{_MARK}{uuid.uuid4().hex[:10]}"
    with pytest.raises(PermissionError, match="_caller"):
        store.insert_identifier(ns, "x", "X", obj["object_id"], method="seed")
    assert store.get_active_identifier(ns, "X") is None           # 零副作用
    sku = _mint(store, object_type="sku")
    with pytest.raises(PermissionError, match="_caller"):
        store.add_link(sku["object_id"], obj["object_id"], "sku_of_product")
    assert store.list_links(src_object_id=sku["object_id"]) == []


def test_lifecycle_composites_stay_open(store):
    """受祝福入口不设闸：mint_object_with_alias / confirm_case_with_identifier /
    repoint_identifier 照常可用（routes/agent_tools 冻结调用面不破）。"""
    ns = f"{_MARK}{uuid.uuid4().hex[:10]}"
    res = store.mint_object_with_alias(
        "product", f"{_MARK}复合-{uuid.uuid4().hex[:6]}", owner_dept="pmc",
        namespace=ns, raw_value="c1", norm_value="C1", confirmed_by="auto")
    assert store.get_active_identifier(ns, "C1")["target_object_id"] == res["object_id"]
    new_id = store.repoint_identifier(res["identifier_id"], _mint(store)["object_id"],
                                      by="steward_1")
    assert store.get_active_identifier(ns, "C1")["identifier_id"] == new_id


# ── P2：owner_dept 白名单 fail-closed ─────────────────────────────────────────────


def test_owner_dept_whitelist_failure_fails_closed(monkeypatch):
    """白名单加载失败（缺依赖/坏配置）→ RuntimeError 拒写，绝不静默放行（旧行为）。"""
    import opensearch_pipeline.kb_authz as kb_authz

    def _boom():
        raise ImportError("kb_authz 依赖缺失（模拟极简 pod）")

    monkeypatch.setattr(kb_authz, "_valid_owner_depts", _boom)
    store = MemoryOntologyStore()
    with pytest.raises(RuntimeError, match="fail-closed"):
        store.mint_object("product", "白名单故障品", owner_dept="pmc", _caller="test")
    assert store.find_objects("product") == []                    # 零副作用
    with pytest.raises(RuntimeError, match="fail-closed"):
        store.mint_object_with_alias("product", "白名单故障品2", owner_dept="pmc",
                                     namespace="u8", raw_value="w", norm_value="W")
