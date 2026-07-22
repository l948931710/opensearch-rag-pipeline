# -*- coding: utf-8 -*-
"""Majors γ4(M15 价表)——RAG_MODEL_PRICE_TABLE_JSON 契约+回算+写路径(2026-07-21 codex 共识)。

- parse_price_table：空→None；非法(坏 JSON/缺 version/负价/NaN/超长版本)→ValueError；
- estimate_cost：Decimal quantize(0.0001, HALF_UP)；未知 model/(0,0) 用量→(None,None)；
- load_config：非空非法 fail-fast；合法放行；
- 写路径 _insert_llm_row：带版本=16 列；057 缺列 1054→负缓存+同语句降级 15 列；
  未配价表=15 列 byte-identical；
- gateway._record_call：价表配了才携 price_table_version 键/kwarg(旧桩零破坏)。
"""
import json
from decimal import Decimal

import pytest

from opensearch_pipeline import llm_pricing
from opensearch_pipeline.llm_pricing import estimate_cost, parse_price_table

_TABLE = {
    "version": "2026-07-v1", "effective_from": "2026-07-21",
    "models": {"qwen3.6-plus": {"in_rmb_per_mtok": 4.0, "out_rmb_per_mtok": 12.0},
               "qwen3-vl-plus": {"in_rmb_per_mtok": "8.0", "out_rmb_per_mtok": 24}},
}


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    monkeypatch.delenv("RAG_MODEL_PRICE_TABLE_JSON", raising=False)
    llm_pricing._reset_cache()
    yield
    llm_pricing._reset_cache()


# ── parse ────────────────────────────────────────────────────────────────────


def test_parse_empty_is_disabled():
    assert parse_price_table("") is None and parse_price_table("   ") is None


def test_parse_valid_converts_decimal():
    t = parse_price_table(json.dumps(_TABLE))
    assert t["version"] == "2026-07-v1"
    assert t["models"]["qwen3.6-plus"] == (Decimal("4.0"), Decimal("12.0"))
    assert t["models"]["qwen3-vl-plus"] == (Decimal("8.0"), Decimal("24"))


@pytest.mark.parametrize("raw", [
    "{not json",
    json.dumps({"models": {"m": {"in_rmb_per_mtok": 1, "out_rmb_per_mtok": 1}}}),  # 缺 version
    json.dumps({"version": "v", "models": {}}),                                     # 空 models
    json.dumps({"version": "v", "effective_from": "d",
                "models": {"m": {"in_rmb_per_mtok": -1, "out_rmb_per_mtok": 1}}}),  # 负价
    json.dumps({"version": "v", "effective_from": "d",
                "models": {"m": {"in_rmb_per_mtok": "NaN", "out_rmb_per_mtok": 1}}}),  # NaN
    json.dumps({"version": "v" * 40, "effective_from": "d",
                "models": {"m": {"in_rmb_per_mtok": 1, "out_rmb_per_mtok": 1}}}),   # 版本超32
    json.dumps({"version": "v", "models": {"m": {"in_rmb_per_mtok": 1,
                                                 "out_rmb_per_mtok": 1}}}),         # 缺 effective_from
])
def test_parse_invalid_raises(raw):
    with pytest.raises(ValueError):
        parse_price_table(raw)


# ── estimate ─────────────────────────────────────────────────────────────────


def test_estimate_rounding_half_up(monkeypatch):
    monkeypatch.setenv("RAG_MODEL_PRICE_TABLE_JSON", json.dumps(_TABLE))
    llm_pricing._reset_cache()
    # 1000*4/1e6 + 500*12/1e6 = 0.004 + 0.006 = 0.0100
    cost, ver = estimate_cost("qwen3.6-plus", 1000, 500)
    assert cost == Decimal("0.0100") and ver == "2026-07-v1"
    # 13*4/1e6 = 0.000052 → HALF_UP 到 0.0001
    cost2, _ = estimate_cost("qwen3.6-plus", 13, 0)
    assert cost2 == Decimal("0.0001")


def test_estimate_unknown_model_and_no_usage(monkeypatch):
    monkeypatch.setenv("RAG_MODEL_PRICE_TABLE_JSON", json.dumps(_TABLE))
    llm_pricing._reset_cache()
    assert estimate_cost("gpt-x", 100, 100) == (None, None)          # 未知不编造
    assert estimate_cost("qwen3.6-plus", 0, 0) == (None, None)       # 无用量不落 0
    assert estimate_cost(None, 100, 100) == (None, None)


def test_estimate_disabled_by_default():
    assert estimate_cost("qwen3.6-plus", 1000, 1000) == (None, None)


# ── config fail-fast ─────────────────────────────────────────────────────────


def test_load_config_rejects_invalid_table():
    from tests.test_config_loading import _fresh_load
    with pytest.raises(ValueError, match="RAG_MODEL_PRICE_TABLE_JSON"):
        _fresh_load(RAG_SIMULATE="true", RAG_MODEL_PRICE_TABLE_JSON="{bad")


def test_load_config_accepts_valid_table():
    from tests.test_config_loading import _fresh_load
    cfg = _fresh_load(RAG_SIMULATE="true",
                      RAG_MODEL_PRICE_TABLE_JSON=json.dumps(_TABLE))
    assert cfg is not None


# ── 写路径（fake cursor）─────────────────────────────────────────────────────


class _Cur:
    def __init__(self, raise_1054_on_16col=False):
        self.sqls = []
        self._raise = raise_1054_on_16col

    def execute(self, sql, params=None):
        self.sqls.append((sql, params))
        if self._raise and "price_table_version" in sql:
            raise Exception(1054, "Unknown column 'price_table_version' in 'field list'")


@pytest.fixture(autouse=True)
def _reset_price_col_cache(monkeypatch):
    from opensearch_pipeline.agent_runtime import run_store
    monkeypatch.setattr(run_store, "_PRICE_VERSION_COL_MISSING", False)


def _row(**kw):
    base = {"call_id": "c1", "provider": "dashscope", "model": "qwen3.6-plus",
            "status": "ok", "cost_estimate": Decimal("0.0100")}
    base.update(kw)
    return base


def test_insert_llm_row_with_version_uses_16_cols():
    from opensearch_pipeline.agent_runtime.run_store import _insert_llm_row
    cur = _Cur()
    _insert_llm_row(cur, "db", "r1", _row(price_table_version="2026-07-v1"))
    assert len(cur.sqls) == 1 and "price_table_version" in cur.sqls[0][0]
    assert cur.sqls[0][1][-1] == "2026-07-v1"


def test_insert_llm_row_without_version_byte_identical():
    from opensearch_pipeline.agent_runtime.run_store import _insert_llm_row
    cur = _Cur()
    _insert_llm_row(cur, "db", "r1", _row())
    assert len(cur.sqls) == 1 and "price_table_version" not in cur.sqls[0][0]


def test_insert_llm_row_1054_degrades_and_caches():
    from opensearch_pipeline.agent_runtime import run_store
    cur = _Cur(raise_1054_on_16col=True)
    run_store._insert_llm_row(cur, "db", "r1", _row(price_table_version="v"))
    # 第一句 16 列被 1054 拒 → 同语句 15 列降级重写
    assert len(cur.sqls) == 2 and "price_table_version" not in cur.sqls[1][0]
    assert run_store._PRICE_VERSION_COL_MISSING is True
    # 负缓存后直接 15 列
    cur2 = _Cur()
    run_store._insert_llm_row(cur2, "db", "r1", _row(price_table_version="v"))
    assert len(cur2.sqls) == 1 and "price_table_version" not in cur2.sqls[0][0]


# ── gateway 行构造 ────────────────────────────────────────────────────────────


class _Usage:
    tokens_prompt, tokens_completion = 1000, 500
    total = 1500


def _gateway(spy):
    from opensearch_pipeline.agent_runtime.model_gateway import ModelGateway
    return ModelGateway({}, routes={}, call_logger=spy)


def test_gateway_sync_row_carries_version_only_when_configured(monkeypatch):
    calls = []

    def spy(**kw):
        calls.append(kw)

    gw = _gateway(spy)

    class _Ctx:
        run_id, request_id, user_id, acl_groups = "r", "q", "u", ["g"]
        prompt_version = None

    # 未配价表：不携新 kwarg（旧桩 logger 零破坏）
    gw._record_call(_Ctx(), "agent", "dashscope", "qwen3.6-plus", _Usage(), 5, "error")
    assert "price_table_version" not in calls[-1] and calls[-1]["cost_estimate"] is None
    # 配了价表：cost+version 同落
    monkeypatch.setenv("RAG_MODEL_PRICE_TABLE_JSON", json.dumps(_TABLE))
    llm_pricing._reset_cache()
    gw._record_call(_Ctx(), "agent", "dashscope", "qwen3.6-plus", _Usage(), 5, "error")
    assert calls[-1]["price_table_version"] == "2026-07-v1"
    assert calls[-1]["cost_estimate"] == Decimal("0.0100")


def test_gateway_fold_row_carries_version(monkeypatch):
    monkeypatch.setenv("RAG_MODEL_PRICE_TABLE_JSON", json.dumps(_TABLE))
    llm_pricing._reset_cache()
    gw = _gateway(lambda **kw: None)
    fold = []

    class _Ctx:
        run_id, request_id, user_id, acl_groups = "r", "q", "u", ["g"]
        prompt_version = None
        _llm_fold = fold

    gw._record_call(_Ctx(), "agent", "dashscope", "qwen3.6-plus", _Usage(), 5, "ok")
    assert fold and fold[0]["price_table_version"] == "2026-07-v1"
    assert fold[0]["cost_estimate"] == Decimal("0.0100")
