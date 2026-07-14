# -*- coding: utf-8 -*-
"""
test_ontology_governance_batch6.py — 复核批次6 C2 回归锁

C2 归一化感知召回：normalized_title 等值主臂（空白/全半角变体 LIKE 漏召回=false-mint
    根源）· LIKE 辅臂兜 backfill 缺口（WARNING 可观测）· 截断打满 WARNING ·
    播种路径 would-auto 仿真（S9 resolver 硬闸测不到的盲区）+ backtest seed 臂。
（C3 先 ACL 后截断的回归锁在 test_ontology_acl_matrix.py；find_objects norm_title
 双后端等值在 test_ontology_store.py。）
"""
import logging

import pytest

from opensearch_pipeline.ontology.normalize import title_key
from opensearch_pipeline.ontology.seeding import (
    SeedRecord,
    SeedReport,
    _Sink,
    simulate_seed_decision,
)
from opensearch_pipeline.ontology.store import MemoryOntologyStore


def _rec(raw, title, ns="u8", otype="product", attrs=None):
    return SeedRecord(namespace=ns, raw_code=raw, object_type=otype, title=title,
                      owner_dept="pmc", attrs=dict(attrs or {}))


def _mint(store, title, otype="product"):
    return store.mint_object(otype, title, owner_dept="pmc", _caller="test")


# ═══════════════════════════════════════════════════════════════
# title_key / 归一召回
# ═══════════════════════════════════════════════════════════════


def test_title_key_collapses_whitespace_and_width_variants():
    assert title_key("PP 龙虾杯") == title_key("PP龙虾杯") == title_key("PP　龙虾杯")
    assert title_key("ＰＰ龙虾杯") == title_key("PP龙虾杯")     # 全角字母 → 半角
    assert title_key("pp龙虾杯") != title_key("PP龙虾杯")       # 大小写原样（铁律 3）


def test_title_matches_recalls_whitespace_variant():
    """"PP龙虾杯" 不是 "PP 龙虾杯" 的子串——LIKE 臂必 miss，归一臂必须召回。"""
    store = MemoryOntologyStore()
    obj = _mint(store, "PP 龙虾杯")
    sink = _Sink(store, True, SeedReport())
    hits = sink.title_matches("product", "PP龙虾杯")
    assert [h["object_id"] for h in hits] == [obj["object_id"]]


class _LegacyStore(MemoryOntologyStore):
    """模拟 038 backfill 前的存量库：normalized_title 为 NULL 且等值臂不做回退计算。"""

    def mint_object(self, *a, **kw):
        obj = super().mint_object(*a, **kw)
        self._objects[obj["object_id"]]["normalized_title"] = None
        return obj

    def find_objects(self, object_type, *, title_like=None, norm_title=None,
                     status="active", limit=50):
        if norm_title is not None:
            # 对齐 RDS 语义：NULL 行不参与等值匹配（不做 Python 侧回退）
            with self._lock:
                out = [dict(o) for o in self._objects.values()
                       if o["object_type"] == object_type and o["status"] == status
                       and o.get("normalized_title") == norm_title]
                return sorted(out, key=lambda o: o["canonical_ref"])[:limit]
        return super().find_objects(object_type, title_like=title_like,
                                    status=status, limit=limit)


def test_title_matches_legacy_arm_covers_backfill_gap_with_warning(caplog):
    """归一臂 miss（存量 NULL）而 LIKE 辅臂命中 → 仍召回 + WARNING 报 backfill 缺口。"""
    store = _LegacyStore()
    obj = _mint(store, "白盒杯")
    sink = _Sink(store, True, SeedReport())
    with caplog.at_level(logging.WARNING, logger="opensearch_pipeline.ontology.seeding"):
        hits = sink.title_matches("product", "白盒杯")
    assert [h["object_id"] for h in hits] == [obj["object_id"]]
    assert any("backfill 缺口" in r.message for r in caplog.records)


def test_title_matches_truncation_warning(monkeypatch, caplog):
    from opensearch_pipeline.ontology import seeding as seeding_mod
    monkeypatch.setattr(seeding_mod, "_TITLE_MATCH_LIMIT", 2)
    store = MemoryOntologyStore()
    for i in range(3):
        _mint(store, f"同名 杯{'　' * i}")        # 三个归一同键对象（空白变体）
    sink = _Sink(store, True, SeedReport())
    with caplog.at_level(logging.WARNING, logger="opensearch_pipeline.ontology.seeding"):
        sink.title_matches("product", "同名杯")
    assert any("打满 LIMIT" in r.message for r in caplog.records)


# ═══════════════════════════════════════════════════════════════
# 播种路径 would-auto 仿真（纯读）
# ═══════════════════════════════════════════════════════════════


def test_simulate_skip_active():
    store = MemoryOntologyStore()
    obj = _mint(store, "龙虾杯")
    store.insert_identifier("u8", "P-001", "P-001", obj["object_id"],
                            method="manual", _caller="test")
    sim = simulate_seed_decision(store, _rec("P-001", "龙虾杯"))
    assert sim["action"] == "skip_active" and sim["target"] == obj["object_id"]


def test_simulate_would_auto_on_unique_title_match():
    store = MemoryOntologyStore()
    obj = _mint(store, "龙虾杯")
    sim = simulate_seed_decision(store, _rec("P-002", "龙虾 杯"))   # 空白变体也该聚上
    assert sim["action"] == "auto_alias"
    assert sim["target"] == obj["object_id"]
    assert sim["candidates"] == 1
    # 纯读：仿真不落别名
    assert store.get_active_identifier("u8", "P-002") is None


def test_simulate_multi_candidate_goes_case():
    store = MemoryOntologyStore()
    _mint(store, "方形杯")
    _mint(store, "方形 杯")           # 归一同键的第二个对象 → 多候选恒人审
    sim = simulate_seed_decision(store, _rec("P-003", "方形杯"))
    assert sim["action"] == "case" and sim["candidates"] == 2


def test_simulate_no_candidate_mints():
    sim = simulate_seed_decision(MemoryOntologyStore(), _rec("P-004", "全新品名"))
    assert sim["action"] == "mint" and sim["candidates"] == 0


# ═══════════════════════════════════════════════════════════════
# backtest seed 臂
# ═══════════════════════════════════════════════════════════════


def test_run_seed_backtest_metrics_and_false_merge():
    from scripts.ontology_backtest import run_seed_backtest
    store = MemoryOntologyStore()
    obj = _mint(store, "白盒杯")
    ref = obj["canonical_ref"].upper()
    rows = [
        {"namespace": "u8", "raw_code": "A1", "expected_ref": ref,
         "title": "白盒杯", "object_type": "product", "owner_dept": "pmc"},     # auto_correct
        {"namespace": "u8", "raw_code": "A2", "expected_ref": "FLP-P-999999",
         "title": "白 盒杯", "object_type": "product", "owner_dept": "pmc"},    # auto_wrong（播种侧误合并）
        {"namespace": "u8", "raw_code": "A3", "expected_ref": "",
         "title": "", "object_type": "", "owner_dept": ""},                     # 缺列 skip
        {"namespace": "u8", "raw_code": "A4", "expected_ref": "",
         "title": "全新品", "object_type": "product", "owner_dept": "pmc"},     # mint
    ]
    rep = run_seed_backtest(store, rows)
    m = rep["metrics"]
    assert m["evaluated"] == 3 and m["skipped_no_title"] == 1
    assert m["would_auto"] == 2
    assert m["auto_correct"] == 1 and m["auto_wrong"] == 1
    assert m["action_mint"] == 1
    assert m["auto_wrong_rate"] == pytest.approx(round(1 / 3, 4))
