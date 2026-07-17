# -*- coding: utf-8 -*-
"""
agent_worker.py — PR-3 Stage D：durable dispatch 独立 worker 进程入口

    python -m opensearch_pipeline.agent_worker

D2 拍板的「进程形态可后拆」在此兑现：worker 逻辑（读命令表→SKIP LOCKED 认领→重建
执行）与进程形态无关，本入口把 web 进程里的同一套件（runtime 四元组、reaper、
DurableDispatcher.run_forever）搬进无 HTTP 的前台进程——**代码零分叉**，复用
routes/agent 的单例与 `_dispatch_recover`（它掌握 ctx/回调重建），行为与 web 内嵌
恢复线程逐字节一致。多消费者安全：认领是 `FOR UPDATE SKIP LOCKED`，worker 与 web
副本的内嵌循环并存不冲突；要把恢复职责**集中**到 worker，给 web 副本设
`RAG_AGENT_DISPATCH_LOOP=off`（fast-path enqueue+inline dispatch 不受影响）。

职责面（=web 进程的控制面线程，一处不多一处不少）：
- dispatcher 恢复扫描（前台主线程，启动即先补欠账）；
- reaper（run 收尸/审批过期/TTL cross-heal/B6 对账/Stage C 自动对账——随 runtime 起）；
- SIGTERM/SIGINT → 恢复循环停 + executor 排水（`_drain_runtime`，与 SAE 滚动发布的
  优雅窗口同款）。

前置（fail-fast，起不来就喊）：RAG_AGENT_ENABLE + RAG_AGENT_DURABLE_DISPATCH 必开、
schema/043/044 已 apply；部署形态（SAE 第二应用/单副本 worker）user-gated。
"""
from __future__ import annotations

import logging
import signal
import threading

logger = logging.getLogger(__name__)


def _flag_on(name: str) -> bool:
    import os
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def build_worker():
    """装配 worker：(dispatcher, stop_event)。runtime/审批/reaper 随 routes/agent 单例
    建成（与 web 进程同一套构造路径——治理接线/工具注册/审计全生效）。测试可对
    routes.agent 的 _get_runtime/_get_dispatcher monkeypatch 后调本函数。"""
    if not _flag_on("RAG_AGENT_ENABLE"):
        raise RuntimeError("agent_worker 需要 RAG_AGENT_ENABLE=true（worker 只服务 agent 命令面）")
    if not _flag_on("RAG_AGENT_DURABLE_DISPATCH"):
        raise RuntimeError(
            "agent_worker 需要 RAG_AGENT_DURABLE_DISPATCH=true（无 outbox 无命令可认领；"
            "并确认 schema/043/044 已 apply）")
    from opensearch_pipeline.routes import agent as agent_route
    # _build_runtime 会顺带 _start_dispatcher()（web 的 daemon 形态）——worker 前台
    # 主循环就是恢复循环，先占位 _DISPATCHER_STARTED 抑制 daemon 双跑（SKIP LOCKED
    # 下双跑无害但无谓；抑制让日志/holder 单一可读）。reaper 照常随 runtime 起。
    agent_route._DISPATCHER_STARTED = True
    agent_route._get_runtime()
    dispatcher = agent_route._get_dispatcher()
    stop = threading.Event()
    return dispatcher, stop


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    dispatcher, stop = build_worker()

    def _graceful(signum, _frame):
        logger.warning("agent_worker 收到信号 %s——恢复循环停止，开始排水", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    logger.warning("agent_worker 启动（holder=%s，lease=%ss）——前台恢复循环",
                   dispatcher.holder, dispatcher.lease_s)
    try:
        dispatcher.run_forever(stop)          # 启动即先跑一轮补欠账，随后按间隔
    finally:
        from opensearch_pipeline.routes import agent as agent_route
        agent_route._drain_runtime()          # 幂等：在跑 run 限时等/兜底标失败
        logger.warning("agent_worker 已退出（排水完成）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
