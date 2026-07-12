# -*- coding: utf-8 -*-
"""runner — 压测入口。

用法：
  python -m stress_harness.runner --scenario matrix --tier local --scale smoke
  python -m stress_harness.runner --scenario S2-on,S2-off --tier local --scale full

tier=local：本进程起 mock 后端 + 每场景拉起真 uvicorn 服务（env 按场景覆盖），
零外部成本。tier=staging：**不**起 mock/服务，只跑 S0 子集打真实端点——需要
STRESS_STAGING_ACK=I_UNDERSTAND_COSTS、STRESS_TARGET_URL（非回环）、
STRESS_TARGET_TOKEN 与 --budget-model-calls（硬中止）；执行手册见设计文档 §6。

退出码：0=全绿（draft 门 FAIL 不计）；2=任一非 draft 门 FAIL；3=运行错误。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from stress_harness.dbprobe import DBProbe
from stress_harness.gates import ScenarioResult, gate
from stress_harness.mockend import MockBackend, free_port
from stress_harness.report import write_report
from stress_harness.scenarios import SPECS, Rig
from stress_harness.serverctl import ServerCtl, build_env

SCOPE_NOTES_LOCAL = [
    "真实被测面：FastAPI/uvicorn 单 worker 服务、executor/loop/ModelGateway/tool 中间件栈、"
    "SSE 线协议、MySQL 持久化（agent_run/llm_call_log/tool_invocation/qa_session_log）、"
    "限流器、投机检索、reaper。",
    "替身面：DashScope chat（mock，OpenAI 兼容含流式 tool_calls）、DashScope embedding"
    "（mock）、检索走本地 OpenSearch 回退路径打 mock（生产为 HA3——HA3 引擎本身的容量"
    "不在本轮范围）。",
    "RAG_MAIN_HIT_REVALIDATE=false（mock 命中在权威表无行，复核会全丢弃）；"
    "该复核的 RDS 读放大未计入本轮 DB 压力。",
    "mock 模型时延为常数（默认 0.3s）：本报告的时延门是**管线开销门**，"
    "不是真实模型时延门——staging 档才给真实时延（见设计文档 §6）。",
]


def _matrix(names: str):
    if names == "matrix":
        return SPECS
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    by_id = {s.sid: s for s in SPECS}
    missing = [n for n in wanted if n not in by_id]
    if missing:
        raise SystemExit(f"未知场景: {missing}；可选: {[s.sid for s in SPECS]} 或 matrix")
    return [by_id[n] for n in wanted]


async def run_local(specs, scale: str, budget: int | None) -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(__file__).resolve().parent / "reports" / f"run_{ts}_wip"
    run_dir.mkdir(parents=True, exist_ok=True)

    mock = MockBackend().start()
    db = DBProbe()
    api_port = free_port()
    rig = Rig(mock=mock, db=db, api_port=api_port, run_dir=run_dir, scale=scale)
    results: list[ScenarioResult] = []
    llm_spent = 0
    try:
        for spec in specs:
            overrides = {k: v.replace("{mock}", f"http://127.0.0.1:{mock.port}")
                         for k, v in spec.env.items()}
            env = build_env(api_port, mock.port, overrides)
            server = ServerCtl(api_port, env, run_dir / "logs" / f"{spec.sid}.log")
            rig.server = server
            mock.reset_stats()
            mock.clear_faults()
            print(f"▶ {spec.sid} — {spec.title}（scale={scale}）", flush=True)
            t0 = time.monotonic()
            try:
                server.start()
                res = await spec.run(rig)
            except Exception as e:   # noqa: BLE001 — 单场景失败不拖垮矩阵
                res = rig.new_result(spec.sid, spec.title)
                res.duration_s = time.monotonic() - t0
                res.gates.append(gate(f"{spec.sid}-crashed", "场景执行异常", "无异常",
                                      f"{type(e).__name__}: {e}", False))
            finally:
                server.stop()
            llm_spent += mock.stats()["llm"]
            results.append(res)
            verdict = "FAIL" if res.hard_fail else "ok"
            print(f"  {spec.sid} 完成 {res.duration_s:.1f}s [{verdict}]", flush=True)
            if budget and llm_spent >= budget:
                print(f"⛔ 模型调用预算耗尽（{llm_spent}/{budget}），中止后续场景", flush=True)
                break
    finally:
        mock.stop()

    env_summary = {"tier": "local", "scale": scale, "api_port": api_port,
                   "mock_port": mock.port, "db_available": db.available,
                   "mock_llm_calls_total": llm_spent}
    outdir = write_report(results, f"local-{scale}", SCOPE_NOTES_LOCAL, env_summary)
    # 报告目录去掉 wip 名（日志并入正式目录）
    logs = run_dir / "logs"
    if logs.exists():
        logs.rename(outdir / "logs")
    try:
        run_dir.rmdir()
    except OSError:
        pass
    print(f"📄 报告: {outdir}/report.md", flush=True)
    return 2 if any(r.hard_fail for r in results) else 0


async def run_staging(budget: int | None) -> int:
    """staging 档：只跑 S0 基线子集打真实端点；硬预算中止。细则见设计文档 §6。"""
    ack = os.environ.get("STRESS_STAGING_ACK", "")
    target = os.environ.get("STRESS_TARGET_URL", "")
    token = os.environ.get("STRESS_TARGET_TOKEN", "")
    if ack != "I_UNDERSTAND_COSTS":
        raise SystemExit("staging 档需 STRESS_STAGING_ACK=I_UNDERSTAND_COSTS（真实计费）")
    if not target or "127.0.0.1" in target or "localhost" in target:
        raise SystemExit("STRESS_TARGET_URL 必须是非回环的 staging 端点")
    if not token:
        raise SystemExit("STRESS_TARGET_TOKEN 未设（用 staging 侧签名密钥铸的 Bearer）")
    if not budget:
        raise SystemExit("staging 档必须 --budget-model-calls N（硬中止护栏）")

    import httpx
    from stress_harness.gates import pctl
    from stress_harness.sse import ask_sse
    res = ScenarioResult(scenario="S0-staging", title="staging 真实端点基线",
                         started_at=datetime.now(timezone.utc).isoformat())
    spent = 0
    lat = []
    t0 = time.monotonic()
    async with httpx.AsyncClient() as client:
        for i in range(10):
            if spent >= budget:
                res.notes.append(f"预算 {budget} 耗尽，提前止步于 {i} 轮")
                break
            r = await ask_sse(client, f"{target.rstrip('/')}/api/agent/ask", token,
                              {"question": "如何查询车间设备点检记录？",
                               "session_id": f"stress-stg-{i}"}, timeout_s=300.0)
            lat.append(r.t_total_s)
            # done.usage 缺失时保守按 12 计（agent 上限一半）
            spent += max(2, 12 if not r.usage else 2)
            res.metrics[f"run_{i}"] = {"status": r.status, "total_s": r.t_total_s,
                                       "violations": r.order_violations}
    res.duration_s = time.monotonic() - t0
    res.metrics["total_p95_s"] = pctl([x for x in lat if x], 95)
    res.metrics["model_call_budget_spent_est"] = spent
    res.gates.append(gate("G4-staging", "run 总时长 p95（light 档）", "≤ 60s",
                          res.metrics["total_p95_s"],
                          (res.metrics["total_p95_s"] or 9e9) <= 60, draft=True))
    outdir = write_report([res], "staging-s0", ["staging 真实端点：无 mock，一切真实计费。"],
                          {"tier": "staging", "target": target, "budget": budget})
    print(f"📄 报告: {outdir}/report.md", flush=True)
    return 2 if res.hard_fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="agent 压测 runner")
    ap.add_argument("--scenario", default="matrix",
                    help="matrix 或逗号分隔场景 ID（如 S0,S2-on,S2-off）")
    ap.add_argument("--tier", choices=["local", "staging"], default="local")
    ap.add_argument("--scale", choices=["smoke", "full"], default="smoke")
    ap.add_argument("--budget-model-calls", type=int, default=None)
    args = ap.parse_args()
    try:
        if args.tier == "staging":
            return asyncio.run(run_staging(args.budget_model_calls))
        specs = _matrix(args.scenario)
        return asyncio.run(run_local(specs, args.scale, args.budget_model_calls))
    except KeyboardInterrupt:
        return 3


if __name__ == "__main__":
    sys.exit(main())
