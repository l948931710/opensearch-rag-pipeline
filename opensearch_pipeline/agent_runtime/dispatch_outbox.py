# -*- coding: utf-8 -*-
"""
dispatch_outbox.py — PR-3 Stage A：durable dispatch 命令表（outbox+lease）的 RDS 读写

设计（docs/pr3_durable_worker_design_2026-07-17_DRAFT.md）：
- **command-as-truth（D1）**：命令行是唯一的 pre-dispatch durable 真相，agent_run 行由
  认领方在执行时才创建——「无人持有的 queued run」从根上不存在；
- **认领=租约（D2）**：`FOR UPDATE SKIP LOCKED` 抢单 + lease_holder/lease_expires_at，
  单副本零竞争、多副本天然安全；持有者死亡=租约过期，恢复扫描重驱；
- **绑定分界（D4）**：bind_run 之前 at-least-once（可重驱，attempts 封顶），之后
  at-most-once（绝不重执行——run 终态则命令收口 done）。

DB 访问沿用 run_store 惯例：`db._get_db_conn()`、`%s` 占位、显式 commit/rollback/close；
多语句事务一律 `_begin` 钉连接（批次1 SteadyDB 纪律）。所有方法抛错由调用方分流——
enqueue 失败=受理失败（fail-closed），其余在 dispatcher 内 fail-open 记日志。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Optional

from opensearch_pipeline.agent_runtime.run_store import _begin
from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)

_COLS = ("command_id, kind, status, thread_id, user_id, channel, payload_json, "
         "run_id, attempts, lease_holder, lease_expires_at, last_error")


def _op_db() -> str:
    return get_config().rds.operation_database


def insert_command_tx(cur, *, command_id: str, kind: str, thread_id: str, user_id: str,
                      channel: str, payload: Dict[str, Any],
                      run_id: Optional[str] = None) -> str:
    """PR-3 Stage B：命令与主事实**同事务**落库（approval_store.decide 的 outbox_writer
    游标回调用）——决定 commit ⇒ resume 命令必然存在，「decide 后进程崩溃」从 B6
    周期对账推断升级为 durable 命令消费。抛错=调用方事务整体回滚（决定与命令同生共死）。
    resume 命令的 run_id 在 enqueue 时即写（恢复按 run 现状收敛，见 durable_dispatcher）。"""
    cur.execute(
        f"INSERT INTO {_op_db()}.agent_dispatch_command "
        "(command_id, kind, status, thread_id, user_id, channel, payload_json, run_id) "
        "VALUES (%s,%s,'queued',%s,%s,%s,%s,%s)",
        (command_id, kind, thread_id, user_id, channel,
         json.dumps(payload, ensure_ascii=False), run_id))
    return command_id


def _row_to_dict(row) -> Dict[str, Any]:
    d = dict(zip((c.strip() for c in _COLS.split(",")), row))
    if d.get("payload_json"):
        try:
            d["payload"] = json.loads(d["payload_json"])
        except Exception:   # noqa: BLE001 — 载荷坏行：payload=None，由调用方按 failed 收口
            d["payload"] = None
    else:
        d["payload"] = None
    return d


class RDSDispatchOutbox:
    """agent_dispatch_command 的 RDS（fuling_operation）实现。"""

    def _conn(self):
        from opensearch_pipeline.db import _get_db_conn
        return _get_db_conn()

    # ── 写侧 ─────────────────────────────────────────────────────
    def enqueue(self, *, thread_id: str, user_id: str, channel: str,
                payload: Dict[str, Any], kind: str = "submit") -> str:
        """受理即落命令（单条 INSERT 自成事务）。返回 command_id。
        失败上抛=受理失败（fail-closed：命令不 durable 就不该假装接了单）。"""
        command_id = uuid.uuid4().hex
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {db}.agent_dispatch_command "
                    "(command_id, kind, status, thread_id, user_id, channel, payload_json) "
                    "VALUES (%s,%s,'queued',%s,%s,%s,%s)",
                    (command_id, kind, thread_id, user_id, channel,
                     json.dumps(payload, ensure_ascii=False)))
            conn.commit()
            return command_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_specific(self, command_id: str, *, holder: str, lease_s: int) -> bool:
        """fast-path 认领（enqueue 后同请求内）：queued → claimed（CAS）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.agent_dispatch_command "
                    "SET status='claimed', lease_holder=%s, "
                    "    lease_expires_at=DATE_ADD(NOW(3), INTERVAL %s SECOND), "
                    "    attempts=attempts+1 "
                    "WHERE command_id=%s AND status='queued'",
                    (holder, int(lease_s), command_id))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_next(self, *, holder: str, lease_s: int,
                   max_attempts: int = 3) -> Optional[Dict[str, Any]]:
        """恢复扫描认领：queued 或 lease 已过期的 claimed（持有者已死）中最老的一条。
        SKIP LOCKED：并发扫描（未来多副本）各拿各的，绝不双认领。
        attempts 已达上限的行不再认领（由 sweep_exhausted 收口 failed）。
        Stage B kind-aware（顺修 Stage A 缺口）：**已绑 run 的 submit 命令绝不再认领**
        （at-most-once——此前 list_bound_expired 单批漏网的已绑 submit 会被本查询捡走
        重执行=双答案）；resume 命令 run_id 恒有值，重驱天然幂等（CAS first-claim-wins），
        照常认领。"""
        db = _op_db()
        conn = self._conn()
        _begin(conn)                        # SELECT FOR UPDATE + UPDATE：钉连接
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_COLS} FROM {db}.agent_dispatch_command "
                    "WHERE (status='queued' OR (status='claimed' AND lease_expires_at < NOW(3))) "
                    "  AND attempts < %s "
                    "  AND (run_id IS NULL OR kind='resume') "
                    "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (int(max_attempts),))
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return None
                cmd = _row_to_dict(row)
                cur.execute(
                    f"UPDATE {db}.agent_dispatch_command "
                    "SET status='claimed', lease_holder=%s, "
                    "    lease_expires_at=DATE_ADD(NOW(3), INTERVAL %s SECOND), "
                    "    attempts=attempts+1 "
                    "WHERE command_id=%s",
                    (holder, int(lease_s), cmd["command_id"]))
            conn.commit()
            cmd["attempts"] = int(cmd.get("attempts") or 0) + 1
            cmd["lease_holder"] = holder
            return cmd
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def renew_lease(self, command_id: str, *, holder: str, lease_s: int) -> bool:
        """续租（CAS on holder）：只有当前持有者能续。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.agent_dispatch_command "
                    "SET lease_expires_at=DATE_ADD(NOW(3), INTERVAL %s SECOND) "
                    "WHERE command_id=%s AND status='claimed' AND lease_holder=%s",
                    (int(lease_s), command_id, holder))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def bind_run(self, command_id: str, run_id: str, *, holder: str) -> bool:
        """认领方创建 run 后立即回填 run_id——**at-most-once 分界线**（D4）：
        绑定后的命令绝不重执行（恢复扫描只按 run 终态收口）。CAS on holder。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.agent_dispatch_command SET run_id=%s "
                    "WHERE command_id=%s AND status='claimed' AND lease_holder=%s",
                    (run_id, command_id, holder))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete(self, command_id: str, *, status: str, holder: Optional[str] = None,
                 error: Optional[str] = None) -> bool:
        """收口：claimed → done/failed/cancelled。holder 给定时 CAS on holder（正常收口）；
        holder=None 是恢复扫描的越权收口（持有者已死，按 run 终态定论）。"""
        if status not in ("done", "failed", "cancelled"):
            raise ValueError(f"非法收口 status={status!r}")
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                if holder is not None:
                    cur.execute(
                        f"UPDATE {db}.agent_dispatch_command "
                        "SET status=%s, last_error=%s "
                        "WHERE command_id=%s AND status='claimed' AND lease_holder=%s",
                        (status, (error or None) and str(error)[:500], command_id, holder))
                else:
                    cur.execute(
                        f"UPDATE {db}.agent_dispatch_command "
                        "SET status=%s, last_error=%s "
                        "WHERE command_id=%s AND status='claimed'",
                        (status, (error or None) and str(error)[:500], command_id))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def sweep_exhausted(self, *, max_attempts: int = 3) -> int:
        """attempts 封顶且仍未收口（queued/lease 过期 claimed 且未绑 run）→ failed。
        绑了 run 的过期命令不在此收口（走 run 终态判定，见 dispatcher）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.agent_dispatch_command "
                    "SET status='failed', last_error='重驱次数封顶（attempts 上限）' "
                    "WHERE run_id IS NULL AND attempts >= %s "
                    "  AND (status='queued' OR (status='claimed' AND lease_expires_at < NOW(3)))",
                    (int(max_attempts),))
                n = cur.rowcount
            conn.commit()
            return int(n)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 读侧 ─────────────────────────────────────────────────────
    def list_bound_expired(self, limit: int = 20) -> "list":
        """已绑 run 但 lease 过期的 **submit** 命令（持有者死于执行中）——dispatcher 无条件
        收口 done（dispatch 已成功，run 生死归 run 机器）。resume 命令有意排除：run_id
        恒有值不代表 dispatch 成功，须按 run 现状收敛（claim_next→recover_fn 处理）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_COLS} FROM {db}.agent_dispatch_command "
                    "WHERE status='claimed' AND lease_expires_at < NOW(3) "
                    "  AND run_id IS NOT NULL AND kind='submit' ORDER BY id LIMIT %s",
                    (int(limit),))
                rows = cur.fetchall() or []
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def backlog_count(self) -> int:
        """观测：待认领/过期未收口的命令数（readiness/告警面备用）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM {db}.agent_dispatch_command "
                    "WHERE status='queued' OR (status='claimed' AND lease_expires_at < NOW(3))")
                row = cur.fetchone()
            return int(row[0] if not isinstance(row, dict) else list(row.values())[0])
        finally:
            conn.close()
