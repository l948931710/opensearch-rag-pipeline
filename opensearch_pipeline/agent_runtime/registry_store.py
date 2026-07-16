# -*- coding: utf-8 -*-
"""
registry_store.py — tool_registry 表的读写（v2 报告 §4「元数据落 DB，进程内只是缓存」；
深度审查 2026-07-09 多实例运维组：kill switch 此前只在进程内存、tool_registry 表死代码——
多实例部署下无法全局拉闸停用工具）。

三件事：
- **sync_specs**：启动时把代码内声明的工具 upsert 进 tool_registry（spec/风险/归属跟代码走；
  **status 绝不覆盖**——DB 里被管理员置 disabled 的行，重启/发布后必须保持停用）。
- **disabled_names**：DB 驱动的 kill switch 读侧（TTL 缓存，默认 30s）。注入
  ToolRegistry.attach_disabled_source → 任一实例经管理端点停用，所有实例在一个 TTL 内生效。
  **fail-open**：DB 故障沿用上一次快照（无快照则空集）——kill switch 失效方向是
  「继续可用」，绝不因 DB 抖动把全部工具误杀（Policy/审批仍在每次调用兜底）。
- **set_status / list_tools / drift_warnings**：管理端点（routes/agent.py，kb_admin）消费；
  drift = 代码内声明 vs 表内记录的键差 + spec 内容差（发布不同版本代码的实例并存时可见）。

DB 访问沿用 run_store 惯例：`db._get_db_conn()`、`%s` 占位、显式 commit/rollback/close。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set

from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)

_VALID_STATUS = ("active", "deprecated", "disabled")


def _op_db() -> str:
    return get_config().rds.operation_database


class RDSToolRegistryStore:
    """tool_registry（schema/022）的 RDS 实现。"""

    def __init__(self, ttl_s: float = 30.0):
        self._ttl = float(ttl_s)
        self._lock = threading.Lock()
        self._cached_disabled: Optional[Set[str]] = None
        self._cached_at = 0.0
        # 批次5（unknown-unknowns P1-03）：sync_specs 时从**代码内声明**捕获 HIGH_WRITE
        # 工具名（不依赖 DB 读成功）——冷实例 + DB 故障叠加时 kill switch 对写工具
        # fail-closed（见 disabled_names），只读工具维持 fail-open。
        self._high_write_names: Set[str] = set()

    def _conn(self):
        from opensearch_pipeline.db import _get_db_conn
        return _get_db_conn()

    # ── 启动同步 ─────────────────────────────────────────────────
    def sync_specs(self, registry, registered_by: str = "platform") -> int:
        """代码内声明 upsert 进表（spec_json/risk/scope/owner 跟代码走，**status 不覆盖**）。
        返回 upsert 行数。失败上抛由调用方决定 fail-open（runtime 建立不因 DB 抖动失败）。"""
        rows = registry.to_registry_rows(registered_by=registered_by)
        # P1-03：HIGH_WRITE 名单在任何 DB 写**之前**捕获（纯本地信息）——sync 失败
        # （fail-open 路径）也要拿到，否则冷实例 DB 故障时写工具的 fail-closed 无名单可依。
        with self._lock:
            self._high_write_names = {
                r["tool_name"] for r in rows
                if str(r.get("risk_level", "")).lower() == "high_write"}
        if not rows:
            return 0
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        f"INSERT INTO {db}.tool_registry "
                        "(tool_name, version, spec_json, risk_level, permission_scope, "
                        " owner_team, status, registered_by, created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,'active',%s,NOW(3)) "
                        "ON DUPLICATE KEY UPDATE "
                        "spec_json=VALUES(spec_json), risk_level=VALUES(risk_level), "
                        "permission_scope=VALUES(permission_scope), owner_team=VALUES(owner_team), "
                        "registered_by=VALUES(registered_by)",   # status 刻意不在列表：保住 disabled
                        (r["tool_name"], r["version"], r["spec_json"], r["risk_level"],
                         r["permission_scope"], r["owner_team"], r["registered_by"]),
                    )
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── kill switch 读侧（TTL 缓存 + fail-open）──────────────────
    def disabled_names(self) -> Set[str]:
        """当前被停用的工具名集合。TTL 内走缓存；DB 故障沿用上一次快照（无快照→空集）。
        绝不抛异常——本方法挂在 ToolRegistry.resolve 热路径上。"""
        now = time.monotonic()
        with self._lock:
            if self._cached_disabled is not None and now - self._cached_at < self._ttl:
                return self._cached_disabled
        try:
            db = _op_db()
            conn = self._conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT DISTINCT tool_name FROM {db}.tool_registry WHERE status='disabled'")
                    names = {row[0] for row in cur.fetchall()}
            finally:
                conn.close()
            with self._lock:
                self._cached_disabled = names
                self._cached_at = now
            return names
        except Exception:   # noqa: BLE001 — 读故障：只读 fail-open / HIGH_WRITE fail-closed
            logger.warning("tool_registry kill switch 读取失败（沿用上次快照）", exc_info=True)
            with self._lock:
                if self._cached_disabled is not None:
                    return self._cached_disabled          # warm：沿用上次快照（原语义）
                # 批次5（unknown-unknowns P1-03）：冷实例 + DB 故障——事故里最需要
                # kill switch 的场景，此前返回空禁用集 = HIGH_WRITE 工具恰在此时重新
                # 出现。无可信快照时把 HIGH_WRITE 名单视为禁用（fail-closed；Policy/
                # 审批仍逐调用兜底），只读工具维持 fail-open 不误杀；首次成功读取后
                # 自动恢复 DB 事实。
                if self._high_write_names:
                    logger.critical(
                        "tool_registry 冷启动且 DB 不可读——HIGH_WRITE 工具 fail-closed "
                        "禁用：%s", sorted(self._high_write_names))
                return set(self._high_write_names)

    def probe_health(self) -> str:
        """readiness 探针（批次5 P0-06b）：**绕缓存**直读 disabled 集一次——
        「表在但 kill-switch 读退化」（权限被收/行损坏）此前只活在 warning 日志里。
        返回 ok / error；绝不抛出。"""
        try:
            db = _op_db()
            conn = self._conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT COUNT(*) FROM {db}.tool_registry WHERE status='disabled'")
                    cur.fetchone()
            finally:
                conn.close()
            return "ok"
        except Exception as e:   # noqa: BLE001
            logger.warning("readiness: kill switch 读探针失败: %s", e)
            return "error"

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cached_disabled = None
            self._cached_at = 0.0

    # ── 管理写侧 ─────────────────────────────────────────────────
    def set_status(self, tool_name: str, status: str) -> int:
        """置某工具（全部版本）status。返回受影响行数（0 = 表内无此工具）。"""
        if status not in _VALID_STATUS:
            raise ValueError(f"非法 status={status!r}（合法：{_VALID_STATUS}）")
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.tool_registry SET status=%s WHERE tool_name=%s",
                    (status, tool_name))
                n = cur.rowcount
            conn.commit()
            self.invalidate_cache()          # 本实例即时生效；其他实例等 TTL
            return int(n)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 治理读侧 ─────────────────────────────────────────────────
    def list_rows(self) -> List[Dict[str, Any]]:
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT tool_name, version, spec_json, risk_level, permission_scope, "
                    f"owner_team, status, registered_by, created_at FROM {db}.tool_registry "
                    "ORDER BY tool_name, version")
                rows = cur.fetchall() or []
            keys = ("tool_name", "version", "spec_json", "risk_level", "permission_scope",
                    "owner_team", "status", "registered_by", "created_at")
            out = []
            for r in rows:
                d = dict(zip(keys, r))
                d["created_at"] = str(d["created_at"]) if d.get("created_at") is not None else None
                out.append(d)
            return out
        finally:
            conn.close()

    def drift_warnings(self, registry) -> List[str]:
        """代码内声明 vs 表内记录：键差（registry.drift_check）+ 同键 spec 内容差。
        多实例滚动发布期（新旧代码并存）或表被手改时可见。"""
        rows = self.list_rows()
        warnings = list(registry.drift_check(rows))
        code = {(r["tool_name"], r["version"]): r["spec_json"]
                for r in registry.to_registry_rows()}
        for row in rows:
            key = (row["tool_name"], row["version"])
            if key in code:
                try:
                    if json.loads(code[key]) != json.loads(row["spec_json"] or "{}"):
                        warnings.append(f"spec 漂移（代码 ≠ DB）: {key[0]}@{key[1]}")
                except Exception:   # noqa: BLE001
                    warnings.append(f"spec 无法比对（DB 行损坏?）: {key[0]}@{key[1]}")
        return warnings
