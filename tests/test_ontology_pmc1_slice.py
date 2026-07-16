# -*- coding: utf-8 -*-
"""
test_ontology_pmc1_slice.py — PMC-1 切片 PR11-PR14（packing 内核/工具、实体锚定、
flag 接线、backtest）。

夹具全走 MemoryOntologyStore（hermetic）；接线/授予的双向守护在
test_ontology_resolve.py / test_ontology_identity_resolve_tool.py（本文件只测新面）。
"""
import json

import pytest

from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.ontology.packing_math import (
    CalcRule,
    PackingRuleError,
    plan_shipment,
)
from opensearch_pipeline.ontology.store import MemoryOntologyStore

# ── 共享夹具 ─────────────────────────────────────────────────────────────────

_RULE_GOLDEN = {
    "containers": {"40HQ": {"inner_l_mm": 12032, "inner_w_mm": 2352,
                            "inner_h_mm": 2698, "max_payload_kg": 28000}},
    "fill_rate": 0.85, "hem_allowance_mm": 5,
    "applicable_object_types": ["sku"],
    "source": "单测夹具参数（非生产）",
}


def _rule_obj(**over):
    obj = {"canonical_ref": "FLP-CR-000001", "version": 3, "title": "测试装柜参数",
           "object_id": "rule1", "golden_json": dict(_RULE_GOLDEN)}
    obj.update(over)
    return obj


def _ctx(acl=("pmc",)):
    return ExecutionContext.create(request_id="r", user_id="u1", acl_groups=list(acl),
                                   roles=["employee"], channel="console", thread_id="t")


def _seed_sku_with_packing(store, *, raw="LX620-50", classification="public",
                           gross_kg=None, owner="pmc"):
    golden = {"box_type": "五层瓦楞", "per_box": "3000", "outer_dim": "600x400x300"}
    if gross_kg is not None:
        golden["gross_kg"] = gross_kg
    sku = store.mint_object("sku", "6.2口径龙虾杯 50只装", owner_dept=owner,
                            data_classification=classification, _caller="test")
    store.insert_identifier("u8", raw, raw.strip().upper(), sku["object_id"],
                            method="seed", _caller="test")
    spec = store.mint_object("packing_spec", "龙虾杯箱规", owner_dept=owner,
                             data_classification=classification, golden=golden,
                             allow_missing_provenance=True, _caller="test")
    store.add_link(spec["object_id"], sku["object_id"], "packing_spec_of_sku",
                   _caller="test")
    return sku, spec


def _seed_rule(store, *, classification="public", owner="pmc", golden=None):
    return store.mint_object("calc_rule", "PMC 装柜参数", owner_dept=owner,
                             data_classification=classification,
                             golden=dict(golden or _RULE_GOLDEN),
                             allow_missing_provenance=True, _caller="test")


# ── PR11 packing_math 纯内核 ─────────────────────────────────────────────────


def test_calc_rule_from_object_and_citation():
    rule = CalcRule.from_object(_rule_obj())
    assert rule.rule_ref == "FLP-CR-000001" and rule.version == 3
    assert rule.containers["40HQ"].max_payload_kg == 28000
    c = rule.citation()
    assert c["model"] == "volume_fill" and c["fill_rate"] == 0.85
    assert c["rule_ref"] == "FLP-CR-000001" and c["rule_version"] == 3


@pytest.mark.parametrize("mutate,msg", [
    (lambda g: g.pop("containers"), "containers"),
    (lambda g: g.update(fill_rate=0), "fill_rate"),
    (lambda g: g.update(fill_rate=1.2), "fill_rate"),
    (lambda g: g.update(hem_allowance_mm=-1), "hem_allowance_mm"),
    (lambda g: g.update(effective_from="not-a-date"), "effective_from"),
    (lambda g: g["containers"].update({"20GP": {"inner_l_mm": -1, "inner_w_mm": 1,
                                                "inner_h_mm": 1}}), "20GP"),
])
def test_calc_rule_rejects_bad_params(mutate, msg):
    golden = json.loads(json.dumps(_RULE_GOLDEN))
    mutate(golden)
    with pytest.raises(PackingRuleError, match=msg):
        CalcRule.from_object(_rule_obj(golden_json=golden))


def test_calc_rule_effective_window():
    golden = dict(_RULE_GOLDEN, effective_from="2099-01-01")
    rule = CalcRule.from_object(_rule_obj(golden_json=golden))
    with pytest.raises(PackingRuleError, match="起生效"):
        rule.assert_effective()


def test_plan_shipment_volume_and_demand_math():
    """确定性算例：40HQ 内容积 12032×2352×2698mm=76.351414272m³；箱 600×400×300
    +5mm 折边 → 605×405×305=0.074732625m³；产能 floor(76.351414272×0.85/0.074732625)
    =868 箱（向下取整，宁少报）；60 万件÷3000=200 箱（向上取整）→ 1 柜（尾柜 200 箱）。"""
    rule = CalcRule.from_object(_rule_obj())
    plan = plan_shipment(carton_mm=(600, 400, 300), rule=rule, pcs_per_carton=3000,
                         order_qty_pcs=600000, container_name="40HQ")
    p = plan.plans[0]
    assert p.cartons_by_volume == 868
    assert p.cartons_capacity == 868 and p.pcs_capacity == 868 * 3000
    assert p.binding_constraint == "volume"      # 无毛重 → 体积约束
    assert plan.order_cartons == 200
    assert p.containers_needed == 1 and p.last_container_cartons == 200
    assert plan.citation["rule_ref"] == "FLP-CR-000001"
    assert any("毛重" in n for n in plan.notes)   # 无毛重注记


def test_plan_shipment_weight_binding():
    """毛重 100kg/箱 × 限重 28000kg → 280 箱 < 体积产能 868 → 限重约束方。"""
    rule = CalcRule.from_object(_rule_obj())
    plan = plan_shipment(carton_mm=(600, 400, 300), rule=rule, pcs_per_carton=3000,
                         carton_gross_kg=100, container_name="40HQ")
    p = plan.plans[0]
    assert p.cartons_by_weight == 280 and p.cartons_capacity == 280
    assert p.binding_constraint == "weight"


def test_plan_shipment_demand_ceils():
    rule = CalcRule.from_object(_rule_obj())
    plan = plan_shipment(carton_mm=(600, 400, 300), rule=rule, pcs_per_carton=100,
                         order_qty_pcs=601, container_name="40HQ")
    assert plan.order_cartons == 7                # ceil(601/100)


@pytest.mark.parametrize("kw,msg", [
    (dict(container_name="20GP"), "不在 calc_rule"),
    (dict(order_qty_pcs=100), "缺每箱件数"),
    (dict(carton_mm=(0, 400, 300)), "箱长"),
])
def test_plan_shipment_input_errors(kw, msg):
    rule = CalcRule.from_object(_rule_obj())
    base = dict(carton_mm=(600, 400, 300), rule=rule)
    base.update(kw)
    with pytest.raises(PackingRuleError, match=msg):
        plan_shipment(**base)


# ── PR11 packing_calc 工具 ───────────────────────────────────────────────────


def _calc_tool(store):
    from opensearch_pipeline.agent_tools.packing_calc import PackingCalcTool
    return PackingCalcTool(store=store)


def test_packing_calc_happy_path_cites_rule():
    store = MemoryOntologyStore()
    _seed_sku_with_packing(store)
    _seed_rule(store)
    res = _calc_tool(store).run(_ctx(), {"target": "LX620-50", "namespace": "u8",
                                         "container": "40HQ", "order_qty_pcs": 600000})
    assert res.status == "succeeded"
    assert res.receipt["status"] == "ok"
    assert res.receipt["plans"][0]["cartons_capacity"] == 868
    assert res.receipt["order"] == {"qty_pcs": 600000, "cartons": 200}
    text = res.content[0].text
    assert "计算依据" in text and "FLP-CR-" in text and "868" in text


def test_packing_calc_unresolved_and_no_spec():
    store = MemoryOntologyStore()
    _seed_rule(store)
    res = _calc_tool(store).run(_ctx(), {"target": "NOPE-1", "namespace": "u8"})
    assert res.receipt["status"] == "unresolved"
    # 有 SKU 无箱规 → no_visible_spec（与无权限同答）
    sku = store.mint_object("sku", "无箱规品", owner_dept="pmc",
                            data_classification="public", _caller="test")
    store.insert_identifier("u8", "BARE-1", "BARE-1", sku["object_id"],
                            method="seed", _caller="test")
    res2 = _calc_tool(store).run(_ctx(), {"target": "BARE-1", "namespace": "u8"})
    assert res2.receipt["status"] == "no_visible_spec"


def test_packing_calc_acl_hides_spec_from_outsider():
    store = MemoryOntologyStore()
    _seed_sku_with_packing(store, classification="internal", owner="pmc")
    _seed_rule(store)
    res = _calc_tool(store).run(_ctx(acl=("hr",)), {"target": "LX620-50", "namespace": "u8"})
    # spec 行 internal+pmc 对 hr 不可见；SKU 对象本身也 internal → 整体未消解同答
    assert res.receipt["status"] in ("unresolved", "no_visible_spec")
    assert "868" not in res.content[0].text


def test_packing_calc_no_rule_fail_closed():
    store = MemoryOntologyStore()
    _seed_sku_with_packing(store)
    res = _calc_tool(store).run(_ctx(), {"target": "LX620-50", "namespace": "u8"})
    assert res.receipt["status"] == "rule_error"
    assert "calc_rule" in res.receipt["error"]


def test_packing_calc_multiple_rules_require_explicit_ref():
    store = MemoryOntologyStore()
    _seed_sku_with_packing(store)
    r1 = _seed_rule(store)
    _seed_rule(store)
    res = _calc_tool(store).run(_ctx(), {"target": "LX620-50", "namespace": "u8"})
    assert res.receipt["status"] == "rule_error" and "rule_ref" in res.receipt["error"]
    res2 = _calc_tool(store).run(_ctx(), {"target": "LX620-50", "namespace": "u8",
                                          "rule_ref": r1["canonical_ref"]})
    assert res2.receipt["status"] == "ok"


def test_packing_calc_rule_selection_no_n_plus_1():
    """§4.2（perf 批次 A）：无 rule_ref 的规则选取走批量授权全列查询一把取，
    不再 find_objects('calc_rule') + 逐规则 get_object。"""
    store = MemoryOntologyStore()
    _seed_sku_with_packing(store)
    _seed_rule(store)
    calls = {"find_objects": 0, "search_objects_authorized_full": 0}

    def _spy(name):
        orig = getattr(store, name)

        def w(*a, **k):
            if a and a[0] == "calc_rule":
                calls[name] += 1
            return orig(*a, **k)
        setattr(store, name, w)

    for n in list(calls):
        _spy(n)
    res = _calc_tool(store).run(_ctx(), {"target": "LX620-50", "namespace": "u8",
                                         "container": "40HQ", "order_qty_pcs": 600000})
    assert res.receipt["status"] == "ok"
    assert calls["search_objects_authorized_full"] == 1   # 批量授权全列一把取
    assert calls["find_objects"] == 0                     # 旧 find_objects+逐条 get_object 已弃


# ── PR12 实体锚定 ────────────────────────────────────────────────────────────


def test_extract_code_tokens():
    from opensearch_pipeline.ontology.packing_lookup import extract_code_tokens
    toks = extract_code_tokens("LX620-50 的箱规是多少？一个 40HQ 能装多少箱，2026 年交货")
    assert "LX620-50" in toks
    assert "2026" not in toks                     # 纯数字排除
    assert extract_code_tokens("今天天气怎么样") == []


def test_anchor_for_query_exact_hit_builds_block():
    from opensearch_pipeline.ontology.packing_lookup import anchor_for_query
    store = MemoryOntologyStore()
    sku, _spec = _seed_sku_with_packing(store)
    block, receipt = anchor_for_query("LX620-50 的箱规是多少", acl_groups=["pmc"],
                                      store=store)
    assert block and "实体档案" in block and sku["canonical_ref"] in block
    assert "每箱 3000 件" in block
    assert receipt["anchors"][0]["token"] == "LX620-50"


def test_anchor_for_query_acl_and_miss_are_silent():
    from opensearch_pipeline.ontology.packing_lookup import anchor_for_query
    store = MemoryOntologyStore()
    _seed_sku_with_packing(store, classification="confidential", owner="pmc")
    # 不可读 → 不锚（零存在性泄露）；未知 token → 不锚
    assert anchor_for_query("LX620-50 箱规", acl_groups=["hr"], store=store) == (None, None)
    assert anchor_for_query("XYZ-999 是什么", acl_groups=["pmc"], store=store) == (None, None)


def test_knowledge_search_anchor_flag_off_unchanged(monkeypatch):
    monkeypatch.delenv("RAG_ONTOLOGY_ANCHOR_ENABLE", raising=False)
    import opensearch_pipeline.retriever as rt
    from opensearch_pipeline.agent_tools.knowledge_search import KnowledgeSearchTool
    monkeypatch.setattr(rt, "retrieve_and_enrich", lambda query, **k: [
        {"doc_id": "D1", "chunk_text": "包装标准全文", "doc_title": "包装规范"}])
    res = KnowledgeSearchTool().run(_ctx(), {"query": "LX620-50 箱规"})
    assert res.status == "succeeded"
    assert all("实体档案" not in (b.text or "") for b in res.content)
    assert "anchor" not in (res.receipt or {})


def test_knowledge_search_anchor_flag_on_prepends_block(monkeypatch):
    monkeypatch.setenv("RAG_ONTOLOGY_ANCHOR_ENABLE", "true")
    import opensearch_pipeline.ontology.packing_lookup as pl
    import opensearch_pipeline.retriever as rt
    from opensearch_pipeline.agent_tools.knowledge_search import KnowledgeSearchTool
    monkeypatch.setattr(rt, "retrieve_and_enrich", lambda query, **k: [
        {"doc_id": "D1", "chunk_text": "包装标准全文", "doc_title": "包装规范"}])
    monkeypatch.setattr(pl, "anchor_for_query",
                        lambda query, acl_groups: ("【实体档案·本体锚定】桩块",
                                                   {"anchors": [{"token": "LX620-50"}]}))
    res = KnowledgeSearchTool().run(_ctx(), {"query": "LX620-50 箱规"})
    assert res.status == "succeeded"
    assert "实体档案" in res.content[0].text      # 锚定块前置
    assert res.receipt["anchor"]["anchors"][0]["token"] == "LX620-50"
    # 检索片段仍在（锚定是前置不是替换）
    assert any("包装" in (b.text or "") for b in res.content[1:])


def test_knowledge_search_anchor_failure_is_fail_open(monkeypatch):
    monkeypatch.setenv("RAG_ONTOLOGY_ANCHOR_ENABLE", "true")
    import opensearch_pipeline.ontology.packing_lookup as pl
    import opensearch_pipeline.retriever as rt
    from opensearch_pipeline.agent_tools.knowledge_search import KnowledgeSearchTool
    monkeypatch.setattr(rt, "retrieve_and_enrich", lambda query, **k: [
        {"doc_id": "D1", "chunk_text": "包装标准全文", "doc_title": "包装规范"}])

    def _boom(*a, **k):
        raise RuntimeError("锚定层炸了")

    monkeypatch.setattr(pl, "anchor_for_query", _boom)
    res = KnowledgeSearchTool().run(_ctx(), {"query": "LX620-50 箱规"})
    assert res.status == "succeeded"              # 检索主路径不受影响


# ── PR13 提示词条件增段 ──────────────────────────────────────────────────────


def test_prompt_ontology_suffix_flag_gated(monkeypatch):
    import opensearch_pipeline.routes.agent as agent_route
    monkeypatch.delenv("RAG_ONTOLOGY_TOOLS_ENABLE", raising=False)
    off = agent_route._agent_system_prompt()
    assert "ontology_resolve" not in off          # off 臂逐字节无本体段（L7 冻结基线）
    monkeypatch.setenv("RAG_ONTOLOGY_TOOLS_ENABLE", "true")
    on = agent_route._agent_system_prompt()
    assert "ontology_resolve" in on and "packing_calc" in on
    assert on.replace(agent_route._AGENT_PROMPT_ONTOLOGY_SUFFIX, "") == off


def test_l7_runner_scripted_selfcheck_ontology_family():
    """PR13 增族自检（scripted provider，零网络零付费）：理想模型下触发率/参数相关性
    恒 1.0——判分逻辑本身的回归锁；live 重冻是另一场付费会话。"""
    from eval_harness.agent import runner
    import json as _json
    import pathlib
    cases_path = pathlib.Path(runner.__file__).parent / "agent_cases_ontology.json"
    cases = _json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    scores = []
    for c in cases:
        out = runner._run_case(c, runner._ScriptedIdealProvider)
        scores.append(runner._score(c, out))
    m = runner._aggregate(scores)
    assert m["n_ontology_tool_expected"] == len(cases)
    assert m["ontology_tool_trigger_rate"] == 1.0
    assert m["ontology_args_relevance"] == 1.0
    assert m["error_count"] == 0


# ── PR14 backtest ────────────────────────────────────────────────────────────


def _backtest_store():
    store = MemoryOntologyStore()
    a = store.mint_object("sku", "甲品", owner_dept="pmc",
                          data_classification="public", _caller="test")
    b = store.mint_object("sku", "乙品", owner_dept="pmc",
                          data_classification="public", _caller="test")
    store.insert_identifier("u8", "AAA-1", "AAA-1", a["object_id"], method="seed",
                            _caller="test")
    # 错绑：BBB-2 在库里指向甲品，而 GT 期望乙品 → false_merge
    store.insert_identifier("u8", "BBB-2", "BBB-2", a["object_id"], method="seed",
                            _caller="test")
    return store, a, b


def test_backtest_metrics_and_false_merge_gate(tmp_path):
    from scripts.ontology_backtest import main, run_backtest
    store, a, b = _backtest_store()
    rows = [
        {"namespace": "u8", "raw_code": "AAA-1", "expected_ref": a["canonical_ref"]},
        {"namespace": "u8", "raw_code": "BBB-2", "expected_ref": b["canonical_ref"]},
        {"namespace": "u8", "raw_code": "CCC-3", "expected_ref": b["canonical_ref"]},
        {"namespace": "u8", "raw_code": "DDD-4", "expected_ref": ""},
    ]
    report = run_backtest(store, rows)
    m = report["metrics"]
    assert m["exact_correct"] == 1
    assert m["false_merge"] == 1                  # BBB-2 解析到甲品 ≠ GT 乙品
    assert m["unresolved_miss"] == 1              # CCC-3 无候选
    assert m["true_negative"] == 1                # DDD-4 应无对应且确实未解析
    assert m["auto_wrong"] == 0                   # exact 命中不产生 would-auto（那是正式映射）
    assert "u8|exact" in report["strata"]

    csv_p = tmp_path / "gt.csv"
    csv_p.write_text("namespace,raw_code,expected_ref\n"
                     f"u8,BBB-2,{b['canonical_ref']}\n", encoding="utf-8")
    assert main([str(csv_p)], store=store) == 2   # false-merge 硬门破 → exit 2


def test_backtest_clean_gt_passes_and_prints_manifest(tmp_path, capsys):
    from scripts.ontology_backtest import main
    store, a, _b = _backtest_store()
    csv_p = tmp_path / "gt.csv"
    csv_p.write_text("namespace,raw_code,expected_ref\n"
                     f"u8,AAA-1,{a['canonical_ref']}\n"
                     "u8,DDD-4,\n", encoding="utf-8")
    out_p = tmp_path / "report.json"
    rc = main([str(csv_p), "--out", str(out_p)], store=store)
    assert rc == 0
    printed = capsys.readouterr().out
    assert "source_sha256" in printed and "manifest 骨架" in printed
    import hashlib
    assert hashlib.sha256(csv_p.read_bytes()).hexdigest() in printed
    report = json.loads(out_p.read_text(encoding="utf-8"))
    assert report["metrics"]["false_merge"] == 0


def test_auto_eligible_pure_function_no_ack_needed(monkeypatch):
    """auto_eligible 是评估切面：无 ack/manifest 也能判——但 may_auto_activate 仍被
    ack 闸锁死（评估≠放行）。"""
    monkeypatch.delenv("RAG_ONTOLOGY_AUTO_ACK", raising=False)
    monkeypatch.delenv("RAG_ONTOLOGY_ACK_HMAC_KEY", raising=False)
    from opensearch_pipeline.ontology.resolve import (
        Candidate, TauTable, auto_eligible, may_auto_activate)
    cands = [Candidate(target_object_id="T1", method="rule", confidence=0.96)]
    assert auto_eligible(cands, intent="read", namespace="u8",
                         tau_table=TauTable()) is not None
    assert may_auto_activate(cands, intent="read", namespace="u8",
                             tau_table=TauTable(), source_fingerprint="ab" * 32) is None
