# -*- coding: utf-8 -*-
"""ontology/sem.py + store 关系/sem 投影（PR10）。

锁死的契约：
- store：add_link 幂等（uk 撞行返回现行 id）· sem_spec_rows 三方同形
  （视图 = 内联回退 = Memory 组装，RDS 侧 parity 测试钉住）；
- sem.py：纯读唯一读取口（S7）· acl_groups 行级过滤 fail-closed（None/空=只见 public）·
  「无记录」与「无权限」不可区分（防存在性泄露）· 未消解回落原值标「未确认」·
  verified 优先，draft 标「估算/未验证」。
"""
import uuid

import pytest

from opensearch_pipeline.ontology.sem import UNRESOLVED_NOTE, lookup_specs
from opensearch_pipeline.ontology.store import (
    MemoryOntologyStore,
    RDSOntologyStore,
    SEM_PROJECTIONS,
)

# ── RDS 可用性探测（照 test_ontology_store 模板；另探 030 视图）─────────────────────


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
                "('ontology_object','ontology_link','ontology_ref_seq')")
            ok = cur.fetchone()[0] == 3
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name='sem_packing'")
            view_ok = cur.fetchone()[0] == 1
        conn.close()
        return ok and view_ok
    except Exception:   # noqa: BLE001
        return False


_RDS_OK = _rds_ready()
# 独立打标前缀：xdist loadgroup 按文件分 worker，本文件与 test_ontology_store 会并行
# 跑同一本地库——各扫各的标（__pys_ vs __pyt_），跨 worker 不互删在飞数据。
_MARK = "__pys_"


def _cleanup_rds(_retry: bool = True):
    import pymysql

    from opensearch_pipeline.config import get_config
    cfg = get_config()
    conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.ontology_database,
                           autocommit=True, connect_timeout=3)
    like = _MARK + "%"
    with conn.cursor() as cur:
        cur.execute("DELETE l FROM ontology_link l JOIN ontology_object o "
                    "ON o.object_id IN (l.src_object_id, l.dst_object_id) "
                    "WHERE o.title LIKE %s", (like,))
        cur.execute("DELETE FROM ontology_identifier WHERE namespace LIKE %s", (like,))
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
        pytest.skip("本地 MySQL 无 ontology 表/030 视图（先 apply schema 027-030）")
    yield RDSOntologyStore()
    _cleanup_rds_safe()


@pytest.fixture()
def mstore():
    return MemoryOntologyStore()


def _title(stem):
    return f"{_MARK}{stem}-{uuid.uuid4().hex[:6]}"


def _build_sku(store, *, with_product=True, packing=("verified",), stacking=(),
               spec_dept="pmc", spec_cls="internal", sku_cls="internal"):
    """product ← sku ← spec×N。返回 ids 便于断言。"""
    out = {"specs": []}
    sku = store.mint_object("sku", _title("龙虾杯50x20"), owner_dept="marketing",
                            golden={"规格": "50×20"}, data_classification=sku_cls)
    out["sku"] = sku
    if with_product:
        prod = store.mint_object("product", _title("6.2口径龙虾杯"), owner_dept="marketing")
        store.add_link(sku["object_id"], prod["object_id"], "sku_of_product")
        out["product"] = prod
    for state in packing:
        sp = store.mint_object("packing_spec", _title("箱规"), owner_dept=spec_dept,
                               golden={"box_type": "A=T5", "per_box": 50,
                                       "outer_dim": "60×40×50cm"},
                               data_classification=spec_cls, lifecycle_state=state)
        store.add_link(sp["object_id"], sku["object_id"], "packing_spec_of_sku")
        out["specs"].append(sp)
    for state in stacking:
        st = store.mint_object("stacking_spec", _title("香规"), owner_dept=spec_dept,
                               golden={"stack_mode": "口对口", "per_stack": 25,
                                       "container_cbm": 68},
                               data_classification=spec_cls, lifecycle_state=state)
        store.add_link(st["object_id"], sku["object_id"], "stacking_spec_of_sku")
        out["stacks"] = out.get("stacks", []) + [st]
    return out


# ── store：关系 + 投影（双后端契约）───────────────────────────────────────────────


def test_add_link_idempotent(store):
    a = store.mint_object("product", _title("甲"), owner_dept="pmc")
    b = store.mint_object("sku", _title("甲-50"), owner_dept="pmc")
    l1 = store.add_link(b["object_id"], a["object_id"], "sku_of_product")
    l2 = store.add_link(b["object_id"], a["object_id"], "sku_of_product")
    assert l1 == l2
    assert len(store.list_links(src_object_id=b["object_id"])) == 1


def test_list_links_filters(store):
    a = store.mint_object("product", _title("甲"), owner_dept="pmc")
    b = store.mint_object("sku", _title("甲-50"), owner_dept="pmc")
    c = store.mint_object("packing_spec", _title("箱规"), owner_dept="pmc")
    store.add_link(b["object_id"], a["object_id"], "sku_of_product")
    store.add_link(c["object_id"], b["object_id"], "packing_spec_of_sku")
    assert len(store.list_links(dst_object_id=b["object_id"])) == 1
    assert len(store.list_links(link_type="sku_of_product",
                                src_object_id=b["object_id"])) == 1
    assert store.list_links(dst_object_id=b["object_id"],
                            link_type="sku_of_product") == []


def test_get_object_by_ref(store):
    a = store.mint_object("mold", _title("模-1"), owner_dept="rd")
    hit = store.get_object_by_ref(a["canonical_ref"])
    assert hit and hit["object_id"] == a["object_id"]
    assert store.get_object_by_ref("FLP-P-999999") is None


def test_sem_spec_rows_shape(store):
    built = _build_sku(store, packing=("verified",), stacking=("draft",))
    rows = store.sem_spec_rows(built["sku"]["object_id"], "packing")
    assert len(rows) == 1
    r = rows[0]
    assert r["sku_ref"] == built["sku"]["canonical_ref"]
    assert r["product_name"] == built["product"]["title"]
    assert (r["box_type"], r["per_box"], r["outer_dim"]) == ("A=T5", "50", "60×40×50cm")
    assert r["spec_state"] == "verified" and r["owner_dept"] == "pmc"
    assert r["data_classification"] == "internal"
    st = store.sem_spec_rows(built["sku"]["object_id"], "stacking")[0]
    assert (st["stack_mode"], st["per_stack"], st["container_cbm"]) == ("口对口", "25", "68")
    assert st["spec_state"] == "draft"


def test_sem_spec_rows_no_product_link(store):
    built = _build_sku(store, with_product=False)
    r = store.sem_spec_rows(built["sku"]["object_id"], "packing")[0]
    assert r["product_id"] is None and r["product_name"] is None


def test_sem_spec_rows_excludes_retired(store):
    built = _build_sku(store)
    store.retire_object(built["specs"][0]["object_id"])
    assert store.sem_spec_rows(built["sku"]["object_id"], "packing") == []
    store2_target = built["sku"]["object_id"]
    store.retire_object(store2_target)
    assert store.sem_spec_rows(store2_target, "packing") == []


rds_only = pytest.mark.skipif(not _RDS_OK, reason="本地 MySQL 无 ontology 表/030 视图")


@rds_only
def test_rds_view_and_inline_parity(monkeypatch):
    """视图路径与内联回退必须逐列同形（030 视图列=回退 SQL 列，qa_facts 式降级安全）。"""
    store = RDSOntologyStore()
    try:
        built = _build_sku(store, packing=("verified", "draft"), stacking=("verified",))
        via_view = {k: store.sem_spec_rows(built["sku"]["object_id"], k)
                    for k in SEM_PROJECTIONS}
        assert len(via_view["packing"]) == 2   # 视图确实在服务（探测为真）
        monkeypatch.setattr(RDSOntologyStore, "_sem_view_ready", lambda self, v: False)
        via_inline = {k: store.sem_spec_rows(built["sku"]["object_id"], k)
                      for k in SEM_PROJECTIONS}
        for kind in SEM_PROJECTIONS:
            key = lambda r: r["spec_id"]   # noqa: E731
            assert sorted(via_view[kind], key=key) == sorted(via_inline[kind], key=key)
    finally:
        _cleanup_rds_safe()


# ── sem.py 服务层（memory；行过滤/回落/挑行规则）──────────────────────────────────


def test_lookup_by_object_id_happy(mstore):
    built = _build_sku(mstore, packing=("verified",), stacking=("verified",))
    ans = lookup_specs(mstore, built["sku"]["object_id"], acl_groups=("pmc", "marketing"))
    assert ans.resolved and ans.sku["canonical_ref"] == built["sku"]["canonical_ref"]
    assert ans.specs["packing"]["per_box"] == "50"
    assert ans.specs["stacking"]["stack_mode"] == "口对口"
    assert ans.spec_counts == {"packing": 1, "stacking": 1}
    assert ans.notes == []


def test_lookup_verified_beats_draft(mstore):
    built = _build_sku(mstore, packing=("draft", "verified", "draft"))
    ans = lookup_specs(mstore, built["sku"]["object_id"], acl_groups=("pmc", "marketing"),
                       domains=("packing",))
    assert ans.specs["packing"]["spec_state"] == "verified"
    assert ans.spec_counts["packing"] == 3
    assert not any("draft" in n for n in ans.notes)


def test_lookup_draft_only_notes_estimate(mstore):
    built = _build_sku(mstore, packing=("draft",))
    ans = lookup_specs(mstore, built["sku"]["object_id"], acl_groups=("pmc", "marketing"),
                       domains=("packing",))
    assert ans.specs["packing"]["spec_state"] == "draft"
    assert any("打样" in n for n in ans.notes)


def test_lookup_by_canonical_ref_and_raw_alias(mstore):
    built = _build_sku(mstore)
    ref = built["sku"]["canonical_ref"]
    grp = ("pmc", "marketing")
    assert lookup_specs(mstore, ref, acl_groups=grp).resolved
    assert lookup_specs(mstore, ref.lower(), acl_groups=grp).resolved   # 大小写宽容
    mstore.insert_identifier("u8", "LX620-50", "LX620-50", built["sku"]["object_id"],
                             method="manual", confidence=1.0)
    ans = lookup_specs(mstore, "lx620-50", namespace="u8", acl_groups=grp)
    assert ans.resolved and ans.specs["packing"] is not None


def test_unresolved_falls_back_to_raw(mstore):
    ans = lookup_specs(mstore, "神秘编号-9", namespace="u8", acl_groups=("pmc",))
    assert not ans.resolved and ans.raw_input == "神秘编号-9"
    assert UNRESOLVED_NOTE in ans.notes and ans.specs == {}


def test_raw_without_namespace_needs_ns(mstore):
    ans = lookup_specs(mstore, "LX620-50", acl_groups=("pmc",))
    assert not ans.resolved and any("namespace" in n for n in ans.notes)


def test_acl_row_filter_fail_closed(mstore):
    built = _build_sku(mstore, spec_cls="internal", spec_dept="pmc")
    sku_id = built["sku"]["object_id"]
    # PR-B 对象门（P0-02 验收④）：连 SKU 本体都不可读（internal, owner=marketing）→
    # 整体未消解，零对象字段出参——不再是"resolved 但 spec None"
    for groups in (None, ()):
        ans = lookup_specs(mstore, sku_id, acl_groups=groups, domains=("packing",))
        assert not ans.resolved and ans.sku is None
        assert UNRESOLVED_NOTE in ans.notes
    # SKU 可读（marketing）但 spec 行不可读（pmc internal）→ resolved + spec None（行过滤）
    ans = lookup_specs(mstore, sku_id, acl_groups=("marketing",), domains=("packing",))
    assert ans.resolved and ans.specs["packing"] is None
    assert ans.spec_counts["packing"] == 0
    assert lookup_specs(mstore, sku_id, acl_groups=("pmc", "marketing"),
                        domains=("packing",)).specs["packing"] is not None
    assert lookup_specs(mstore, sku_id, acl_groups=None, bypass_acl=True,
                        domains=("packing",)).specs["packing"] is not None


def test_public_spec_visible_to_all(mstore):
    built = _build_sku(mstore, spec_cls="public", sku_cls="public")
    ans = lookup_specs(mstore, built["sku"]["object_id"], acl_groups=(),
                       domains=("packing",))
    assert ans.resolved and ans.specs["packing"] is not None


def test_missing_and_denied_indistinguishable():
    s1, s2 = MemoryOntologyStore(), MemoryOntologyStore()
    no_spec = _build_sku(s1, packing=())
    denied = _build_sku(s2, packing=("verified",), spec_dept="pmc")
    a1 = lookup_specs(s1, no_spec["sku"]["object_id"], acl_groups=("marketing",),
                      domains=("packing",))
    a2 = lookup_specs(s2, denied["sku"]["object_id"], acl_groups=("marketing",),
                      domains=("packing",))
    assert a1.specs == a2.specs == {"packing": None}
    assert a1.notes == a2.notes                     # 防存在性泄露：文案逐字一致
    assert a1.spec_counts == a2.spec_counts == {"packing": 0}


def test_product_target_notes_non_sku(mstore):
    built = _build_sku(mstore)
    ans = lookup_specs(mstore, built["product"]["object_id"], acl_groups=("marketing",))
    assert ans.resolved and ans.specs == {}
    assert any("非 SKU" in n for n in ans.notes)


def test_confidential_sku_object_gate(mstore):
    """PR-B（P0-02 验收④）：confidential SKU 对无权调用方整体不可见——不再返回
    掩码标题+代理号（ID/ref 本身即存在性泄露，外审否决"无害代理号"论）。"""
    built = _build_sku(mstore, sku_cls="confidential", spec_cls="public")
    ans = lookup_specs(mstore, built["sku"]["object_id"], acl_groups=(),
                       domains=("packing",))
    assert not ans.resolved and ans.sku is None
    assert UNRESOLVED_NOTE in ans.notes and ans.specs == {}
    # 归属部门可读：全字段出参
    ok = lookup_specs(mstore, built["sku"]["object_id"], acl_groups=("marketing",),
                      domains=("packing",))
    assert ok.resolved and ok.sku["canonical_ref"] == built["sku"]["canonical_ref"]
    assert ok.specs["packing"] is not None


def test_unknown_domain_raises(mstore):
    built = _build_sku(mstore)
    with pytest.raises(ValueError, match="未知投影域"):
        lookup_specs(mstore, built["sku"]["object_id"], acl_groups=("pmc",),
                     domains=("packing", "bom"))


def test_retired_object_falls_back_unresolved(mstore):
    built = _build_sku(mstore)
    mstore.retire_object(built["sku"]["object_id"])
    ans = lookup_specs(mstore, built["sku"]["object_id"], acl_groups=("pmc",))
    assert not ans.resolved and UNRESOLVED_NOTE in ans.notes
