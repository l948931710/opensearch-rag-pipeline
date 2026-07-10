# -*- coding: utf-8 -*-
"""ontology/seeding.py + scripts/ontology_seed.py —— 播种管线（PR5）。

锁死的决策纪律：已激活跳过（幂等续跑）· 同名同属性唯一才 auto（同物多号愈合路径）·
属性冲突/改模后缀/多候选一律入 case 不铸新对象（防重复品繁殖+改模恒人审）·
dry-run 零写库且批内仿真与真跑同账。
"""
import io
import sys
from pathlib import Path

import pytest

from opensearch_pipeline.ontology.seeding import (
    CsvSnapshotSource,
    SeedRecord,
    U8SnapshotSource,
    seed_snapshot,
)
from opensearch_pipeline.ontology.store import MemoryOntologyStore

FIXTURE = Path(__file__).parent / "fixtures" / "ontology_seed_sample.csv"


class _ListSource:
    def __init__(self, records):
        self._records = records

    def iter_records(self):
        return iter(self._records)


def _rec(raw, title="6.2口径龙虾杯", ns="u8", otype="product", attrs=None, dept="pmc"):
    return SeedRecord(namespace=ns, raw_code=raw, object_type=otype, title=title,
                      owner_dept=dept, attrs=dict(attrs or {"材质": "PP"}))


@pytest.fixture()
def store():
    return MemoryOntologyStore()


# ── 决策分支 ─────────────────────────────────────────────────────────────────────


def test_mint_new_object_with_canonical_alias(store):
    rep = seed_snapshot(store, _ListSource([_rec("LX620-50")]), dry_run=False)
    assert (rep.minted, rep.auto_aliased, rep.cases_opened, rep.errors) == (1, 0, 0, 0)
    objs = store.find_objects("product")
    assert len(objs) == 1 and objs[0]["title"] == "6.2口径龙虾杯"
    alias = store.get_active_identifier("u8", "LX620-50")
    assert alias["relation"] == "canonical" and alias["resolution_method"] == "seed"
    assert alias["confirmed_by"] == "auto" and float(alias["confidence"]) == 1.0


def test_rerun_is_idempotent(store):
    src = [_rec("LX620-50")]
    seed_snapshot(store, _ListSource(src), dry_run=False)
    rep2 = seed_snapshot(store, _ListSource(src), dry_run=False)
    assert rep2.skipped_active == 1 and rep2.minted == 0
    assert len(store.find_objects("product")) == 1
    assert len(store.list_identifiers_for_target(
        store.find_objects("product")[0]["object_id"])) == 1


def test_same_title_same_attrs_auto_alias(store):
    """同物多货号的愈合路径：第二个码 auto 别名到第一个对象，不铸重复品。"""
    rep = seed_snapshot(store, _ListSource([_rec("LX620-50"), _rec("LX620-ALT")]),
                        dry_run=False)
    assert (rep.minted, rep.auto_aliased) == (1, 1)
    objs = store.find_objects("product")
    assert len(objs) == 1
    alias = store.get_active_identifier("u8", "LX620-ALT")
    assert alias["target_object_id"] == objs[0]["object_id"]
    assert alias["resolution_method"] == "rule"
    assert float(alias["confidence"]) == pytest.approx(0.96)
    assert alias["confirmed_by"] == "auto"           # 入抽检队列的标


def test_attr_conflict_goes_to_case(store):
    """同名但属性矛盾（PP vs PS）→ 压 HITL：不 auto、不铸新对象。"""
    rep = seed_snapshot(store, _ListSource([
        _rec("SQ370", title="PP水杯370ml", attrs={"材质": "PP"}),
        _rec("SQ370X", title="PP水杯370ml", attrs={"材质": "PS"}),
    ]), dry_run=False)
    assert (rep.minted, rep.auto_aliased, rep.cases_opened) == (1, 0, 1)
    assert len(store.find_objects("product")) == 1
    assert store.get_active_identifier("u8", "SQ370X") is None
    cases = store.list_open_cases(namespace="u8")
    assert len(cases) == 1 and cases[0]["norm_value"] == "SQ370X"
    cands = store.list_candidates(cases[0]["case_id"])
    assert len(cands) == 1 and float(cands[0]["confidence"]) == pytest.approx(0.80)
    assert cands[0]["features_json"] and "attr_conflict" in cands[0]["features_json"]


def test_revision_suffix_always_case_never_auto(store):
    rep = seed_snapshot(store, _ListSource([
        _rec("LX620-50"),
        _rec("LX620-50-M", title="6.2口径龙虾杯改模"),
    ]), dry_run=False)
    assert (rep.minted, rep.auto_aliased, rep.cases_opened) == (1, 0, 1)
    assert store.get_active_identifier("u8", "LX620-50-M") is None   # 改模恒人审
    case = store.list_open_cases(namespace="u8")[0]
    cand = store.list_candidates(case["case_id"])[0]
    assert float(cand["confidence"]) == pytest.approx(0.85)
    assert "strip_suffix" in (cand["features_json"] or "")


def test_suffix_without_base_is_plain_mint(store):
    rep = seed_snapshot(store, _ListSource([_rec("ZZZ-W", title="独立新品W")]), dry_run=False)
    assert rep.minted == 1 and rep.cases_opened == 0


def test_multi_title_match_denied_auto(store):
    """库里已有两个同名对象（历史重复品）→ 多候选 → case，绝不 auto 挑一个。"""
    store.mint_object("product", "PP水杯370ml", owner_dept="pmc", golden={"材质": "PP"})
    store.mint_object("product", "PP水杯370ml", owner_dept="pmc", golden={"材质": "PP"})
    rep = seed_snapshot(store, _ListSource([
        _rec("SQ370", title="PP水杯370ml", attrs={"材质": "PP"})]), dry_run=False)
    assert (rep.minted, rep.auto_aliased, rep.cases_opened) == (0, 0, 1)
    case = store.list_open_cases(namespace="u8")[0]
    assert len(store.list_candidates(case["case_id"])) == 2


def test_crash_resume_self_heals_orphan_object(store):
    """崩溃在铸对象后、落别名前：重跑经同名聚类 auto 补上别名，不再铸第二个对象。"""
    store.mint_object("product", "6.2口径龙虾杯", owner_dept="pmc", golden={"材质": "PP"})
    rep = seed_snapshot(store, _ListSource([_rec("LX620-50")]), dry_run=False)
    assert (rep.minted, rep.auto_aliased) == (0, 1)
    assert len(store.find_objects("product")) == 1


def test_case_dedupes_within_batch(store):
    """同一未解析码在批内出现两次 → case 只计 1（真跑 upsert 聚合 seen_count）。"""
    rep = seed_snapshot(store, _ListSource([
        _rec("LX620-50"),
        _rec("LX620-50-M", title="改模甲"),
        _rec("LX620-50-M", title="改模甲"),
    ]), dry_run=False)
    assert rep.cases_opened == 1
    assert store.list_open_cases(namespace="u8")[0]["seen_count"] == 2


def test_error_isolated_per_record(store):
    bad = SeedRecord(namespace="  ", raw_code="X", object_type="product",
                     title="坏行", owner_dept="pmc")
    rep = seed_snapshot(store, _ListSource([_rec("A1", title="甲"), bad,
                                            _rec("B2", title="乙")]), dry_run=False)
    assert rep.errors == 1 and rep.minted == 2


def test_limit_stops_early(store):
    rep = seed_snapshot(store, _ListSource([_rec(f"C{i}", title=f"品{i}") for i in range(5)]),
                        dry_run=False, limit=2)
    assert rep.records == 2 and rep.minted == 2
    assert any(a.get("action") == "limit_reached" for a in rep.actions)


# ── dry-run：零写库 + 批内仿真同账 ────────────────────────────────────────────────

_BATCH = [
    _rec("LX620-50"),                                             # mint
    _rec("LX620-ALT"),                                            # auto（批内同名）
    _rec("LX620-50-M", title="6.2口径龙虾杯改模"),                  # case（后缀，批内基础码）
    _rec("SQ370", title="PP水杯370ml", attrs={"材质": "PP"}),      # mint
    _rec("SQ370X", title="PP水杯370ml", attrs={"材质": "PS"}),     # case（属性冲突）
    _rec("pp-1100", title="PP-1100", ns="material_grade", otype="material", attrs={}),  # mint
]


def test_dry_run_writes_nothing_but_simulates_batch(store):
    rep = seed_snapshot(store, _ListSource(_BATCH), dry_run=True)
    assert (rep.minted, rep.auto_aliased, rep.cases_opened) == (3, 1, 2)
    assert rep.dry_run and "DRY-RUN" in rep.summary()
    # 零写库
    assert store.find_objects("product") == [] and store.find_objects("material") == []
    assert store.list_open_cases() == []
    assert store.coverage()["active_identifiers"] == 0


def test_dry_run_matches_real_run_counts():
    dry = seed_snapshot(MemoryOntologyStore(), _ListSource(_BATCH), dry_run=True)
    real = seed_snapshot(MemoryOntologyStore(), _ListSource(_BATCH), dry_run=False)
    for k in ("records", "minted", "auto_aliased", "cases_opened", "skipped_active", "errors"):
        assert getattr(dry, k) == getattr(real, k), k


# ── 数据源 ───────────────────────────────────────────────────────────────────────


def test_csv_source_parses_fixture():
    records = list(CsvSnapshotSource(str(FIXTURE)).iter_records())
    assert len(records) == 6
    first = records[0]
    assert first.namespace == "u8" and first.raw_code == "LX620-50"
    assert first.attrs == {"材质": "PP", "克重": "12.5"}       # 非固定列全进 attrs
    assert records[5].object_type == "material" and records[5].attrs == {}
    assert all(r.data_classification == "internal" for r in records)


def test_csv_source_missing_column_raises(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("namespace,raw_code\nu8,X1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必需列"):
        list(CsvSnapshotSource(str(p)).iter_records())


def test_csv_source_skips_blank_raw(tmp_path):
    p = tmp_path / "blank.csv"
    p.write_text("namespace,raw_code,object_type,title,owner_dept\n"
                 "u8,,product,空码,pmc\nu8,A1,product,甲,pmc\n", encoding="utf-8")
    assert len(list(CsvSnapshotSource(str(p)).iter_records())) == 1


def test_fixture_end_to_end_totals(store):
    rep = seed_snapshot(store, CsvSnapshotSource(str(FIXTURE)), dry_run=False)
    assert (rep.minted, rep.auto_aliased, rep.cases_opened, rep.errors) == (3, 1, 2, 0)


def test_u8_source_is_contract_stub():
    with pytest.raises(NotImplementedError, match="go/no-go"):
        list(U8SnapshotSource().iter_records())


# ── CLI ──────────────────────────────────────────────────────────────────────────


def _run_cli(argv, **kw):
    from scripts.ontology_seed import main
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        code = main(argv, **kw)
    finally:
        sys.stdout = old
    return code, out.getvalue()


def test_cli_dry_run_default(store):
    code, out = _run_cli([str(FIXTURE)], store=store)
    assert code == 0
    assert "DRY-RUN" in out and "--commit" in out
    assert store.find_objects("product") == []               # 缺省不写库


def test_cli_commit_writes(store):
    code, out = _run_cli([str(FIXTURE), "--commit"], store=store)
    assert code == 0 and "已写库" in out
    assert len(store.find_objects("product")) == 2           # 龙虾杯 + PP水杯


def test_cli_governance_seeds(store):
    code, _ = _run_cli([str(FIXTURE), "--commit", "--governance-seeds"], store=store)
    assert code == 0
    assert len(store.list_stewardship()) >= 6
    assert len(store.list_attribute_sources()) >= 6


def test_cli_refuses_prod_target(monkeypatch):
    monkeypatch.setattr("opensearch_pipeline.config.is_prod_target", lambda *a, **k: True)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    code, _ = _run_cli([str(FIXTURE), "--commit"])            # store=None → 走 prod 守卫
    assert code == 2 and "生产 RDS" in err.getvalue()


def test_cli_commit_dryrun_mutex():
    with pytest.raises(SystemExit):
        _run_cli([str(FIXTURE), "--commit", "--dry-run"])
