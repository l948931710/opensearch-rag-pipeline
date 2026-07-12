# -*- coding: utf-8 -*-
"""P1-10 / P1-12（2026-07-11 外评收口）——provenance fail-closed + coverage 分母可靠性。

- P1-10：golden 写路径（mint / mint_with_alias / update_golden）缺 provenance 即拒；
  值变更必须同步刷新溯源、删值剪除溯源行；sem 读出口透出 source/version/as_of；
- P1-12：seeding population 分母=全量源计数（limit 只截处理不截计数）；
  coverage 超分母不再 min() 截顶——原样透出 + anomaly 显式标注。

Memory 后端为主（校验逻辑是双后端共享的纯前置检查；RDS 侧 merge/CAS 语义由
test_ontology_store.py::test_golden_provenance_write_and_merge 双后端契约钉住）。
"""
import json

import pytest

from opensearch_pipeline.ontology.seeding import SeedRecord, seed_snapshot
from opensearch_pipeline.ontology.sem import lookup_specs
from opensearch_pipeline.ontology.store import MemoryOntologyStore

_PROV = {"sor_system": "snapshot:seeding", "source_key": "SRC-1",
         "as_of": "2026-07-11T00:00:00+00:00", "recorded_by": "seeding"}


@pytest.fixture()
def store():
    return MemoryOntologyStore()


class _ListSource:
    def __init__(self, records):
        self._records = records

    def iter_records(self):
        return iter(self._records)


def _rec(raw, title="6.2口径龙虾杯", ns="u8"):
    return SeedRecord(namespace=ns, raw_code=raw, object_type="product", title=title,
                      owner_dept="pmc", attrs={"材质": "PP"})


# ── P1-10：golden 写路径 fail-closed ───────────────────────────────────────────────


def test_mint_requires_provenance_for_golden(store):
    with pytest.raises(ValueError, match="provenance"):
        store.mint_object("product", "无溯源品", owner_dept="pmc",
                          golden={"口径": "6.2"}, _caller="test")
    assert store.find_objects("product") == []                    # 零副作用
    # 空 golden 无需溯源；显式逃生口放行；全覆盖溯源放行
    store.mint_object("product", "空黄金", owner_dept="pmc", _caller="test")
    store.mint_object("product", "显式无源", owner_dept="pmc", golden={"口径": "6.2"},
                      allow_missing_provenance=True, _caller="test")
    store.mint_object("product", "带溯源", owner_dept="pmc", golden={"口径": "6.2"},
                      provenance={"口径": dict(_PROV)}, _caller="test")


def test_mint_rejects_partial_provenance(store):
    """溯源必须覆盖**每个** golden 属性——半覆盖照拒。"""
    with pytest.raises(ValueError, match="克重"):
        store.mint_object("product", "半溯源品", owner_dept="pmc",
                          golden={"口径": "6.2", "克重": "12g"},
                          provenance={"口径": dict(_PROV)}, _caller="test")


def test_mint_with_alias_requires_provenance(store):
    with pytest.raises(ValueError, match="provenance"):
        store.mint_object_with_alias("product", "无溯源品", owner_dept="pmc",
                                     namespace="u8", raw_value="a", norm_value="A",
                                     golden={"口径": "6.2"})
    assert store.get_active_identifier("u8", "A") is None         # 整体零副作用


def test_update_golden_changed_value_requires_fresh_provenance(store):
    obj = store.mint_object("product", "改值品", owner_dept="pmc",
                            golden={"口径": "6.2"}, provenance={"口径": dict(_PROV)},
                            _caller="test")
    # 改值不带溯源 → 拒（新值挂旧来源=溯源造假）
    with pytest.raises(ValueError, match="口径"):
        store.update_golden(obj["object_id"], {"口径": "7.0"}, expected_version=1)
    assert store.get_object(obj["object_id"])["version"] == 1     # 拒后零副作用
    # 带新溯源 → 过，且溯源行被刷新
    new_stamp = dict(_PROV, as_of="2026-07-11T09:00:00+00:00", recorded_by="steward_1")
    assert store.update_golden(obj["object_id"], {"口径": "7.0"}, expected_version=1,
                               provenance={"口径": new_stamp}) is True
    prov = json.loads(store.get_object(obj["object_id"])["golden_provenance_json"])
    assert prov["口径"]["recorded_by"] == "steward_1"


def test_update_golden_unchanged_values_need_no_provenance(store):
    """没改值的重写（幂等写/仅版本推进）不强制溯源——闸只咬「变更」。"""
    obj = store.mint_object("product", "未变品", owner_dept="pmc",
                            golden={"口径": "6.2"}, provenance={"口径": dict(_PROV)},
                            _caller="test")
    assert store.update_golden(obj["object_id"], {"口径": "6.2"}, expected_version=1) is True
    prov = json.loads(store.get_object(obj["object_id"])["golden_provenance_json"])
    assert prov["口径"]["sor_system"] == "snapshot:seeding"       # 旧溯源原样保留


def test_update_golden_removed_attr_prunes_provenance_row(store):
    """删属性 → 溯源行同步剪除（不留「有来源没有值」的陈旧行）。"""
    obj = store.mint_object("product", "删值品", owner_dept="pmc",
                            golden={"口径": "6.2", "克重": "12g"},
                            provenance={"口径": dict(_PROV), "克重": dict(_PROV)},
                            _caller="test")
    assert store.update_golden(obj["object_id"], {"口径": "6.2"}, expected_version=1) is True
    prov = json.loads(store.get_object(obj["object_id"])["golden_provenance_json"])
    assert "克重" not in prov and "口径" in prov


def test_update_golden_cas_loser_still_returns_false(store):
    """CAS 语义先于溯源闸：版本已过期 → 直接 False（不误报 provenance 错误）。"""
    obj = store.mint_object("product", "竞态品", owner_dept="pmc",
                            golden={"口径": "6.2"}, provenance={"口径": dict(_PROV)},
                            _caller="test")
    assert store.update_golden(obj["object_id"], {"口径": "7.0"}, expected_version=99) is False


# ── P1-10：sem 读出口 source/version/as_of ────────────────────────────────────────


def _build_sku_with_spec(store, *, spec_provenance):
    sku = store.mint_object("sku", "龙虾杯50x20", owner_dept="pmc", _caller="test")
    spec = store.mint_object("packing_spec", "箱规", owner_dept="pmc",
                             golden={"box_type": "A=T5", "per_box": 50,
                                     "outer_dim": "60×40×50cm"},
                             provenance=spec_provenance,
                             allow_missing_provenance=(spec_provenance is None),
                             lifecycle_state="verified", _caller="test")
    store.add_link(spec["object_id"], sku["object_id"], "packing_spec_of_sku",
                   _caller="test")
    return sku, spec


def test_sem_spec_carries_source_version_as_of(store):
    stamps = {f: dict(_PROV, as_of=f"2026-07-1{i}T00:00:00+00:00")
              for i, f in enumerate(("box_type", "per_box", "outer_dim"))}
    sku, _spec = _build_sku_with_spec(store, spec_provenance=stamps)
    ans = lookup_specs(store, sku["object_id"], acl_groups={"pmc"}, domains=("packing",))
    row = ans.specs["packing"]
    assert row is not None
    assert row["source"] == ["snapshot:seeding"]                  # 去重来源列表
    assert row["version"] == 1                                    # 对象乐观锁版本
    assert row["as_of"] == "2026-07-12T00:00:00+00:00"            # 溯源戳最新时点
    assert row["per_box"] == "50"                                 # 原投影字段不受富化影响


def test_sem_spec_without_provenance_degrades_to_none(store):
    """溯源缺失（历史行/显式无源）→ 三字段置 None，不编造，不拦答案。"""
    sku, _spec = _build_sku_with_spec(store, spec_provenance=None)
    ans = lookup_specs(store, sku["object_id"], acl_groups={"pmc"}, domains=("packing",))
    row = ans.specs["packing"]
    assert row is not None
    assert row["source"] is None and row["as_of"] is None
    assert row["version"] == 1


# ── P1-12：population 分母 + anomaly ──────────────────────────────────────────────


def test_seed_limit_does_not_shrink_population_denominator(store):
    """limit=2 只处理 2 条，但 population 分母登记全量 5 条（此前=2 → 覆盖率虚高）。"""
    src = _ListSource([_rec(f"C{i}", title=f"品{i}") for i in range(5)])
    rep = seed_snapshot(store, src, dry_run=False, limit=2)
    assert rep.records == 2 and rep.minted == 2
    assert any(a.get("action") == "limit_reached" for a in rep.actions)
    assert store.population_total() == 5
    cov = store.coverage()
    assert cov["denominator"] == "population" and cov["population_records"] == 5
    assert cov["resolution_coverage"] == pytest.approx(2 / 5)
    assert cov["anomaly"] is None


def test_coverage_over_population_flags_anomaly_not_clamped(store):
    """active > population（快照陈旧/源收缩）→ 覆盖率原样 >1.0 + anomaly 标注，
    绝不 min(1.0,·) 掩成 100%。"""
    for i, raw in enumerate(("A1", "A2")):
        obj = store.mint_object("product", f"品{i}", owner_dept="pmc", _caller="test")
        store.insert_identifier("u8", raw, raw, obj["object_id"], method="seed",
                                confirmed_by="auto", _caller="test")
    store.record_population_snapshot({"u8": 1}, source="seeding")   # 人为陈旧分母
    cov = store.coverage()
    assert cov["resolution_coverage"] == pytest.approx(2.0)
    assert cov["anomaly"] == "coverage_exceeds_population"
    assert cov["denominator"] == "population"


def test_coverage_approx_keeps_anomaly_none(store):
    obj = store.mint_object("product", "品", owner_dept="pmc", _caller="test")
    store.insert_identifier("u8", "b1", "B1", obj["object_id"], method="seed",
                            _caller="test")
    cov = store.coverage()
    assert cov["denominator"] == "approx" and cov["anomaly"] is None
