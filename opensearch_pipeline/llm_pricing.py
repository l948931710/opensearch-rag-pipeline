# -*- coding: utf-8 -*-
"""llm_pricing.py — LLM 成本回算价表（Majors γ4/M15，codex 共识 2026-07-21）。

背景：llm_call_log.cost_estimate 恒 NULL 是记录在案的拍板（schema/023:6——不编造
模型单价，价表配置后回算）。本模块是那张价表的落地：`RAG_MODEL_PRICE_TABLE_JSON`
非空时按 token 用量回算 cost_estimate，并把 price_table_version 同行落值
（schema/057）——成本可审计（哪版价表算的）、换版可追溯。

契约（env JSON）::

    {"version": "2026-07-v1", "effective_from": "2026-07-21",
     "models": {"qwen3.6-plus": {"in_rmb_per_mtok": 4.0, "out_rmb_per_mtok": 12.0}}}

- 默认空 → 完全不启用：estimate_cost 恒 (None, None)，cost_estimate 维持 NULL（拍板不破）。
- 非空非法 → parse_price_table 抛 ValueError，load_config fail-fast 拒绝启动
  （价表是成本审计输入，静默吞坏配置=成本回算悄悄停摆）。
- 单价单位=元/百万 token（rmb per mtok），须非负有限数（拒 NaN/Inf/负数/非数）。
- 未知 model → (None, None)（不编造；新模型上线先补价表）。
- **Decimal 计算**：cost = (tp*in + tc*out) / 1_000_000，quantize 到 0.0001、
  ROUND_HALF_UP——与 llm_call_log.cost_estimate DECIMAL(10,4) 落库精度一致
  （入库前即定值，不依赖 MySQL 端舍入）。

运行期读取走进程内缓存（_get_cached）：env 在 load_config 已验过，这里坏表只
warn+当未配（记账辅助面绝不炸模型调用）；测试用 _reset_cache() 重读。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

PRICE_TABLE_ENV = "RAG_MODEL_PRICE_TABLE_JSON"
_QUANT = Decimal("0.0001")          # DECIMAL(10,4) 对齐

_lock = threading.Lock()
_cache: dict = {"raw": None, "table": None}   # raw=解析时的 env 原文（变更即重解析）


def parse_price_table(raw: str) -> Optional[dict]:
    """解析+校验价表 JSON。空/空白 → None（未启用）；非法 → ValueError（调用方
    load_config fail-fast）。返回 {"version": str, "models": {name: (in_dec, out_dec)}}。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception as e:   # noqa: BLE001
        raise ValueError(f"{PRICE_TABLE_ENV} 不是合法 JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError(f"{PRICE_TABLE_ENV} 顶层必须是对象")
    version = obj.get("version")
    if not isinstance(version, str) or not version.strip() or len(version.strip()) > 32:
        raise ValueError(f"{PRICE_TABLE_ENV}.version 必须是非空字符串（≤32，落 057 列）")
    if not isinstance(obj.get("effective_from"), str) or not obj["effective_from"].strip():
        raise ValueError(f"{PRICE_TABLE_ENV}.effective_from 必须是非空字符串（审计锚点）")
    models = obj.get("models")
    if not isinstance(models, dict) or not models:
        raise ValueError(f"{PRICE_TABLE_ENV}.models 必须是非空对象")
    parsed: dict = {}
    for name, prices in models.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{PRICE_TABLE_ENV}.models 含空模型名")
        if not isinstance(prices, dict):
            raise ValueError(f"{PRICE_TABLE_ENV}.models[{name!r}] 必须是对象")
        pair = []
        for key in ("in_rmb_per_mtok", "out_rmb_per_mtok"):
            v = prices.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                raise ValueError(f"{PRICE_TABLE_ENV}.models[{name!r}].{key} 必须是数")
            try:
                d = Decimal(str(v))
            except (InvalidOperation, ValueError) as e:
                raise ValueError(
                    f"{PRICE_TABLE_ENV}.models[{name!r}].{key} 不是合法数: {v!r}") from e
            if not d.is_finite() or d < 0:
                raise ValueError(
                    f"{PRICE_TABLE_ENV}.models[{name!r}].{key} 必须非负有限数（拒 NaN/Inf）")
            pair.append(d)
        parsed[name.strip()] = (pair[0], pair[1])
    return {"version": version.strip(), "models": parsed}


def _get_cached() -> Optional[dict]:
    """进程内缓存读取（env 原文变更即重解析——测试/热切换友好）。运行期坏表
    warn+当未配：fail-fast 归 load_config，记账面 fail-open。"""
    raw = (os.environ.get(PRICE_TABLE_ENV) or "").strip()
    with _lock:
        if _cache["raw"] == raw:
            return _cache["table"]
        try:
            table = parse_price_table(raw)
        except ValueError as e:
            logger.warning("价表运行期解析失败（记账按未配处理，启动校验在 load_config）: %s", e)
            table = None
        _cache["raw"], _cache["table"] = raw, table
        return table


def _reset_cache() -> None:
    with _lock:
        _cache["raw"], _cache["table"] = None, None


def estimate_cost(model: Optional[str],
                  tokens_prompt: Optional[int],
                  tokens_completion: Optional[int]) -> Tuple[Optional[Decimal], Optional[str]]:
    """回算一次调用成本。返回 (cost_estimate, price_table_version)：
    价表未配 / 未知 model / 无 token 数 → (None, None)——绝不编造。
    cost = (tp*in + tc*out)/1e6，Decimal quantize(0.0001, ROUND_HALF_UP)。"""
    table = _get_cached()
    if table is None or not model:
        return None, None
    prices = table["models"].get(str(model).strip())
    if prices is None:
        return None, None
    tp = int(tokens_prompt or 0)
    tc = int(tokens_completion or 0)
    if tp <= 0 and tc <= 0:
        return None, None                 # 无用量事实：不落 0 值污染成本聚合
    cost = ((Decimal(tp) * prices[0] + Decimal(tc) * prices[1])
            / Decimal(1_000_000)).quantize(_QUANT, rounding=ROUND_HALF_UP)
    return cost, table["version"]
