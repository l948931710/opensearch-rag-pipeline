# -*- coding: utf-8 -*-
"""ontology/resolve.py + agent_tools/ontology_resolve.py —— 消解服务与工具（PR4）。

锁死的架构不变量：
- **S1 纯读**：resolve() 全路径零写库（spy store 上跑 exact/rule/embedding/unresolved）；
- **auto 三禁**：write 意图 / embedding / 多候选 永不 auto（may_auto_activate 仅离线调用）；
- **embedding 封顶**：相似度 1.0 也构造性够不着 τ_high；
- **未接线守护**：ontology_resolve 不在 build_default_registry（接线集中 PR13 一次重冻 L7）。
"""
import hashlib

import pytest

from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.ontology.resolve import (
    Candidate,
    OntologyResolver,
    Tau,
    TauTable,
    may_auto_activate,
)
from opensearch_pipeline.ontology.store import MemoryOntologyStore


def _hash_embedder(text):
    """确定性伪向量：同文本同向量（cos=1），异文本近似正交（cos≈0）。"""
    h = hashlib.sha256(text.strip().encode("utf-8")).digest()
    return [b / 255.0 * 2 - 1 for b in h]



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


@pytest.fixture()
def resolver(store):
    return OntologyResolver(store, embedder=_hash_embedder)


def _seed_product(store, title="6.2口径龙虾杯", ns="u8", raw="abc123", **obj_kw):
    obj = store.mint_object("product", title, owner_dept=obj_kw.pop("owner_dept", "pmc"),
                            **obj_kw)
    store.insert_identifier(ns, raw, raw.strip().upper(), obj["object_id"], method="seed")
    return obj


# ── TauTable（S6 机制）────────────────────────────────────────────────────────────


def test_tau_default_lookup():
    t = TauTable()
    assert t.lookup("u8", "rule") == Tau(0.95, 0.70)


def test_tau_layered_precedence():
    t = TauTable(layered={
        ("customer", "embedding"): Tau(0.97, 0.80),
        ("customer", "*"): Tau(0.96, 0.75),
        ("*", "rule"): Tau(0.93, 0.72),
    })
    assert t.lookup("customer:KFC", "embedding") == Tau(0.97, 0.80)   # (ns,method) 最先
    assert t.lookup("customer:KFC", "kie") == Tau(0.96, 0.75)         # (ns,*) 次之
    assert t.lookup("u8", "rule") == Tau(0.93, 0.72)                  # (*,method) 再次
    assert t.lookup("u8", "embedding") == Tau(0.95, 0.70)             # 全局默认兜底


def test_tau_ctor_rejects_inverted():
    with pytest.raises(ValueError):
        TauTable(default=Tau(high=0.5, low=0.9))


def test_tau_from_env_global_override(monkeypatch):
    monkeypatch.setenv("RAG_ONTOLOGY_TAU_HIGH", "0.90")
    monkeypatch.setenv("RAG_ONTOLOGY_TAU_LOW", "0.50")
    assert TauTable.from_env().lookup("u8", "rule") == Tau(0.90, 0.50)


def test_tau_from_env_layered_and_bad_entries(monkeypatch):
    monkeypatch.setenv(
        "RAG_ONTOLOGY_TAU_TABLE",
        '{"customer|embedding": [0.97, 0.80], "badkey": [0.9, 0.5], "u8|rule": [0.2, 0.9]}')
    t = TauTable.from_env()
    assert t.lookup("customer:X", "embedding") == Tau(0.97, 0.80)
    assert t.lookup("u8", "rule") == Tau(0.95, 0.70)      # high<low 条目被跳过 → 默认
    monkeypatch.setenv("RAG_ONTOLOGY_TAU_TABLE", "not-json")
    assert TauTable.from_env().lookup("u8", "rule") == Tau(0.95, 0.70)   # 整表 fail-open


def test_tau_from_env_inverted_global_falls_back(monkeypatch):
    monkeypatch.setenv("RAG_ONTOLOGY_TAU_HIGH", "0.3")
    monkeypatch.setenv("RAG_ONTOLOGY_TAU_LOW", "0.8")
    assert TauTable.from_env().lookup("u8", "rule") == Tau(0.95, 0.70)


# ── exact 命中 ───────────────────────────────────────────────────────────────────


def test_exact_hit_resolved_and_enriched(resolver, store):
    obj = _seed_product(store)
    res = resolver.resolve("u8", " abc123 ")          # 归一后命中
    assert res.status == "resolved" and res.confidence == 1.0 and res.method == "exact"
    assert res.object_id == obj["object_id"]
    assert res.canonical_ref == obj["canonical_ref"]
    assert res.title == "6.2口径龙虾杯" and res.object_type == "product"
    assert res.requires_hitl is False                 # read + resolved：可直接引用


def test_exact_hit_write_intent_requires_hitl(resolver, store):
    _seed_product(store)
    res = resolver.resolve("u8", "ABC123", intent="write")
    assert res.status == "resolved"
    assert res.requires_hitl is True                  # 写意图精确命中也须人审（S1）


def test_exact_hit_carries_target_revision(resolver, store):
    obj = store.mint_object("product", "改模品", owner_dept="pmc")
    store.insert_identifier("u8", "abc123-m", "ABC123-M", obj["object_id"],
                            method="manual", target_revision="r2")
    res = resolver.resolve("u8", "ABC123-M")
    assert res.status == "resolved" and res.target_revision == "r2"


def test_exact_hit_retired_target_never_resolved(resolver, store):
    """P0-03：目标对象退役后（含级联前的历史 active 别名窗口），exact 绝不返回 resolved。"""
    obj = _seed_product(store)
    # 绕过 retire 级联，直接把对象打成 retired，模拟"别名仍 active 而对象已死"的病态窗口
    store._objects[obj["object_id"]]["status"] = "retired"
    res = resolver.resolve("u8", "abc123")
    assert res.status == "unresolved" and res.requires_hitl is True
    assert res.object_id is None and res.confidence == 0.0


def test_exact_hit_merged_target_never_resolved(resolver, store):
    """P0-03：merged 残留别名（级联失败/历史脏数据）不得 resolved。"""
    obj = _seed_product(store)
    store._objects[obj["object_id"]]["status"] = "merged"
    res = resolver.resolve("u8", "abc123")
    assert res.status == "unresolved" and res.requires_hitl is True


def test_exact_hit_missing_target_never_resolved(resolver, store):
    """P0-03：目标对象整行缺失（悬空别名）不得 resolved（FK 加上后 RDS 不可达，Memory 防御）。"""
    obj = _seed_product(store)
    del store._objects[obj["object_id"]]
    res = resolver.resolve("u8", "abc123")
    assert res.status == "unresolved" and res.requires_hitl is True


def test_rule_candidate_skips_retired_base_target(resolver, store):
    """P0-03：剥后缀候选的基础码目标已退役 → 不作候选。"""
    obj = _seed_product(store)                        # ABC123 → obj
    store._objects[obj["object_id"]]["status"] = "retired"
    res = resolver.resolve("u8", "ABC123-M")
    assert all(c.target_object_id != obj["object_id"] for c in res.candidates)


@pytest.mark.parametrize("bad", ["", "   "])
def test_resolve_rejects_empty_value(resolver, bad):
    with pytest.raises(ValueError):
        resolver.resolve("u8", bad)


def test_resolve_rejects_bad_intent(resolver):
    with pytest.raises(ValueError, match="intent"):
        resolver.resolve("u8", "X", intent="delete")


# ── rule 候选（剥后缀提示）────────────────────────────────────────────────────────


def test_rule_candidate_from_suffix(resolver, store):
    obj = _seed_product(store)                        # ABC123 active
    res = resolver.resolve("u8", "ABC123-M")
    assert res.status == "candidate" and res.requires_hitl is True
    top = res.candidates[0]
    assert top.method == "rule" and top.target_object_id == obj["object_id"]
    assert top.confidence == pytest.approx(0.85)      # 设计性落在 HITL 区间：改模判定不 auto
    assert top.features["suffix"] == "-M" and top.features["base_norm"] == "ABC123"


@pytest.mark.parametrize("suffix", ["-N", "-W"])
def test_rule_candidate_other_suffixes(resolver, store, suffix):
    _seed_product(store)
    res = resolver.resolve("u8", f"ABC123{suffix}")
    assert res.status == "candidate" and res.candidates[0].method == "rule"


def test_rule_no_candidate_without_base(resolver):
    res = resolver.resolve("u8", "ZZZ999-M")
    assert res.status == "unresolved" and res.candidates == []


def test_rule_suffix_only_value_no_candidate(resolver, store):
    _seed_product(store)
    res = resolver.resolve("u8", "-M")                # 纯后缀不剥（len 护栏）
    assert all(c.method != "rule" for c in res.candidates)


# ── embedding 候选（封顶 + hint 过滤 + fail-open）─────────────────────────────────


def test_embedding_candidate_capped_below_tau_high(resolver, store):
    store.mint_object("product", "黑色注塑叉子", owner_dept="rd")
    res = resolver.resolve("lab_sample", "黑色注塑叉子")   # 同名 → cos=1.0
    assert res.status == "candidate"
    top = res.candidates[0]
    assert top.method == "embedding"
    assert top.confidence < 0.95                      # 构造性封顶：够不着 auto 线
    assert top.confidence >= 0.90
    assert top.features["similarity"] == pytest.approx(1.0)


def test_embedding_dissimilar_title_unresolved(resolver, store):
    store.mint_object("product", "PP水杯370ml", owner_dept="pmc")
    res = resolver.resolve("lab_sample", "黑色注塑叉子")
    assert res.status == "unresolved"                 # 异文本 cos≈0 < τ_low


def test_embedding_hint_narrows_pool(resolver, store):
    store.mint_object("sku", "黑色注塑叉子", owner_dept="pmc")
    hit = resolver.resolve("lab_sample", "黑色注塑叉子", object_type_hint="sku")
    miss = resolver.resolve("lab_sample", "黑色注塑叉子", object_type_hint="product")
    assert hit.status == "candidate" and hit.candidates[0].object_type == "sku"
    assert miss.status == "unresolved"


def test_embedding_disabled_with_none_embedder(store):
    store.mint_object("product", "黑色注塑叉子", owner_dept="rd")
    r = OntologyResolver(store, embedder=None)
    res = r.resolve("lab_sample", "黑色注塑叉子")
    assert res.status == "unresolved" and res.candidates == []


def test_embedding_failure_fails_open(store):
    store.mint_object("product", "黑色注塑叉子", owner_dept="rd")

    def _boom(_text):
        raise RuntimeError("dashscope down")

    r = OntologyResolver(store, embedder=_boom)
    res = r.resolve("lab_sample", "黑色注塑叉子")
    assert res.status == "unresolved"                 # 失败只降级，不炸解析


def test_candidates_truncated_to_cap(store):
    for i in range(7):
        store.mint_object("product", "黑色注塑叉子", owner_dept="pmc")   # 7 个同名对象
    r = OntologyResolver(store, embedder=_hash_embedder)
    res = r.resolve("lab_sample", "黑色注塑叉子")
    assert len(res.candidates) == 5                   # _MAX_CANDIDATES


def test_tau_env_flips_status(store, monkeypatch):
    """τ_low env 可调：默认 unresolved 的弱候选，调低门槛后变 candidate。"""
    store.mint_object("product", "完全不相干的品名XYZ", owner_dept="pmc")
    monkeypatch.setenv("RAG_ONTOLOGY_TAU_LOW", "0.0")
    monkeypatch.setenv("RAG_ONTOLOGY_TAU_HIGH", "0.95")
    r = OntologyResolver(store, embedder=_hash_embedder, tau_table=TauTable.from_env())
    res = r.resolve("lab_sample", "黑色注塑叉子")
    assert res.status == "candidate"                  # cos≈0 也 ≥ 0.0


# ── S1 纯读：全路径零写库 ─────────────────────────────────────────────────────────

_WRITE_METHODS = ("mint_object", "insert_identifier", "deactivate_identifier",
                  "repoint_identifier", "upsert_case", "add_candidate", "resolve_case",
                  "dismiss_case", "update_golden", "retire_object", "mark_duplicate",
                  "upsert_attribute_sources", "upsert_stewardship")


class _ReadOnlySpy(MemoryOntologyStore):
    """resolve() 期间任何写方法被触碰即炸——S1 的机械守护。"""

    armed = False

    def __getattribute__(self, name):
        if name in _WRITE_METHODS and object.__getattribute__(self, "armed"):
            raise AssertionError(f"resolve() 纯读被破坏：调用了写方法 {name}")
        return object.__getattribute__(self, name)


def test_resolve_never_writes():
    spy = _ReadOnlySpy()
    obj = spy.mint_object("product", "黑色注塑叉子", owner_dept="rd")
    spy.insert_identifier("u8", "abc123", "ABC123", obj["object_id"], method="seed")
    r = OntologyResolver(spy, embedder=_hash_embedder)
    spy.armed = True
    r.resolve("u8", "ABC123")                          # exact 路径
    r.resolve("u8", "ABC123-M")                        # rule 路径
    r.resolve("lab_sample", "黑色注塑叉子")             # embedding 路径
    r.resolve("u8", "ZZZ404", intent="write")          # unresolved + write 意图
    spy.armed = False


# ── may_auto_activate 三禁（离线专用判定）────────────────────────────────────────


def _cand(target="obj1", method="rule", conf=0.97, **kw):
    return Candidate(target_object_id=target, method=method, confidence=conf, **kw)


def test_auto_denied_for_write_intent():
    assert may_auto_activate([_cand()], intent="write", namespace="u8",
                             tau_table=TauTable()) is None


def test_auto_denied_for_embedding_top():
    assert may_auto_activate([_cand(method="embedding", conf=0.999)], intent="read",
                             namespace="u8", tau_table=TauTable()) is None


def test_auto_denied_for_competing_candidates():
    cands = [_cand(target="a", conf=0.97), _cand(target="b", method="kie", conf=0.75)]
    assert may_auto_activate(cands, intent="read", namespace="u8",
                             tau_table=TauTable()) is None


def test_auto_allows_same_target_multi_method():
    cands = [_cand(target="a", conf=0.97), _cand(target="a", method="kie", conf=0.80)]
    got = may_auto_activate(cands, intent="read", namespace="u8", tau_table=TauTable())
    assert got is not None and got.target_object_id == "a"    # 同目标=互相印证


def test_auto_denied_below_high_and_boundary():
    assert may_auto_activate([_cand(conf=0.94)], intent="read", namespace="u8",
                             tau_table=TauTable()) is None
    assert may_auto_activate([_cand(conf=0.95)], intent="read", namespace="u8",
                             tau_table=TauTable()) is not None   # ≥ 含边界


def test_auto_denied_for_suffix_rule_design_conf(resolver, store):
    """设计不变量：剥后缀 rule 候选（0.85）永远进不了 auto——改模判定全人审。"""
    _seed_product(store)
    res = resolver.resolve("u8", "ABC123-M")
    assert may_auto_activate(res.candidates, intent="read", namespace="u8",
                             tau_table=TauTable()) is None


def test_auto_empty_candidates():
    assert may_auto_activate([], intent="read", namespace="u8",
                             tau_table=TauTable()) is None


def test_auto_ignores_subthreshold_competitor():
    cands = [_cand(target="a", conf=0.96), _cand(target="b", method="kie", conf=0.30)]
    got = may_auto_activate(cands, intent="read", namespace="u8", tau_table=TauTable())
    assert got is not None and got.target_object_id == "a"    # 低于 τ_low 的不算竞争


# ── 工具层（READ_ONLY / 掩码 / 未接线守护）───────────────────────────────────────


def _ctx(acl=("pmc",)):
    return ExecutionContext.create(request_id="", user_id="u1", acl_groups=list(acl),
                                   roles=("employee",), channel="console", thread_id="t1")


@pytest.fixture()
def tool(store):
    from opensearch_pipeline.agent_tools.ontology_resolve import OntologyResolveTool
    return OntologyResolveTool(resolver=OntologyResolver(store, embedder=_hash_embedder))


def test_tool_spec_contract(tool):
    from opensearch_pipeline.agent_runtime.tool import RiskLevel
    spec = tool.spec
    assert spec.risk_level == RiskLevel.READ_ONLY
    assert spec.permission_scope == "ontology.resolve"
    assert spec.input_schema["additionalProperties"] is False
    assert "intent" not in spec.input_schema["properties"]     # 写意图无模型通道（S1）
    assert set(spec.input_schema["required"]) == {"namespace", "value"}


def test_tool_rejects_forged_params(tool):
    with pytest.raises(Exception):
        tool.run(_ctx(), {"namespace": "u8", "value": "A", "intent": "write"})
    with pytest.raises(Exception):
        tool.run(_ctx(), {"namespace": "u8", "value": "A", "user_dept": ["finance"]})


def test_tool_resolved_output(tool, store):
    _seed_product(store)
    res = tool.run(_ctx(), {"namespace": "u8", "value": "abc123"})
    assert res.status == "succeeded"
    assert res.receipt["status"] == "resolved"
    assert res.receipt["confidence"] == 1.0
    text = res.content[0].text
    assert "已解析" in text and "6.2口径龙虾杯" in text


def test_tool_candidate_output_marked_unconfirmed(tool, store):
    _seed_product(store)
    res = tool.run(_ctx(), {"namespace": "u8", "value": "ABC123-M"})
    assert res.receipt["status"] == "candidate" and res.receipt["requires_hitl"] is True
    assert len(res.receipt["candidates"]) <= 3
    assert "未经确认" in res.content[0].text


def test_tool_unresolved_output(tool):
    res = tool.run(_ctx(), {"namespace": "u8", "value": "ZZZ404"})
    assert res.receipt["status"] == "unresolved"
    assert "无法解析" in res.content[0].text


def test_tool_object_gate_confidential_without_acl(store):
    """PR-B（P0-02 验收④）：解析目标不可读 → 整体降级"无法解析"，零对象字段出参
    （不再返回掩码标题+代理号——ID/ref 即存在性泄露）。"""
    from opensearch_pipeline.agent_tools.ontology_resolve import OntologyResolveTool
    obj = store.mint_object("material", "PP-1100 特惠采购价目", owner_dept="supply",
                            data_classification="confidential")
    store.insert_identifier("material_grade", "pp-1100", "PP-1100", obj["object_id"],
                            method="seed")
    tool = OntologyResolveTool(resolver=OntologyResolver(store, embedder=None))
    denied = tool.run(_ctx(acl=("pmc",)),
                      {"namespace": "material_grade", "value": "pp-1100"})
    assert "PP-1100 特惠采购价目" not in denied.content[0].text
    assert obj["canonical_ref"] not in denied.content[0].text
    assert denied.receipt["status"] == "unresolved"
    assert denied.receipt["object_id"] is None
    assert denied.receipt["canonical_ref"] is None
    assert denied.receipt["requires_hitl"] is True
    clear = tool.run(_ctx(acl=("supply",)),
                     {"namespace": "material_grade", "value": "pp-1100"})
    assert clear.receipt["status"] == "resolved"
    assert "PP-1100 特惠采购价目" in clear.content[0].text


def test_confidential_never_leaves_domain_via_embedding(store):
    """PR-H（P1「embedding 出域」）：confidential 标题在调 provider 前就被过滤——
    机密品名对**任何**调用方都不产生 embedding 候选（消解只走 exact/rule/工作台），
    embedder 绝不收到机密标题。"""
    from opensearch_pipeline.agent_tools.ontology_resolve import OntologyResolveTool
    obj = store.mint_object("material", "机密牌号目录", owner_dept="supply",
                            data_classification="confidential")
    seen_titles = []

    def _spy_embedder(text):
        seen_titles.append(text)
        return _hash_embedder(text)

    tool = OntologyResolveTool(resolver=OntologyResolver(store, embedder=_spy_embedder))
    for acl in (("pmc",), ("supply",)):
        # 查询词 ≠ 机密标题（用户输入本就出域，不在断言范围）
        res = tool.run(_ctx(acl=acl), {"namespace": "lab_sample", "value": "牌号目录查询词"})
        assert res.receipt["candidates"] == []
        assert obj["canonical_ref"] not in res.content[0].text
    assert "机密牌号目录" not in seen_titles   # 池标题零出域（修复前会被逐个 embed）


def test_tool_filters_unreadable_candidates(store):
    """PR-B：不可读候选整条不出参（含 id/ref）；有权调用方候选完整（internal 对象）。"""
    from opensearch_pipeline.agent_tools.ontology_resolve import OntologyResolveTool
    obj = store.mint_object("material", "内部牌号目录", owner_dept="supply",
                            data_classification="internal")
    tool = OntologyResolveTool(resolver=OntologyResolver(store, embedder=_hash_embedder))
    denied = tool.run(_ctx(acl=("pmc",)), {"namespace": "lab_sample", "value": "内部牌号目录"})
    assert denied.receipt["candidates"] == []
    assert obj["canonical_ref"] not in denied.content[0].text
    allowed = tool.run(_ctx(acl=("supply",)),
                       {"namespace": "lab_sample", "value": "内部牌号目录"})
    assert allowed.receipt["status"] == "candidate"
    assert allowed.receipt["candidates"][0]["title"] == "内部牌号目录"


def test_tool_bad_value_returns_fail(tool):
    res = tool.run(_ctx(), {"namespace": "u8", "value": "   "})
    assert res.status == "failed" and "非法" in (res.error or "")


def test_tool_resolver_crash_returns_fail(store):
    from opensearch_pipeline.agent_tools.ontology_resolve import OntologyResolveTool

    class _Boom:
        def resolve(self, *a, **k):
            raise RuntimeError("db down")

    tool = OntologyResolveTool(resolver=_Boom())
    res = tool.run(_ctx(), {"namespace": "u8", "value": "A"})
    assert res.status == "failed" and "身份解析失败" in (res.error or "")


def test_tool_not_wired_into_default_registry():
    """未接线守护：接线与 L7 基线重冻集中在 PR13 一次完成——提前注册=连环破门。"""
    from opensearch_pipeline.agent_tools import build_default_registry
    names = {spec.name for spec in build_default_registry().list_specs()}
    assert "ontology_resolve" not in names


# ── PR-E（P0-07）：τ 数值域 + auto 硬关（默认候选-only）──────────────────────────


@pytest.mark.parametrize("high,low", [(-0.5, -1.0), (5.0, 0.7), (0.95, -0.1), (1.2, 1.1)])
def test_tau_ctor_rejects_out_of_range(high, low):
    """负数/大于 1 的 τ 在构造期拦死（外审探针：负阈值可让 0.01 置信 auto）。"""
    with pytest.raises(ValueError, match="τ 非法"):
        TauTable(default=Tau(high=high, low=low))


def test_tau_from_env_strict_raises_on_invalid(monkeypatch):
    monkeypatch.setenv("RAG_ONTOLOGY_TAU_HIGH", "-1")
    monkeypatch.setenv("RAG_ONTOLOGY_TAU_LOW", "-2")
    with pytest.raises(ValueError, match="strict"):
        TauTable.from_env(strict=True)
    # 非 strict（在线纯读）仍回默认——查询不因配置坏瘫掉
    assert TauTable.from_env().lookup("u8", "rule") == Tau(0.95, 0.70)
    monkeypatch.delenv("RAG_ONTOLOGY_TAU_HIGH")
    monkeypatch.delenv("RAG_ONTOLOGY_TAU_LOW")
    monkeypatch.setenv("RAG_ONTOLOGY_TAU_TABLE", '{"u8|rule": [5.0, 0.1]}')
    with pytest.raises(ValueError, match="strict"):
        TauTable.from_env(strict=True)


def _auto_candidate():
    return [Candidate(target_object_id="T1", method="rule", confidence=0.96)]


def test_auto_gate_default_closed(monkeypatch):
    """P0-07 核心：无 ack → 0.96 唯一 rule 候选也不 auto（候选-only 默认）。"""
    monkeypatch.delenv("RAG_ONTOLOGY_AUTO_ACK", raising=False)
    assert may_auto_activate(_auto_candidate(), intent="read", namespace="u8") is None
    from opensearch_pipeline.ontology.resolve import auto_activation_enabled
    assert auto_activation_enabled() is False


@pytest.mark.parametrize("token", [
    "malformed",                                  # 缺段
    "op:2020-01-01:deadbeefcafe",                 # 过期
    ":2099-01-01:deadbeefcafe",                   # op 空（日期也不对，双重非法）
    "op:TODAY:short",                             # hash 过短（TODAY 占位，下面替换）
])
def test_auto_gate_rejects_bad_tokens(monkeypatch, token):
    from datetime import date
    monkeypatch.setenv("RAG_ONTOLOGY_AUTO_ACK", token.replace("TODAY", date.today().isoformat()))
    assert may_auto_activate(_auto_candidate(), intent="read", namespace="u8") is None


def test_auto_gate_valid_token_opens(monkeypatch):
    from datetime import date
    monkeypatch.setenv("RAG_ONTOLOGY_AUTO_ACK",
                       f"gt-run:{date.today().isoformat()}:cafebabe1234")
    winner = may_auto_activate(_auto_candidate(), intent="read", namespace="u8")
    assert winner is not None and winner.target_object_id == "T1"
