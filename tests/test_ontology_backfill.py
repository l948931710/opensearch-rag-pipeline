# -*- coding: utf-8 -*-
"""ontology/backfill.py + case 愈合（seeding 核心扩展）—— 回填 worker（PR9）。

锁死的决策纪律：
- mention 观测语义（默认）：无候选**不铸新对象**，入 case 聚合观测（seen_count 单调涨）；
  --mode master 才回播种语义；
- 愈合对账：active 别名 × open case 尸案 → resolved by='auto' 闭环（skip 路径与
  alias 落成路径两处；alias 落成的 identifier 溯源挂 source_case_id）；
- auto 资格仍唯一走 may_auto_activate（多候选/改模后缀恒 case，模式无关）；
- dry-run 零写库且计数（含 cases_healed）与真跑同账；
- CLI：--dry-run 默认；--commit 需 RAG_ONTOLOGY_BACKFILL_ENABLE=true 第二道闸。
"""
import io
import sys
from pathlib import Path

import pytest

from opensearch_pipeline.ontology.backfill import ENABLE_ENV, backfill_snapshot, main
from opensearch_pipeline.ontology.seeding import SeedRecord, seed_snapshot
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



@pytest.fixture(autouse=True)
def _auto_ack_for_auto_machinery_tests(monkeypatch):
    """PR-E（P0-07）：auto 默认硬关（候选-only）——本文件测的是 auto 机制本身，
    统一注入有效当日 ack；硬关默认态的独立断言见 test_auto_gate_* 系列。"""
    from datetime import date
    monkeypatch.setenv("RAG_ONTOLOGY_AUTO_ACK",
                       f"test:{date.today().isoformat()}:deadbeefcafe")

@pytest.fixture()
def store():
    return MemoryOntologyStore()


# ── mention 观测语义 ─────────────────────────────────────────────────────────────


def test_mention_mode_never_mints(store):
    """无候选的观测 → 不铸对象，入 case（0 候选，工作台手动指定/等主数据）。"""
    rep = backfill_snapshot(store, _ListSource([_rec("OBS-1", title="流程里冒出的新码")]),
                            dry_run=False)
    assert (rep.minted, rep.auto_aliased, rep.cases_opened) == (0, 0, 1)
    assert store.find_objects("product") == []
    cases = store.list_open_cases(namespace="u8")
    assert len(cases) == 1 and cases[0]["norm_value"] == "OBS-1"
    assert store.list_candidates(cases[0]["case_id"]) == []
    assert "backfill" in (cases[0]["evidence_json"] or "")     # 证据快照可溯源到回填轮


def test_mention_reobservation_aggregates(store):
    """同一观测跨轮重现 → 聚合 seen_count，不重复建 case（幂等按 (ns,norm)）。"""
    src = [_rec("OBS-1", title="流程里冒出的新码")]
    backfill_snapshot(store, _ListSource(src), dry_run=False)
    backfill_snapshot(store, _ListSource(src), dry_run=False)
    cases = store.list_open_cases(namespace="u8")
    assert len(cases) == 1 and cases[0]["seen_count"] == 2


def test_mention_mode_still_auto_aliases_unique_high_conf(store):
    """观测语义只收铸对象权：同型同名唯一高置信照走 auto 别名（愈合同物多号）。"""
    store.mint_object("product", "6.2口径龙虾杯", owner_dept="pmc", golden={"材质": "PP"})
    rep = backfill_snapshot(store, _ListSource([_rec("LX620-ALT")]), dry_run=False)
    assert (rep.minted, rep.auto_aliased, rep.cases_opened) == (0, 1, 0)
    alias = store.get_active_identifier("u8", "LX620-ALT")
    assert alias["confirmed_by"] == "auto"


def test_mention_mode_multi_candidate_still_case(store):
    """三禁不因模式而松动：多候选恒 case。"""
    store.mint_object("product", "PP水杯370ml", owner_dept="pmc", golden={"材质": "PP"})
    store.mint_object("product", "PP水杯370ml", owner_dept="pmc", golden={"材质": "PP"})
    rep = backfill_snapshot(store, _ListSource([
        _rec("SQ370", title="PP水杯370ml")]), dry_run=False)
    assert (rep.minted, rep.auto_aliased, rep.cases_opened) == (0, 0, 1)
    case = store.list_open_cases(namespace="u8")[0]
    assert len(store.list_candidates(case["case_id"])) == 2


def test_master_mode_mints_like_seeding(store):
    rep = backfill_snapshot(store, _ListSource([_rec("LX620-50")]), dry_run=False,
                            mint_new=True)
    assert rep.minted == 1
    assert len(store.find_objects("product")) == 1


def test_seeding_default_still_mints(store):
    """回填收铸对象权不得反向污染播种默认语义。"""
    rep = seed_snapshot(store, _ListSource([_rec("LX620-50")]), dry_run=False)
    assert rep.minted == 1


# ── case 愈合对账 ────────────────────────────────────────────────────────────────


def _zombie_store():
    """尸案现场：active 别名已在，open case 还挂着（历史积压/跨路径竞态遗留）。"""
    s = MemoryOntologyStore()
    obj = s.mint_object("product", "6.2口径龙虾杯", owner_dept="pmc", golden={"材质": "PP"})
    ident_id = s.insert_identifier("u8", "LX620-50", "LX620-50", obj["object_id"],
                                   method="seed", relation="canonical",
                                   confidence=1.0, confirmed_by="auto")
    case_id = s.upsert_case("u8", "LX620-50", "LX620-50", object_type_hint="product",
                            evidence={"source": "legacy"})
    return s, ident_id, case_id


def test_heal_on_skip_active_closes_zombie_case():
    s, ident_id, case_id = _zombie_store()
    rep = backfill_snapshot(s, _ListSource([_rec("LX620-50")]), dry_run=False)
    assert (rep.skipped_active, rep.cases_healed) == (1, 1)
    case = s.get_case(case_id)
    assert case["status"] == "resolved"
    assert case["resolved_identifier_id"] == ident_id and case["resolved_by"] == "auto"
    assert s.list_open_cases() == []
    assert any(a.get("action") == "heal_case" and a.get("case") == case_id
               for a in rep.actions)
    assert "case 愈合 1" in rep.summary()


def test_heal_on_alias_landing_links_source_case(store):
    """观测先入 case，后来 auto 别名落成 → case 闭环且 identifier 溯源挂 source_case_id。"""
    store.mint_object("product", "6.2口径龙虾杯", owner_dept="pmc", golden={"材质": "PP"})
    case_id = store.upsert_case("u8", "LX620-ALT", "LX620-ALT",
                                object_type_hint="product", evidence={"source": "backfill"})
    rep = backfill_snapshot(store, _ListSource([_rec("LX620-ALT")]), dry_run=False)
    assert (rep.auto_aliased, rep.cases_healed) == (1, 1)
    alias = store.get_active_identifier("u8", "LX620-ALT")
    assert alias["source_case_id"] == case_id
    case = store.get_case(case_id)
    assert case["status"] == "resolved"
    assert case["resolved_identifier_id"] == alias["identifier_id"]


def test_heal_dry_run_counts_but_writes_nothing():
    s, _ident_id, case_id = _zombie_store()
    rep = backfill_snapshot(s, _ListSource([_rec("LX620-50")]), dry_run=True)
    assert rep.cases_healed == 1 and rep.dry_run
    assert s.get_case(case_id)["status"] == "open"      # 零写库：case 原样开着


def test_dry_real_parity_including_heals():
    """dry 与真跑同账（含批内先后效应：批内 case 被批内后到的 auto 别名愈合）。"""
    def prepped():
        s = MemoryOntologyStore()
        s.mint_object("product", "6.2口径龙虾杯", owner_dept="pmc", golden={"材质": "PP"})
        obj2 = s.mint_object("product", "PP水杯370ml", owner_dept="pmc",
                             golden={"材质": "PP"})
        s.insert_identifier("u8", "STALE-1", "STALE-1", obj2["object_id"],
                            method="manual", confidence=1.0)
        s.upsert_case("u8", "STALE-1", "STALE-1", object_type_hint="product",
                      evidence={"source": "legacy"})
        return s

    batch = [
        _rec("STALE-1", title="PP水杯370ml"),      # skip_active + 尸案愈合
        _rec("NEW-OBS", title="孤例甲"),            # mention：0 候选 → case
        _rec("K9"),                                 # auto 别名（同名唯一 0.96）
        _rec("J7", title="孤例乙"),                 # 批内 case（0 候选）
        _rec("J7"),                                 # 同 norm 后到：auto 别名 → 愈合批内 case
        _rec("K9-M", title="改模乙"),               # 改模后缀 → case 恒人审
    ]
    dry = backfill_snapshot(prepped(), _ListSource(batch), dry_run=True)
    real = backfill_snapshot(prepped(), _ListSource(batch), dry_run=False)
    for k in ("records", "minted", "auto_aliased", "cases_opened", "cases_healed",
              "skipped_active", "errors"):
        assert getattr(dry, k) == getattr(real, k), k
    assert (real.auto_aliased, real.cases_opened, real.cases_healed) == (2, 3, 2)
    assert real.minted == 0


def test_batch_internal_heal_closes_real_case():
    """真跑批内愈合：J7 先入 case 后被同批 auto 别名闭环，identifier 挂上真 case_id。"""
    store = MemoryOntologyStore()
    store.mint_object("product", "6.2口径龙虾杯", owner_dept="pmc", golden={"材质": "PP"})
    backfill_snapshot(store, _ListSource([
        _rec("J7", title="孤例乙"),
        _rec("J7"),
    ]), dry_run=False)
    assert store.list_open_cases() == []
    alias = store.get_active_identifier("u8", "J7")
    assert alias is not None and alias["source_case_id"] is not None
    assert store.get_case(alias["source_case_id"])["status"] == "resolved"


# ── CLI ──────────────────────────────────────────────────────────────────────────


def _run_cli(argv, **kw):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        code = main(argv, **kw)
    finally:
        sys.stdout = old
    return code, out.getvalue()


def test_cli_dry_run_default(store, monkeypatch):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    code, out = _run_cli([str(FIXTURE)], store=store)
    assert code == 0
    assert "DRY-RUN" in out and ENABLE_ENV in out
    assert store.find_objects("product") == [] and store.list_open_cases() == []


def test_cli_commit_requires_enable_env(store, monkeypatch):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    code, _ = _run_cli([str(FIXTURE), "--commit"], store=store)
    assert code == 2 and ENABLE_ENV in err.getvalue()
    assert store.list_open_cases() == []                     # 闸拦下=零写库


def test_cli_commit_with_gate_writes_mention(store, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "true")
    code, out = _run_cli([str(FIXTURE), "--commit"], store=store)
    assert code == 0 and "已写库" in out
    assert store.find_objects("product") == []               # mention 默认不铸
    assert len(store.list_open_cases()) > 0                  # 观测全进工作台


def test_cli_mode_master_mints(store, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "true")
    code, _ = _run_cli([str(FIXTURE), "--commit", "--mode", "master"], store=store)
    assert code == 0
    assert len(store.find_objects("product")) == 2           # 龙虾杯 + PP水杯（播种同账）


def test_cli_prints_coverage(store, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "true")
    code, out = _run_cli([str(FIXTURE), "--commit"], store=store)
    assert code == 0 and "resolution_coverage" in out


def test_cli_errors_exit_2(store):
    bad = SeedRecord(namespace="  ", raw_code="X", object_type="product",
                     title="坏行", owner_dept="pmc")
    code, _ = _run_cli([], store=store, source=_ListSource([bad]))
    assert code == 2


def test_cli_commit_dryrun_mutex(store):
    with pytest.raises(SystemExit):
        _run_cli([str(FIXTURE), "--commit", "--dry-run"], store=store)


# ── PR-E（P0-07）：backfill 生产指纹硬拒（第三道闸）──────────────────────────────


def test_backfill_commit_prod_fingerprint_rejected(tmp_path, monkeypatch):
    """--commit + ENABLE=true 但目标是生产 RDS 且无当日 PROD_ACK → exit 2 零写。"""
    from opensearch_pipeline.ontology import backfill as bf
    csv = tmp_path / "snap.csv"
    csv.write_text("namespace,raw_code,object_type,title,owner_dept\n"
                   "u8,X1,product,测试品,pmc\n", encoding="utf-8")
    monkeypatch.setenv("RAG_ONTOLOGY_BACKFILL_ENABLE", "true")
    monkeypatch.delenv("RAG_ONTOLOGY_BACKFILL_PROD_ACK", raising=False)
    monkeypatch.setattr("opensearch_pipeline.config.is_prod_target",
                        lambda kind, v: kind == "rds")
    store = MemoryOntologyStore()
    rc = bf.main([str(csv), "--commit"], store=store, source=None)
    assert rc == 2
    assert store.get_active_identifier("u8", "X1") is None      # 零写


def test_backfill_commit_prod_ack_same_day_passes_gate(tmp_path, monkeypatch):
    from datetime import date

    from opensearch_pipeline.ontology import backfill as bf
    csv = tmp_path / "snap.csv"
    csv.write_text("namespace,raw_code,object_type,title,owner_dept\n"
                   "u8,X2,product,测试品2,pmc\n", encoding="utf-8")
    monkeypatch.setenv("RAG_ONTOLOGY_BACKFILL_ENABLE", "true")
    monkeypatch.setenv("RAG_ONTOLOGY_BACKFILL_PROD_ACK",
                       f"backfill-r1:{date.today().isoformat()}")
    monkeypatch.setattr("opensearch_pipeline.config.is_prod_target",
                        lambda kind, v: kind == "rds")
    store = MemoryOntologyStore()
    rc = bf.main([str(csv), "--commit"], store=store, source=None)
    assert rc == 0                                              # 闸过（mention 语义只入 case）
    assert store.get_open_case("u8", "X2") is not None
