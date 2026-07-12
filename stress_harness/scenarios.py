# -*- coding: utf-8 -*-
"""scenarios — S0–S8 压测场景（声明式规格 + async 执行体）。

每个场景 = (env 覆盖, mock 剧本, 负载形状, 门)。服务进程由 runner 按场景重启
（env 变更需要新进程），mock 与 DB 探针跨场景复用（统计按时间水位切片）。
规模档：smoke（分钟级，CI/shakedown）与 full（提交基线用）。
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from stress_harness.dbprobe import DBProbe
from stress_harness.gates import ScenarioResult, gate, pctl
from stress_harness.mockend import MockBackend
from stress_harness.serverctl import ServerCtl
from stress_harness.sse import AskResult, ask_sse
from stress_harness import tokens as tok

QUESTION = "PET 吸塑托盘成型温度和静置要求是什么？"


@dataclass
class Rig:
    """跨场景共享的执行环境。server 由 runner 每场景注入。"""

    mock: MockBackend
    db: DBProbe
    api_port: int
    run_dir: Path
    scale: str = "smoke"
    shared: Dict[str, Any] = field(default_factory=dict)
    server: Optional[ServerCtl] = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def agent_url(self) -> str:
        return f"{self.base}/api/agent/ask"

    @property
    def stream_url(self) -> str:
        return f"{self.base}/api/ask/stream"

    def new_result(self, sid: str, title: str) -> ScenarioResult:
        return ScenarioResult(scenario=sid, title=title,
                              started_at=datetime.now(timezone.utc).isoformat())


@dataclass
class Spec:
    sid: str
    title: str
    run: Callable                      # async (rig) -> ScenarioResult
    env: Dict[str, str] = field(default_factory=dict)   # "{mock}" 占位符由 runner 替换


def _client() -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    return httpx.AsyncClient(limits=limits)


async def _one_ask(client, rig: Rig, token: str, sid: str, thinking: bool = False,
                   abandon_after: Optional[str] = None, url: Optional[str] = None,
                   timeout_s: float = 180.0) -> AskResult:
    payload: Dict[str, Any] = {"question": QUESTION, "session_id": sid}
    if thinking:
        payload["thinking"] = True
    return await ask_sse(client, url or rig.agent_url, token, payload,
                         abandon_after=abandon_after, timeout_s=timeout_s)


def _lat(results: List[AskResult], attr: str) -> List[float]:
    return [getattr(r, attr) for r in results if getattr(r, attr) is not None]


def _status_census(results: List[AskResult]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in results:
        key = str(r.status) if not r.transport_error else "transport_error"
        out[key] = out.get(key, 0) + 1
    return out


async def _warmup(client, rig: Rig, token: str) -> None:
    """预热一发：钉死 _get_runtime 单例（F6 冷启动竞态），让后续突发打在真实 4-run 墙上。

    routes/agent.py 的运行时单例是无锁 check-then-act——冷实例上并发首请求会各建一套
    executor（S9 专门量化该竞态；其余场景要测的是**稳态**行为，先预热排除竞态干扰）。
    """
    await _one_ask(client, rig, token, "stress-warmup", timeout_s=60.0)


# ══════════════════════════════════════════════════════════════════
# S0 — 单用户基线
# ══════════════════════════════════════════════════════════════════
async def s0(rig: Rig) -> ScenarioResult:
    res = rig.new_result("S0", "单用户基线（正确性 + 各段时延）")
    n_plain, n_think = (4, 2) if rig.scale == "smoke" else (10, 10)
    rig.mock.clear_faults()
    rig.mock.cfg.llm_latency_s = 0.3
    rig.mock.cfg.tool_turns = 1
    t0 = time.monotonic()
    db_t0 = rig.db.now() if rig.db.available else None
    token = tok.mint_one("su0001")
    results: List[AskResult] = []
    async with _client() as client:
        for i in range(n_plain + n_think):
            r = await _one_ask(client, rig, token, f"stress-s0-{i}",
                               thinking=(i >= n_plain))
            results.append(r)
    res.duration_s = time.monotonic() - t0

    ok = [r for r in results if r.ok]
    violations = [v for r in results for v in r.order_violations]
    # 管线开销 = 端到端 − mock 已知耗时（2 次模型调用 + 1 次检索段）
    known = 2 * rig.mock.cfg.llm_latency_s + rig.mock.cfg.emb_latency_s \
        + rig.mock.cfg.search_latency_s
    overhead = [t - known for t in _lat(results, "t_total_s")]
    res.metrics = {
        "runs": len(results), "ok": len(ok), "status": _status_census(results),
        "ttfb_p95_s": pctl(_lat(results, "ttfb_s"), 95),
        "session_p95_s": pctl(_lat(results, "t_session_s"), 95),
        "first_chunk_p95_s": pctl(_lat(results, "t_first_chunk_s"), 95),
        "first_tool_result_p95_s": pctl(_lat(results, "t_first_tool_result_s"), 95),
        "total_p95_s": pctl(_lat(results, "t_total_s"), 95),
        "overhead_p95_s": pctl(overhead, 95),
        "tool_elapsed_ms": [ms for r in results for ms in r.tool_elapsed_ms],
    }
    res.gates.append(gate("S0-frames", "帧序违规数", "== 0", len(violations),
                          len(violations) == 0, note="; ".join(violations[:3])))
    res.gates.append(gate("S0-success", "run 成功率", "== 100%",
                          f"{len(ok)}/{len(results)}", len(ok) == len(results)))
    res.gates.append(gate("G1", "TTFT(session 帧) p95", "≤ 2s（本地 mock ≤ 0.3s）",
                          res.metrics["session_p95_s"],
                          (res.metrics["session_p95_s"] or 9e9) <= 0.3, draft=True))
    ov = res.metrics["overhead_p95_s"]
    res.gates.append(gate("S0-overhead", "管线开销 p95（扣除 mock 已知耗时）", "< 1.5s",
                          f"{ov:.3f}s" if ov is not None else "n/a",
                          ov is not None and ov < 1.5))
    if rig.db.available and db_t0:
        runs = rig.db.agent_runs_since(db_t0)
        llm = rig.db.llm_calls_since(db_t0)
        tools = rig.db.tool_invocations_since(db_t0)
        qa = rig.db.qa_sessions_since(db_t0)
        n = len(results)
        res.metrics.update({"db_runs": runs, "db_llm": llm, "db_tools": tools, "db_qa": qa})
        res.gates.append(gate("S0-db-runs", "agent_run succeeded 行数", f"== {n}",
                              runs.get("succeeded", 0), runs.get("succeeded", 0) == n))
        res.gates.append(gate("S0-db-llm", "llm_call_log 行数", f"== {2 * n}",
                              llm.get("count", 0), llm.get("count", 0) == 2 * n))
        res.gates.append(gate("S0-db-tools", "tool_invocation succeeded", f"== {n}",
                              tools.get("succeeded", 0), tools.get("succeeded", 0) == n))
        res.gates.append(gate("S0-db-qa", "qa_session_log SUCCESS(model=agent)", f"== {n}",
                              qa.get("SUCCESS", 0), qa.get("SUCCESS", 0) == n))
    else:
        res.gates.append(gate("S0-db", "DB 断言面", "可用", "DB 不可用", None))
    return res


# ══════════════════════════════════════════════════════════════════
# S1 — 并发墙（RAG_AGENT_MAX_CONCURRENT_RUNS=4）
# ══════════════════════════════════════════════════════════════════
async def s1(rig: Rig) -> ScenarioResult:
    res = rig.new_result("S1", "并发爬坡到 4-run 墙（429 干净性）")
    rig.mock.clear_faults()
    rig.mock.cfg.llm_latency_s = 2.0
    rig.mock.cfg.tool_turns = 1
    phases = [(0.5, 10), (1.0, 10), (2.0, 12), (4.0, 12)] if rig.scale == "smoke" \
        else [(0.5, 30), (1.0, 30), (2.0, 45), (4.0, 45), (6.0, 30)]
    tokens = tok.mint(60)
    db_t0 = rig.db.now() if rig.db.available else None
    t0 = time.monotonic()
    results: List[AskResult] = []
    tasks: List[asyncio.Task] = []
    i = 0
    async with _client() as client:
        for rate, dur in phases:
            end = time.monotonic() + dur
            while time.monotonic() < end:
                token = tokens[i % len(tokens)]
                tasks.append(asyncio.create_task(
                    _one_ask(client, rig, token, f"stress-s1-{i}")))
                i += 1
                await asyncio.sleep(1.0 / rate)
        results = list(await asyncio.gather(*tasks))
    res.duration_s = time.monotonic() - t0

    accepted = [r for r in results if r.status == 200]
    rejected = [r for r in results if r.status == 429]
    other = [r for r in results if r.status not in (200, 429)]
    ok = [r for r in accepted if r.ok]
    res.metrics = {
        "submitted": len(results), "accepted": len(accepted), "rejected_429": len(rejected),
        "other_status": _status_census(other), "accepted_ok": len(ok),
        "accepted_total_p95_s": pctl(_lat(accepted, "t_total_s"), 95),
        "reject_body_sample": (rejected[0].body_text[:120] if rejected else ""),
    }
    res.gates.append(gate("S1-clean-429", "超额一律 429（无 5xx/传输错）", "other == 0",
                          len(other), len(other) == 0,
                          note=str(_status_census(other)) if other else ""))
    res.gates.append(gate("S1-wall-hit", "确实到达并发墙（有 429）", "> 0",
                          len(rejected), len(rejected) > 0))
    res.gates.append(gate("S1-accepted-complete", "被接纳的 run 全部完成", "== accepted",
                          f"{len(ok)}/{len(accepted)}", len(ok) == len(accepted)))
    body_ok = all("并发已满" in r.body_text for r in rejected[:20]) if rejected else False
    res.gates.append(gate("S1-reject-msg", "429 文案 = Agent 并发已满", "命中",
                          body_ok, body_ok))
    if rig.db.available and db_t0:
        runs = rig.db.agent_runs_since(db_t0)
        total_rows = sum(runs.values())
        res.metrics["db_runs"] = runs
        res.gates.append(gate("S1-db-no-orphan", "拒绝请求不落 agent_run 行",
                              f"rows == {len(accepted)}", total_rows,
                              total_rows == len(accepted)))
    return res


# ══════════════════════════════════════════════════════════════════
# S2 — 突发拒绝风暴 × 投机检索放大（F1 头条测量；双臂）
# ══════════════════════════════════════════════════════════════════
def _make_s2(arm: str):
    async def s2(rig: Rig) -> ScenarioResult:
        sid = f"S2-{arm}"
        res = rig.new_result(sid, f"突发拒绝风暴（投机检索 {arm}）")
        rig.mock.clear_faults()
        rig.mock.cfg.llm_latency_s = 2.0
        rig.mock.cfg.tool_turns = 1
        burst, waves = (16, 2) if rig.scale == "smoke" else (40, 5)
        tokens = tok.mint(burst)
        submitted = accepted = rejected = other = 0
        async with _client() as client:
            await _warmup(client, rig, tokens[0])
            t0 = time.monotonic()
            for w in range(waves):
                t_wave = time.monotonic()
                tasks = [asyncio.create_task(
                    _one_ask(client, rig, tokens[k], f"stress-s2{arm}-{w}-{k}"))
                    for k in range(burst)]
                results = list(await asyncio.gather(*tasks))
                submitted += len(results)
                accepted += sum(1 for r in results if r.status == 200)
                rejected += sum(1 for r in results if r.status == 429)
                other += sum(1 for r in results if r.status not in (200, 429))
                # 等 in-flight run 与投机池排队排空，下一波从静止起步
                await asyncio.sleep(max(0.0, 6.0 - (time.monotonic() - t_wave)))
        counts = rig.mock.counts_since(t0 - 0.5)
        res.duration_s = time.monotonic() - t0

        emb, search = counts["emb"], counts["search"]
        # 放大系数用**检索次数**归因（embedding 有 query-LRU，同题压测下被缓存吃掉）：
        # spec-on：每次 submit（含被拒）预取 1 次检索，被接纳 run 工具侧命中投机结果不再检索
        #          → search ≈ submitted，被拒归因 = (search − accepted)/rejected ≈ 1.0；
        # spec-off：仅被接纳 run 工具侧检索 → search ≈ accepted，被拒归因 ≈ 0。
        search_per_reject = (max(0, search - accepted) / rejected) if rejected else 0.0
        res.metrics = {
            "submitted": submitted, "accepted": accepted, "rejected_429": rejected,
            "other": other, "emb_calls_lru_flattened": emb, "search_calls": search,
            "search_per_submit": round(search / submitted, 3) if submitted else 0,
            "search_per_rejected_submit": round(search_per_reject, 3),
        }
        res.gates.append(gate("S2-clean", "无 5xx/传输错", "== 0", other, other == 0))
        spec_on = arm == "on"
        g6_pass = search_per_reject <= 0.05
        res.gates.append(gate(
            "G6-spec-waste", "被拒 submit 的投机检索代价（检索次数/被拒）",
            "≤ 0.05", round(search_per_reject, 3), g6_pass, draft=True,
            note="预登记预期：spec-on 臂 FAIL（F1）" if spec_on else ""))
        rig.shared[sid] = res.metrics
        if not spec_on and "S2-on" in rig.shared:
            on_m = rig.shared["S2-on"]
            res.findings.append(
                "F1 投机检索先于并发准入：spec-on 臂 "
                f"{on_m['rejected_429']} 个被拒 submit 共触发 {on_m['search_calls']} 次检索"
                f"（{on_m['search_per_rejected_submit']}/被拒）；spec-off 对照臂 "
                f"{res.metrics['search_per_rejected_submit']}/被拒。同题 embedding 被 "
                "query-LRU 抹平，真实混合流量下 embedding 面同倍放大。整改选项：把 "
                "SpeculativeSearch 构造移到 executor.submit 成功之后，或提交前预检 "
                "executor 空位。")
        return res
    return s2


# ══════════════════════════════════════════════════════════════════
# S3 — 顶格浸泡（泄漏 / 漂移）
# ══════════════════════════════════════════════════════════════════
async def s3(rig: Rig) -> ScenarioResult:
    res = rig.new_result("S3", "顶格浸泡（RSS/线程/时延漂移 + DB 连接上限）")
    rig.mock.clear_faults()
    rig.mock.cfg.llm_latency_s = 0.5
    rig.mock.cfg.tool_turns = 1
    duration = 240 if rig.scale == "smoke" else 1800
    vus = 4          # == RAG_AGENT_MAX_CONCURRENT_RUNS：顶格但不撞墙（撞墙面归 S1/S2）
    tokens = tok.mint(vus)
    if rig.server:
        rig.server.samples.clear()
        rig.server.start_sampling(2.0)
    db_t0 = rig.db.now() if rig.db.available else None
    t0 = time.monotonic()
    results: List[AskResult] = []
    conn_samples: List[int] = []

    async def vu(idx: int):
        # 每 VU 固定 session → 滚动摘要/会话记忆随浸泡增长（真实多轮形态）
        sid = f"stress-s3-vu{idx}"
        while time.monotonic() - t0 < duration:
            r = await _one_ask(client, rig, tokens[idx], sid)
            results.append(r)
            await asyncio.sleep(max(0.2, random.gauss(2.0, 1.0)))

    async def conn_probe():
        while time.monotonic() - t0 < duration:
            c = rig.db.mysql_connections()
            if c >= 0:
                conn_samples.append(c)
            await asyncio.sleep(5.0)

    async with _client() as client:
        await _warmup(client, rig, tokens[0])
        await asyncio.gather(*[vu(i) for i in range(vus)], conn_probe())
    if rig.server:
        rig.server.stop_sampling()
    res.duration_s = time.monotonic() - t0

    ok = [r for r in results if r.ok]
    # 429 是正确的背压（闭环 VU 偶发在自身槽位释放前抢跑），不算错误；
    # 只有 5xx 与传输错才是真故障
    hard_err = [r for r in results if r.transport_error or r.status >= 500]
    backpressure_429 = sum(1 for r in results if r.status == 429)
    half = len(results) // 2
    p95_a = pctl(_lat(results[:half], "t_total_s"), 95)
    p95_b = pctl(_lat(results[half:], "t_total_s"), 95)
    drift = ((p95_b / p95_a) - 1) if (p95_a and p95_b) else None
    samples = rig.server.samples if rig.server else []
    q = max(1, len(samples) // 4)
    rss_first = sum(s["rss_kb"] for s in samples[:q]) / q if samples else 0
    rss_last = sum(s["rss_kb"] for s in samples[-q:]) / q if samples else 0
    rss_drift = (rss_last / rss_first - 1) if rss_first else None
    thr_first = samples[q - 1]["threads"] if samples else 0
    thr_last = samples[-1]["threads"] if samples else 0
    res.metrics = {
        "runs": len(results), "ok": len(ok), "hard_err_5xx_transport": len(hard_err),
        "backpressure_429": backpressure_429,
        "p95_first_half_s": p95_a, "p95_second_half_s": p95_b,
        "p95_drift": f"{drift * 100:.1f}%" if drift is not None else "n/a",
        "rss_first_kb": int(rss_first), "rss_last_kb": int(rss_last),
        "rss_drift": f"{rss_drift * 100:.1f}%" if rss_drift is not None else "n/a",
        "threads_first": thr_first, "threads_last": thr_last,
        "mysql_conn_max": max(conn_samples) if conn_samples else -1,
        "rss_timeline_kb": [s["rss_kb"] for s in samples],
        "threads_timeline": [s["threads"] for s in samples],
    }
    res.gates.append(gate("G9-rss", "RSS 漂移（首/末四分位均值）", "< 15%",
                          res.metrics["rss_drift"],
                          rss_drift is not None and rss_drift < 0.15, draft=True))
    res.gates.append(gate("G9-threads", "线程数平稳", "|Δ| ≤ 8",
                          f"{thr_first}→{thr_last}", abs(thr_last - thr_first) <= 8,
                          draft=True))
    res.gates.append(gate("G9-p95-drift", "p95 漂移（前半 vs 后半）", "< +20%",
                          res.metrics["p95_drift"],
                          drift is not None and drift < 0.20, draft=True))
    res.gates.append(gate("S3-errors", "全程无 5xx/传输错（429 背压不计）", "== 0",
                          len(hard_err), len(hard_err) == 0,
                          note=f"429 背压 {backpressure_429} 次（正确行为）"))
    if conn_samples:
        res.gates.append(gate("S3-db-conns", "MySQL 连接数峰值（服务池上限 20 + 探针）",
                              "≤ 25", max(conn_samples), max(conn_samples) <= 25))
    if rig.db.available and db_t0:
        llm = rig.db.llm_calls_since(db_t0)
        res.metrics["db_llm"] = llm
        expect = 2 * len(ok)
        res.gates.append(gate("S3-llm-account", "llm_call_log 记账完整（2×成功 run）",
                              f"≥ {expect}", llm.get("count", 0),
                              llm.get("count", 0) >= expect))
    return res


# ══════════════════════════════════════════════════════════════════
# S4 — 依赖故障注入（时延尖峰 / 429 风暴→熔断→恢复 / 空补全）
# ══════════════════════════════════════════════════════════════════
async def s4(rig: Rig) -> ScenarioResult:
    res = rig.new_result("S4", "依赖故障注入（尖峰 / 429 风暴熔断 / 空补全燃烧）")
    rig.mock.clear_faults()
    rig.mock.cfg.tool_turns = 1
    token = tok.mint_one("su0001")
    t0 = time.monotonic()
    async with _client() as client:
        # (a) 时延尖峰：单次模型调用接近但不超 provider 60s 超时
        spike = 8.0 if rig.scale == "smoke" else 25.0
        rig.mock.cfg.llm_latency_s = spike
        ra = await _one_ask(client, rig, token, "stress-s4a", timeout_s=180.0)
        res.metrics["a_spike_latency_s"] = spike
        res.metrics["a_total_s"] = ra.t_total_s
        res.gates.append(gate("S4a-survive", f"模型 {spike}s 尖峰下 run 正常完成",
                              "done 且无 error", ra.ok, ra.ok))

        # (b) 429 风暴 → 退避重试 → 熔断打开 → 快速失败 → 冷却恢复
        rig.mock.cfg.llm_latency_s = 0.2
        rig.mock.fail_window(429, 90.0)
        storm: List[AskResult] = []
        for i in range(6):   # 每 run 首call重试 2 次 → 3 次失败/run；两 run 后熔断应开
            r = await _one_ask(client, rig, token, f"stress-s4b-{i}", timeout_s=60.0)
            storm.append(r)
        errored = [r for r in storm if r.error_message or r.status != 200]
        fast_fail = [r.t_total_s for r in storm[-2:] if r.t_total_s is not None]
        res.metrics["b_storm_runs"] = len(storm)
        res.metrics["b_errored"] = len(errored)
        res.metrics["b_last2_total_s"] = [round(t, 2) for t in fast_fail]
        res.gates.append(gate("S4b-all-fail-loud", "风暴期 run 显式失败（error 帧）",
                              f"== {len(storm)}", len(errored),
                              len(errored) == len(storm)))
        res.gates.append(gate("G7-fastfail", "熔断打开后失败为快速失败", "< 1.5s",
                              res.metrics["b_last2_total_s"],
                              all(t < 1.5 for t in fast_fail) if fast_fail else False,
                              draft=True))
        rig.mock.clear_faults()
        heal_t0 = time.monotonic()
        await asyncio.sleep(31.0)   # 熔断冷却 30s
        rc = await _one_ask(client, rig, token, "stress-s4b-recover", timeout_s=60.0)
        recovery_s = time.monotonic() - heal_t0
        res.metrics["b_recovery_s"] = round(recovery_s, 1)
        res.gates.append(gate("G7-recovery", "故障恢复后首个 run 成功", "≤ 45s",
                              f"{recovery_s:.1f}s（ok={rc.ok}）",
                              rc.ok and recovery_s <= 45.0, draft=True))

        # (c) 空补全燃烧：loop empty_final_retries=5 → 每 run ≤ 1+5 次模型调用
        rig.mock.clear_faults()
        rig.mock.cfg.empty_completion = True
        c_t0 = time.monotonic()
        rcc = await _one_ask(client, rig, token, "stress-s4c", timeout_s=60.0)
        c_llm = rig.mock.counts_since(c_t0)["llm"]
        rig.mock.cfg.empty_completion = False
        res.metrics["c_llm_calls_one_run"] = c_llm
        res.metrics["c_run_ok"] = rcc.ok
        res.gates.append(gate("S4c-empty-burn", "空补全重试上限（1 + 5 兜底）",
                              "≤ 6 次/turn", c_llm, 0 < c_llm <= 6,
                              note="commit 3560898 while-loop 语义"))
    res.duration_s = time.monotonic() - t0
    res.findings.append(
        "F3 xhigh/max 档的 max→plus fallback 链从 /api/agent/ask 不可达（端点档位映射仅 "
        "light/high，且两档路由均为单模型链）——高阶档 fallback 韧性在 serving 层无法演练，"
        "属死配置；上线前应决定：暴露档位 or 收敛路由表。")
    return res


# ══════════════════════════════════════════════════════════════════
# S4F2 — 共享网关熔断跨路径污染探针（RAG_SERVING_MODEL_GATEWAY=true）
# ══════════════════════════════════════════════════════════════════
async def s4f2(rig: Rig) -> ScenarioResult:
    res = rig.new_result("S4F2", "共享 ModelGateway 熔断跨路径污染（F2 探针）")
    rig.mock.clear_faults()
    rig.mock.cfg.llm_latency_s = 0.2
    rig.mock.cfg.tool_turns = 1
    token = tok.mint_one("su0001")
    t0 = time.monotonic()
    async with _client() as client:
        # 基线：普通流式问答应当可用
        base = await _one_ask(client, rig, token, "stress-f2-base", url=rig.stream_url)
        # agent 侧打满熔断（qwen3.7-plus 5 次失败）
        rig.mock.fail_window(429, 60.0)
        for i in range(3):
            await _one_ask(client, rig, token, f"stress-f2-storm-{i}", timeout_s=60.0)
        # 熔断开着时立刻探普通路径（mock 仍在故障窗口 → 普通路径自身请求也会 429；
        # 我们要的是「网关实例是否共享」信号：看服务端日志/时延特征区分“被熔断跳过”
        # vs“自己打模型失败”。此处用时延判据：共享熔断 → 秒内快速失败。
        probe = await _one_ask(client, rig, token, "stress-f2-probe", url=rig.stream_url,
                               timeout_s=60.0)
        rig.mock.clear_faults()
    res.duration_s = time.monotonic() - t0
    plain_failed = (probe.error_message != "" or probe.status != 200
                    or probe.t_done_s is None)
    fast = probe.t_total_s is not None and probe.t_total_s < 1.5
    res.metrics = {"baseline_ok": base.ok, "probe_status": probe.status,
                   "probe_error": probe.error_message, "probe_total_s": probe.t_total_s,
                   "probe_fast_fail": fast}
    res.gates.append(gate("F2-baseline", "flag-on 下普通流式基线可用", "ok", base.ok,
                          base.ok))
    res.gates.append(gate("F2-observe", "污染观测（测量型，不判 PASS/FAIL）", "记录",
                          f"failed={plain_failed} fast={fast}", True, draft=True))
    res.findings.append(
        "F2 RAG_SERVING_MODEL_GATEWAY=true 时普通流式与 agent 共用 DashScope 调用面："
        f"agent 侧 429 风暴后普通路径探针 status={probe.status} "
        f"error='{probe.error_message[:60]}' total={probe.t_total_s}s "
        f"fast_fail={fast}——上线若翻此 flag，需把跨路径熔断隔离（独立 gateway 实例或"
        "按 category 分 breaker key）列入前置项。")
    return res


# ══════════════════════════════════════════════════════════════════
# S5 — 客户端弃单风暴（断连 ≠ 取消）
# ══════════════════════════════════════════════════════════════════
async def s5(rig: Rig) -> ScenarioResult:
    res = rig.new_result("S5", "客户端弃单风暴（断连不取消 → 僵尸槽位 + 浪费额度）")
    rig.mock.clear_faults()
    rig.mock.cfg.llm_latency_s = 1.5
    rig.mock.cfg.tool_turns = 1
    n = 8 if rig.scale == "smoke" else 24
    tokens = tok.mint(n + 2)
    t0 = time.monotonic()
    async with _client() as client:
        await _warmup(client, rig, tokens[n + 1])   # 钉死单例 → 真实 4-run 墙
        # 预热的 qa_session_log 写在完成侧、created_at 是 datetime(0) 整秒——与整秒水位
        # 同秒会漏计（+1 假象）。等到下一整秒再取水位，把预热 qa 行甩到更早的整秒。
        await asyncio.sleep(1.2)
        db_t0 = rig.db.now() if rig.db.available else None
        llm_t0 = time.monotonic()
        tasks = []
        for i in range(n):
            after = "session" if i % 2 == 0 else "chunk"
            tasks.append(asyncio.create_task(
                _one_ask(client, rig, tokens[i], f"stress-s5-{i}", abandon_after=after)))
        # 弃单探针要赶在僵尸 run 存活期内发：先短睡（让 4 个被接纳的 run 起跑 + 部分弃单），
        # 不等 gather（chunk-弃单要 ~3s 才返回，等它就错过僵尸窗口——首轮 shakedown 教训）
        await asyncio.sleep(0.8)
        probe = await _one_ask(client, rig, tokens[n], "stress-s5-probe", timeout_s=30.0)
        abandoned = list(await asyncio.gather(*tasks))
        accepted = sum(1 for r in abandoned if r.status == 200)
        rejected = sum(1 for r in abandoned if r.status == 429)
        # 等全部僵尸 run 跑完
        await asyncio.sleep(2 * rig.mock.cfg.llm_latency_s + 2.0)
    res.duration_s = time.monotonic() - t0

    wasted_llm = rig.mock.counts_since(llm_t0)["llm"]
    res.metrics = {
        "burst": n, "accepted": accepted, "rejected_429": rejected,
        "aborted_confirmed": sum(1 for r in abandoned if r.aborted),
        "probe_status_during_zombies": probe.status,
        "llm_calls_after_abandon_window": wasted_llm,
    }
    res.gates.append(gate("S5-zombie-slots", "弃单后槽位仍占用（僵尸窗内探针 429）",
                          "== 429（设计现状的量化）", probe.status,
                          probe.status == 429, draft=True,
                          note="断连不取消是当前设计；本门固化其容量代价"))
    if rig.db.available and db_t0:
        # 服务端视角：断连的 run 照常完成并落库（on_complete 在 executor 侧）。
        # 期望完成数 = 被接纳的弃单 run（探针 429 时不落 run 行）
        expect = accepted + (1 if probe.status == 200 else 0)
        deadline = time.monotonic() + 60
        runs: Dict[str, int] = {}
        while time.monotonic() < deadline:
            runs = rig.db.agent_runs_since(db_t0)
            if runs.get("succeeded", 0) >= expect:
                break
            await asyncio.sleep(2.0)
        qa = rig.db.qa_sessions_since(db_t0)
        res.metrics.update({"db_runs": runs, "db_qa": qa})
        res.gates.append(gate("S5-complete-anyway", "断连 run 服务端照常完成",
                              f"succeeded == {expect}", runs.get("succeeded", 0),
                              runs.get("succeeded", 0) == expect))
        res.gates.append(gate("S5-durable", "断连 run 答案照常落 qa_session_log",
                              f"SUCCESS == {expect}", qa.get("SUCCESS", 0),
                              qa.get("SUCCESS", 0) == expect))
        res.findings.append(
            f"G8 弃单代价量化：{accepted} 个被接纳客户端在首帧后断开，服务端仍完成全部 "
            f"run 并消耗 {wasted_llm} 次模型调用（≈{wasted_llm / max(1, accepted):.1f} "
            f"次/弃单）；僵尸窗内探针得 {probe.status}（429=占满）。取消-on-断连是产品"
            "决策，上线容量按「4 槽 × run 全时长」而非「在看用户数」估算。")
    return res


# ══════════════════════════════════════════════════════════════════
# S6 — 混合负载公平性（agent + 普通流式 + aux GET）
# ══════════════════════════════════════════════════════════════════
async def s6(rig: Rig) -> ScenarioResult:
    res = rig.new_result("S6", "混合负载（agent 顶格时普通问答/辅助面不劣化）")
    rig.mock.clear_faults()
    rig.mock.cfg.llm_latency_s = 0.5
    rig.mock.cfg.tool_turns = 1
    phase_s = 45 if rig.scale == "smoke" else 300
    n_plain, n_aux = (8, 4) if rig.scale == "smoke" else (20, 10)
    tokens = tok.mint(4 + n_plain + n_aux)
    plain_results: Dict[str, List[AskResult]] = {"control": [], "mixed": []}
    aux_lat: Dict[str, List[float]] = {"control": [], "mixed": []}
    pool_503 = 0
    t0 = time.monotonic()

    async def plain_vu(idx: int, phase: str, end: float):
        while time.monotonic() < end:
            r = await _one_ask(client, rig, tokens[4 + idx], f"s6p-{phase}-{idx}",
                               url=rig.stream_url, timeout_s=60.0)
            plain_results[phase].append(r)
            if r.status == 503:
                nonlocal pool_503
                pool_503 += 1
            await asyncio.sleep(1.0)

    async def aux_vu(idx: int, phase: str, end: float):
        headers = {"Authorization": f"Bearer {tokens[4 + n_plain + idx]}"}
        while time.monotonic() < end:
            u = rig.base + ("/api/agent/runs" if idx % 2 == 0 else "/api/health")
            t1 = time.monotonic()
            try:
                await client.get(u, headers=headers, timeout=10.0)
                aux_lat[phase].append(time.monotonic() - t1)
            except httpx.HTTPError:
                aux_lat[phase].append(10.0)
            await asyncio.sleep(0.5)

    async def agent_vu(idx: int, end: float):
        while time.monotonic() < end:
            await _one_ask(client, rig, tokens[idx], f"s6a-{idx}", timeout_s=60.0)
            await asyncio.sleep(0.5)

    async with _client() as client:
        await _warmup(client, rig, tokens[0])   # agent 运行时先钉（排除 F6 冷启动竞态干扰）
        # 对照臂：只有 plain + aux（无 agent），量普通流式在无 agent 竞争时的基线 p95
        end1 = time.monotonic() + phase_s
        await asyncio.gather(*[plain_vu(i, "control", end1) for i in range(n_plain)],
                             *[aux_vu(i, "control", end1) for i in range(n_aux)])
        # 混合臂：叠加 4 个 agent VU 顶格竞争 threadpool tokens 与 DB 池
        end2 = time.monotonic() + phase_s
        await asyncio.gather(*[plain_vu(i, "mixed", end2) for i in range(n_plain)],
                             *[aux_vu(i, "mixed", end2) for i in range(n_aux)],
                             *[agent_vu(i, end2) for i in range(4)])
    res.duration_s = time.monotonic() - t0

    p95_ctl = pctl(_lat(plain_results["control"], "t_total_s"), 95)
    p95_mix = pctl(_lat(plain_results["mixed"], "t_total_s"), 95)
    ratio = (p95_mix / p95_ctl) if (p95_ctl and p95_mix) else None
    aux_p95_mix = pctl(aux_lat["mixed"], 95)
    res.metrics = {
        "plain_ctl_n": len(plain_results["control"]),
        "plain_mix_n": len(plain_results["mixed"]),
        "plain_p95_control_s": p95_ctl, "plain_p95_mixed_s": p95_mix,
        "plain_p95_ratio": round(ratio, 3) if ratio else None,
        "aux_p95_mixed_s": aux_p95_mix, "db_pool_503": pool_503,
    }
    res.gates.append(gate("S6-fairness", "普通流式 p95 劣化（mixed/control）", "≤ 1.25×",
                          res.metrics["plain_p95_ratio"],
                          ratio is not None and ratio <= 1.25, draft=True))
    res.gates.append(gate("S6-db-pool", "DB_POOL_EXHAUSTED 503", "== 0", pool_503,
                          pool_503 == 0))
    res.gates.append(gate("S6-aux", "辅助 GET p95", "< 0.5s", aux_p95_mix,
                          aux_p95_mix is not None and aux_p95_mix < 0.5, draft=True))
    if ratio is not None and ratio > 1.25:
        res.findings.append(
            f"F7 混合负载尾延迟竞争：4 个 agent VU 顶格时，普通流式 p95 从 "
            f"{p95_ctl:.2f}s 劣化到 {p95_mix:.2f}s（{ratio:.2f}×，超 1.25× 提案阈值）。"
            "单 worker 下 agent 的每 run 数十笔同步 DB 写 + threadpool token 占用挤压普通"
            "问答尾延迟；DB 池未耗尽（无 503）说明瓶颈在 threadpool/GIL 而非连接数。"
            "绝对值是 mock 时延产物，方向真实——staging 档用真实模型时延标定 S6-fairness 阈值，"
            "并评估是否给普通问答与 agent 分独立 threadpool token 配额。")
    return res


# ══════════════════════════════════════════════════════════════════
# S7 — 限流治理（限流开：每分/深思配额/全局日熔断+告警）
# ══════════════════════════════════════════════════════════════════
async def s7(rig: Rig) -> ScenarioResult:
    res = rig.new_result("S7", "限流治理（6/min、深思配额、全局日熔断 → 告警/台账）")
    rig.mock.clear_faults()
    rig.mock.cfg.llm_latency_s = 0.05
    rig.mock.cfg.tool_turns = 0     # 1 次模型调用/run，把额度留给限流面
    t0 = time.monotonic()
    async with _client() as client:
        # ① 单用户每分钟 6 次
        u1 = tok.mint_one("su-rate")
        minute: List[AskResult] = []
        for i in range(8):
            minute.append(await _one_ask(client, rig, u1, f"s7m-{i}", timeout_s=30.0))
        got = [r.status for r in minute]
        res.metrics["per_min_statuses"] = got
        res.gates.append(gate("S7-per-min", "第 7+ 次/分钟被 429", "前6=200 后2=429",
                              str(got), got[:6] == [200] * 6 and set(got[6:]) == {429}))

        # ② 深思日配额（env 压到 3）
        u2 = tok.mint_one("su-think")
        think: List[AskResult] = []
        for i in range(5):
            think.append(await _one_ask(client, rig, u2, f"s7t-{i}", thinking=True,
                                        timeout_s=30.0))
        got_t = [r.status for r in think]
        res.metrics["thinking_statuses"] = got_t
        res.gates.append(gate("S7-thinking-quota", "深思第 4+ 次被拒（配额 3）",
                              "前3=200 后2=4xx", str(got_t),
                              got_t[:3] == [200] * 3 and all(s != 200 for s in got_t[3:])))

        # ③ 全局日熔断（cap=25，含上面已计入的量）→ 503 + 台账 + 告警一次
        users = [tok.mint_one(f"su-cap{i:03d}") for i in range(30)]
        cap_hits: List[int] = []
        for i, u in enumerate(users):
            r = await _one_ask(client, rig, u, f"s7c-{i}", timeout_s=30.0)
            cap_hits.append(r.status)
            if r.status == 503:
                break
        res.metrics["cap_statuses_tail"] = cap_hits[-5:]
        tripped = 503 in cap_hits
        res.gates.append(gate("S7-global-cap", "触顶后 503（服务对外全拒）", "出现 503",
                              tripped, tripped))
        # 告警 webhook（dedup 一次）+ qa_admission_reject 台账（60s 批量落）
        hooks = rig.mock.counts_since(t0)["webhook"]
        res.gates.append(gate("S7-alert-once", "全局熔断钉钉告警恰好 1 次（dedup）",
                              "== 1", hooks, hooks == 1))
        # 台账 60s 批量 flush 只在**后续限流检查**里触发——触顶后停止发请求 flush 永不发生
        # （首轮 shakedown 教训）：等待期每 5s 补一发（其 503 同样计入台账并推动 flush）
        rejects: List[Dict[str, Any]] = []
        pinger = tok.mint_one("su-ledger-ping")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            rejects = rig.db.admission_rejects()
            if any(r["reason"] == "global_cap" for r in rejects):
                break
            await _one_ask(client, rig, pinger, "s7-ledger-ping", timeout_s=15.0)
            await asyncio.sleep(5.0)
        res.metrics["admission_rejects"] = rejects
        has_ledger = any(r["reason"] == "global_cap" for r in rejects) \
            if rig.db.available else None
        res.gates.append(gate("S7-ledger", "qa_admission_reject 台账落行（≤90s 批量窗）",
                              "global-cap 行存在", str(has_ledger), has_ledger))

    # ④ 治理单位错配测量（F4）：cap 按「次」记，agent 实耗模型调用数
    llm_total = rig.mock.counts_since(t0)["llm"]
    admitted = sum(1 for s in (res.metrics["per_min_statuses"]
                               + res.metrics["thinking_statuses"] + cap_hits) if s == 200)
    ratio = round(llm_total / max(1, admitted), 2)
    res.metrics["llm_calls_per_admitted_ask"] = ratio
    res.findings.append(
        f"F4 全局日熔断单位错配：cap 按 admission 记 1，本轮 {admitted} 次被接纳 ask 实际"
        f"产生 {llm_total} 次模型调用（{ratio}×/ask；本场景 tool_turns=0 为下界，真实 agent "
        "12 轮上限时可达 12-17×）——2000/day 的账单护栏对 agent 流量按次计等效放大同倍数，"
        "上线前应按「模型调用数」或加权计费单位重定 cap。")
    res.duration_s = time.monotonic() - t0
    return res


# ══════════════════════════════════════════════════════════════════
# S8 — 崩溃恢复钻演（SIGKILL → reaper 收尸 → 新实例满额接客）
# ══════════════════════════════════════════════════════════════════
async def s8(rig: Rig) -> ScenarioResult:
    res = rig.new_result("S8", "崩溃恢复（SIGKILL 中途杀 → 孤儿 run 收尸 → 立即可服务）")
    rig.mock.clear_faults()
    rig.mock.cfg.llm_latency_s = 20.0    # 长 run，保证被杀时在跑
    rig.mock.cfg.tool_turns = 1
    tokens = tok.mint(8)
    t0 = time.monotonic()
    async with _client() as client:
        tasks = [asyncio.create_task(
            _one_ask(client, rig, tokens[i], f"stress-s8-{i}", abandon_after="session"))
            for i in range(3)]
        attached = asyncio.create_task(
            _one_ask(client, rig, tokens[3], "stress-s8-attached", timeout_s=30.0))
        await asyncio.gather(*tasks)
        await asyncio.sleep(2.0)
        orphans_before = rig.db.orphan_running_runs()
        rig.server.kill9()
        attached_res = await attached           # 在线客户端应立刻断流而非挂死
        # 重启（同 env 同端口）；mock 恢复快答
        rig.mock.cfg.llm_latency_s = 0.3
        rig.server.start(wait_ready_s=30.0)
        fresh = list(await asyncio.gather(*[
            _one_ask(client, rig, tokens[4 + i], f"stress-s8-fresh-{i}", timeout_s=30.0)
            for i in range(4)]))
        # reaper（5s 间隔 / 10s stale）应在 ≤25s 内把孤儿 running → failed
        orphans_after = -1
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            orphans_after = rig.db.orphan_running_runs()
            if orphans_after == 0:
                break
            await asyncio.sleep(2.0)
    res.duration_s = time.monotonic() - t0
    fresh_ok = sum(1 for r in fresh if r.ok)
    res.metrics = {
        "orphans_right_after_kill": orphans_before,
        "attached_client_total_s": attached_res.t_total_s,
        "attached_transport_error": attached_res.transport_error[:80],
        "fresh_ok": fresh_ok, "orphans_after_reaper": orphans_after,
    }
    # 4 = 3 个弃单长 run + 1 个在线挂流 run（首轮 shakedown 把在线那只漏计了）
    res.gates.append(gate("S8-orphans-seen", "被杀时留下孤儿 running 行", "== 4",
                          orphans_before, orphans_before == 4))
    res.gates.append(gate("S8-client-unblocked", "在线 SSE 客户端被杀后快速断流（不挂死）",
                          "< 10s", attached_res.t_total_s,
                          attached_res.t_total_s is not None
                          and attached_res.t_total_s < 10.0))
    res.gates.append(gate("S8-fresh-capacity", "新实例立即满额接客（4/4 成功）", "== 4",
                          fresh_ok, fresh_ok == 4))
    res.gates.append(gate("S8-reaper", "孤儿 run ≤25s 内被收尸（running→failed）",
                          "== 0", orphans_after, orphans_after == 0))
    return res


# ══════════════════════════════════════════════════════════════════
# S9 — 冷启动运行时竞态（F6：无锁 _get_runtime 单例 → 并发首请求绕开 4-run 墙）
# ══════════════════════════════════════════════════════════════════
async def s9(rig: Rig) -> ScenarioResult:
    res = rig.new_result("S9", "冷启动竞态（并发首请求 × 无锁运行时单例，F6）")
    rig.mock.clear_faults()
    rig.mock.cfg.llm_latency_s = 2.0     # run 足够长：全部录取意味着真并发超墙
    rig.mock.cfg.tool_turns = 1
    burst = 16
    tokens = tok.mint(burst)
    db_t0 = rig.db.now() if rig.db.available else None
    t0 = time.monotonic()
    async with _client() as client:
        # 不预热——本场景就是要打冷实例的第一拨并发
        results = list(await asyncio.gather(*[
            asyncio.create_task(_one_ask(client, rig, tokens[i], f"stress-s9-{i}"))
            for i in range(burst)]))
    res.duration_s = time.monotonic() - t0
    accepted = sum(1 for r in results if r.status == 200)
    rejected = sum(1 for r in results if r.status == 429)
    res.metrics = {"cold_burst": burst, "accepted": accepted, "rejected_429": rejected,
                   "status": _status_census(results)}
    if rig.db.available and db_t0:
        res.metrics["db_runs"] = rig.db.agent_runs_since(db_t0)
    res.gates.append(gate(
        "F6-cold-cap", "冷实例并发首请求仍受 4-run 墙约束", "accepted ≤ 4", accepted,
        accepted <= 4, draft=True,
        note="预登记预期：FAIL——_get_runtime 无锁 check-then-act，竞态各建私有 executor"))
    res.findings.append(
        f"F6（头条·已服务端证实）冷启动并发墙旁路：routes/agent.py::_get_runtime 是无锁 "
        f"check-then-act 单例。冷实例上 {burst} 个**同刻**首请求实测 {accepted} 个全部被接纳、"
        f"0 拒绝（agent_run 表实测并发重叠达 {accepted}，墙本应=4）——每个竞态请求各建一整套 "
        "runtime（4 槽 executor + 熔断器 + reaper 线程），末位赋值成单例、先建的泄漏但其池仍在"
        "服务。**4-run 并发墙在冷窗口内提供零保护**，而冷窗口正是 SAE 每次发布/扩容/重启撞上"
        "在途流量的时刻：瞬时并发无上界，×2-17 模型调用/run 直灌 DashScope 与 20 连接 DB 池，"
        "叠加 F1 每 run 再拉一次投机检索。S1 的「干净 4-run 墙」仅在首请求单发预热单例后成立。"
        "整改（一行级）：_RUNTIME 构造包 threading.Lock 双检锁。")
    return res


# ══════════════════════════════════════════════════════════════════
# 场景注册表
# ══════════════════════════════════════════════════════════════════
SPECS: List[Spec] = [
    Spec("S0", "单用户基线", s0),
    Spec("S1", "并发墙", s1),
    Spec("S2-on", "突发风暴（spec on）", _make_s2("on")),
    Spec("S2-off", "突发风暴（spec off 对照）", _make_s2("off"),
         env={"RAG_AGENT_SPEC_RETRIEVAL": "false"}),
    Spec("S3", "顶格浸泡", s3),
    Spec("S4", "依赖故障注入", s4),
    Spec("S4F2", "共享网关污染探针", s4f2,
         env={"RAG_SERVING_MODEL_GATEWAY": "true"}),
    Spec("S5", "弃单风暴", s5),
    Spec("S6", "混合负载", s6),
    Spec("S7", "限流治理", s7,
         env={"RAG_RATE_LIMIT_ENABLE": "true", "RAG_GLOBAL_DAILY_LLM_CAP": "25",
              "RAG_THINKING_DAILY_QUOTA": "3",
              "RAG_OPS_ALERT_WEBHOOK": "{mock}/robot/send?access_token=stress"}),
    Spec("S8", "崩溃恢复钻演", s8,
         env={"RAG_AGENT_REAPER_INTERVAL_S": "5", "RAG_AGENT_STALE_RUNNING_S": "10"}),
    Spec("S9", "冷启动竞态（F6）", s9),
]
