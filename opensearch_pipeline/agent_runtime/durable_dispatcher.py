# -*- coding: utf-8 -*-
"""
durable_dispatcher.py — PR-3 Stage A：durable dispatch 执行器（fast-path + 恢复扫描）

命令生命周期（command-as-truth，设计 D1/D4）：
- **fast path**（请求线程内）：enqueue → claim_specific → 执行（建 run + 起驱动）→
  bind_run → complete(done)。命令的 done 语义=「dispatch 成功」（run 自身的生死由
  run 机器负责：心跳/reaper/完成事务——命令表不重复跟踪 run）。
- **恢复扫描**（后台线程，进程崩溃/滚动发布的兜底）：
  1) 已绑 run 但 lease 过期 → dispatch 其实已成功（run 存在）→ 越权收口 done；
  2) attempts 封顶且未绑 run → failed（last_error 留因）；
  3) claim_next（queued / lease 过期未绑 run）→ recover_fn 重建执行：
     - 成功（返回 run_id）→ bind + done；
     - DispatchRetryLater（ThreadBusy/容量/身份瞬断）→ 原样留 claimed，租约到期自然重试；
     - 其余异常 → failed（宁诚实失败不无限重驱）。

recover_fn 由 routes 层注入（它掌握 runtime 四元组与回调构造）；本模块不 import 任何
serving 组件——与 executor 的 B1 拓扑无关接缝同款，日后拆独立 worker 进程时原样搬。
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class DispatchRetryLater(RuntimeError):
    """本轮不可重建执行（ThreadBusy/容量满/身份解析瞬断）——命令留 claimed，
    租约到期后由下一轮扫描重驱（attempts 封顶兜底）。"""


def durable_dispatch_enabled() -> bool:
    return os.environ.get("RAG_AGENT_DURABLE_DISPATCH", "").strip().lower() in (
        "1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


class DurableDispatcher:
    """outbox 命令的认领与恢复。holder=进程实例 id（多副本时天然区分持有者）。"""

    def __init__(self, outbox, recover_fn: Callable[[Dict[str, Any]], Optional[str]],
                 *, holder: Optional[str] = None,
                 lease_s: Optional[int] = None,
                 max_attempts: Optional[int] = None):
        self._outbox = outbox
        self._recover_fn = recover_fn
        self.holder = (holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}")[:64]
        self.lease_s = lease_s if lease_s is not None else _int_env("RAG_AGENT_DISPATCH_LEASE_S", 120)
        self.max_attempts = (max_attempts if max_attempts is not None
                             else _int_env("RAG_AGENT_DISPATCH_MAX_ATTEMPTS", 3))

    # ── fast path（请求线程内）────────────────────────────────────
    def claim_for_request(self, command_id: str) -> bool:
        """enqueue 后同请求内认领。False=已被他方认领（理论并发角落）→ 调用方按直通降级。"""
        return bool(self._outbox.claim_specific(
            command_id, holder=self.holder, lease_s=self.lease_s))

    def bind_and_done(self, command_id: str, run_id: str) -> None:
        """执行已起跑：绑 run（at-most-once 分界）+ 收口 done。fail-open——
        这里任何失败都不碰已起跑的 run（恢复扫描会按「已绑/未绑」正确收敛）。"""
        try:
            self._outbox.bind_run(command_id, run_id, holder=self.holder)
            self._outbox.complete(command_id, status="done", holder=self.holder)
        except Exception:   # noqa: BLE001
            logger.warning("dispatch 命令 %s 收口失败（run %s 已起跑，恢复扫描将按已绑收敛）",
                           command_id, run_id, exc_info=True)

    def close_done(self, command_id: str) -> None:
        """fast path 已完成动作后的命令收口（Stage B resume 命令：run_id 在 enqueue 时
        已写，无 bind 步）。fail-open——收口失败由恢复扫描按 run 现状收敛。"""
        try:
            if self._outbox.claim_specific(command_id, holder=self.holder,
                                           lease_s=self.lease_s):
                self._outbox.complete(command_id, status="done", holder=self.holder)
        except Exception:   # noqa: BLE001
            logger.warning("dispatch 命令 %s fast-path 收口失败（恢复扫描按 run 状态收敛）",
                           command_id, exc_info=True)

    def fail_fast(self, command_id: str, reason: str) -> None:
        """dispatch 被拒（容量 429/会话忙 409）：命令诚实收口 failed（UX 与直通路径一致，
        不做排队重试——那是后续 knob）。fail-open。"""
        try:
            self._outbox.complete(command_id, status="failed",
                                  holder=self.holder, error=reason)
        except Exception:   # noqa: BLE001
            logger.warning("dispatch 命令 %s 失败收口未写成（恢复扫描 attempts 封顶兜底）",
                           command_id, exc_info=True)

    # ── 恢复扫描（后台线程）──────────────────────────────────────
    def tick(self) -> Dict[str, int]:
        """跑一轮恢复。返回计数（观测用）。每步独立 fail-open——单步故障不拖垮整轮。"""
        closed = failed = redriven = 0
        try:
            for cmd in self._outbox.list_bound_expired():
                # 已绑 run=dispatch 已成功（run 的生死归 run 机器/reaper）→ 越权收口
                if self._outbox.complete(cmd["command_id"], status="done", holder=None):
                    closed += 1
                    logger.warning("恢复扫描：命令 %s 已绑 run %s（持有者已死）——收口 done",
                                   cmd["command_id"], cmd.get("run_id"))
        except Exception:   # noqa: BLE001
            logger.warning("恢复扫描：bound-expired 收口失败（下轮再试）", exc_info=True)
        try:
            failed = self._outbox.sweep_exhausted(max_attempts=self.max_attempts)
            if failed:
                logger.error("恢复扫描：%s 条命令重驱封顶落 failed（用户问题未被回答，"
                             "需人工关注 agent_dispatch_command.last_error）", failed)
        except Exception:   # noqa: BLE001
            logger.warning("恢复扫描：attempts 封顶收口失败（下轮再试）", exc_info=True)
        while True:
            try:
                cmd = self._outbox.claim_next(holder=self.holder, lease_s=self.lease_s,
                                              max_attempts=self.max_attempts)
            except Exception:   # noqa: BLE001
                logger.warning("恢复扫描：claim_next 失败（下轮再试）", exc_info=True)
                break
            if cmd is None:
                break
            if not cmd.get("payload"):
                self.fail_fast(cmd["command_id"], "payload 损坏（无法重建执行）")
                continue
            try:
                run_id = self._recover_fn(cmd)
            except DispatchRetryLater as e:
                logger.warning("恢复扫描：命令 %s 本轮不可重驱（%s）——留 claimed 等租约到期",
                               cmd["command_id"], e)
                continue
            except Exception as e:   # noqa: BLE001 — 不可重建：诚实失败
                logger.error("恢复扫描：命令 %s 重建执行失败——收口 failed",
                             cmd["command_id"], exc_info=True)
                self.fail_fast(cmd["command_id"], f"重建执行失败: {str(e)[:300]}")
                continue
            if run_id:
                self.bind_and_done(cmd["command_id"], run_id)
                redriven += 1
                logger.warning("恢复扫描：命令 %s 重驱成功（run %s，答案将落发起人会话历史）",
                               cmd["command_id"], run_id)
            else:
                self.fail_fast(cmd["command_id"], "recover_fn 未返回 run_id")
        return {"closed": closed, "failed": int(failed), "redriven": redriven}

    def run_forever(self, stop: threading.Event, interval_s: Optional[float] = None) -> None:
        """后台循环（daemon 线程体）：启动即先跑一轮（补进程重启前的欠账），随后按间隔。"""
        interval = (interval_s if interval_s is not None
                    else float(_int_env("RAG_AGENT_DISPATCH_INTERVAL_S", 60)))
        while True:
            try:
                self.tick()
            except Exception:   # noqa: BLE001 — 循环永不因单轮异常退出
                logger.warning("durable dispatch 恢复扫描单轮异常（继续）", exc_info=True)
            if stop.wait(max(1.0, interval)):
                return
