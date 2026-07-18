# -*- coding: utf-8 -*-
"""dbprobe — 服务端真相断言面（直连本地 MySQL 读 agent 表族 + 连接数探测）。

原则：**不 truncate 共享表**——按时间水位切片（本场景开始时刻之后的行），
场景之间无破坏性清理，报告可溯源。DB 不可达时所有断言降级为 skipped（报告如实标注）。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

try:
    import pymysql
except ImportError:   # pragma: no cover — production extra 未装时降级
    pymysql = None

# ssl_disabled：本探针钉死本地 MySQL（host-pin），显式明文——MySQL 8 Docker 默认带
# SSL 能力位，pymysql 2.x PREFERRED 会自动协商，显式声明消除客户端版本漂移（P0-02/B3）
_CONN = dict(host="127.0.0.1", port=3306, user="root", password="your_password",
             charset="utf8mb4", autocommit=True, ssl_disabled=True)


class DBProbe:
    def __init__(self, operation_db: str = "fuling_operation"):
        self.db = operation_db
        self.available = False
        if pymysql is not None:
            try:
                self._query("SELECT 1")
                self.available = True
            except Exception:   # noqa: BLE001
                self.available = False

    def _query(self, sql: str, args: tuple = ()) -> List[tuple]:
        conn = pymysql.connect(database=self.db, **_CONN)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return list(cur.fetchall())
        finally:
            conn.close()

    def now(self) -> dt.datetime:
        """DB 侧时钟水位，**下取整到整秒**。

        表精度不一：agent_run/tool_invocation/llm_call_log.created_at 是 datetime(3)，
        但 qa_session_log.created_at 是 datetime(0)（整秒）——若水位带微秒/毫秒，qa 行
        （created_at 截到 .000）会掉到水位下方被漏计（S0-db-qa 5/6 假失败）。下取整到整秒
        后两种精度都 >= 水位。跨场景不误纳：runner 每场景重启服务（>1s 间隔），同场景内的
        预热 run 早于水位数秒，均落在更早的整秒，不会被整秒水位纳入。"""
        t = self._query("SELECT NOW(6)")[0][0]
        return t.replace(microsecond=0)

    # ── 断言面 ──────────────────────────────────────────────────
    def agent_runs_since(self, t0: dt.datetime) -> Dict[str, int]:
        """按状态分布统计 t0 之后创建的 agent_run。"""
        if not self.available:
            return {}
        rows = self._query(
            "SELECT status, COUNT(*) FROM agent_run WHERE started_at >= %s GROUP BY status",
            (t0,))
        return {str(r[0]): int(r[1]) for r in rows}

    def llm_calls_since(self, t0: dt.datetime) -> Dict[str, Any]:
        if not self.available:
            return {}
        rows = self._query(
            "SELECT COUNT(*), COALESCE(AVG(latency_ms),0), COALESCE(MAX(latency_ms),0), "
            "SUM(status='error') FROM llm_call_log WHERE created_at >= %s", (t0,))
        n, avg_ms, max_ms, errs = rows[0]
        by_model = self._query(
            "SELECT model, COUNT(*) FROM llm_call_log WHERE created_at >= %s GROUP BY model",
            (t0,))
        return {"count": int(n), "avg_ms": float(avg_ms), "max_ms": int(max_ms),
                "errors": int(errs or 0),
                "by_model": {str(m): int(c) for m, c in by_model}}

    def tool_invocations_since(self, t0: dt.datetime) -> Dict[str, int]:
        if not self.available:
            return {}
        rows = self._query(
            "SELECT status, COUNT(*) FROM tool_invocation WHERE started_at >= %s "
            "GROUP BY status", (t0,))
        return {str(r[0]): int(r[1]) for r in rows}

    def qa_sessions_since(self, t0: dt.datetime, model_name: str = "agent") -> Dict[str, int]:
        if not self.available:
            return {}
        rows = self._query(
            "SELECT answer_status, COUNT(*) FROM qa_session_log "
            "WHERE created_at >= %s AND model_name = %s GROUP BY answer_status",
            (t0, model_name))
        return {str(r[0]): int(r[1]) for r in rows}

    def admission_rejects(self) -> List[Dict[str, Any]]:
        """qa_admission_reject 全量（表本身按天聚合，行数极小）。"""
        if not self.available:
            return []
        rows = self._query(
            "SELECT stat_date, reason, reject_count FROM qa_admission_reject "
            "ORDER BY stat_date DESC, reason")
        return [{"date": str(r[0]), "reason": str(r[1]), "count": int(r[2])} for r in rows]

    def mysql_connections(self) -> int:
        if not self.available:
            return -1
        rows = self._query("SHOW STATUS LIKE 'Threads_connected'")
        return int(rows[0][1]) if rows else -1

    def orphan_running_runs(self) -> int:
        """状态仍为 running/resuming 的 run 数（S8 重启后应归零）。"""
        if not self.available:
            return -1
        rows = self._query(
            "SELECT COUNT(*) FROM agent_run WHERE status IN ('running','resuming')")
        return int(rows[0][0])
