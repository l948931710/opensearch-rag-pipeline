# -*- coding: utf-8 -*-
"""
operation_ledger.py — PR-3 Stage C：HIGH_WRITE 工具操作台账（agent_tool_operation，schema/045）

「副作用是否已发生」的 durable 真相：工具把台账行与副作用**同一个事务**提交
（`insert_operation_tx` 游标回调，形态同 Stage B dispatch_outbox.insert_command_tx），
于是「行存在 ⇔ 副作用已 commit」成立——check_operation 据此三态作答：

- **applied**：台账有行且 tool_name 相符 → 副作用已提交，附回执；
- **not_applied**：台账无行 → 副作用未提交（对同事务写台账的工具是精确否定）；
- **unknown**：查证不能（存储不可达 / tool_name 错配=账实异常）→ 绝不自动处置，
  留在人工对账通道（fail-safe：宁人工不误判）。

**PK 即 fencing**：operation_id（= tool_invocation.invocation_id，同键重试复用同行 ⇒
同 id）是主键——僵尸线程与「对账放行后的重试」并发提交时至多一个 commit；输家撞
`OperationAlreadyApplied` 整事务回滚，读现行台账回执按幂等成功返回，绝无双副作用。
该异常消息刻意不含 "Duplicate entry"/"1062" 字样——上游各层的 dup 翻译（ontology store
`_is_dup` 按 args[0]==1062、tool_executor 按子串）都不会把它误译，原样穿透到工具层。

DB 访问沿 run_store 惯例：`db._get_db_conn()`、`%s` 占位、显式 commit/rollback/close。
InMemoryOperationLedger 供测试/sim 注入（与 RDSOperationLedger 同契约）。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Optional

from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)


class OperationAlreadyApplied(RuntimeError):
    """同 operation_id 的台账行已存在=该操作已由另一执行提交（fencing 输家）。
    捕获方应回滚本次副作用事务、读现行台账回执按幂等成功收口。"""

    def __init__(self, operation_id: str):
        super().__init__(f"操作 {operation_id} 已由另一执行提交（台账 fencing）")
        self.operation_id = operation_id


def _op_db() -> str:
    return get_config().rds.operation_database


def _is_dup(exc: Exception) -> bool:
    """MySQL 1062（pymysql IntegrityError.args[0]）或消息形态兜底。"""
    args = getattr(exc, "args", None)
    if args and args[0] == 1062:
        return True
    return "Duplicate entry" in str(exc)


def insert_operation_tx(cur, *, operation_id: str, tool_name: str,
                        run_id: Optional[str] = None,
                        receipt: Optional[Dict[str, Any]] = None) -> str:
    """台账行与副作用**同事务**落库（工具在自己写事务内经游标回调调用）。
    同 id 已存在 → 抛 OperationAlreadyApplied（调用方事务整体回滚=fencing）；
    其余异常原样上抛（台账写不进=操作不该 commit，fail-closed 同 P0-06 审计纪律）。"""
    try:
        cur.execute(
            f"INSERT INTO {_op_db()}.agent_tool_operation "
            "(operation_id, tool_name, run_id, receipt_json) VALUES (%s,%s,%s,%s)",
            (operation_id, tool_name, run_id,
             json.dumps(receipt, ensure_ascii=False) if receipt is not None else None))
    except Exception as e:   # noqa: BLE001 — 只翻译 dup；其余原样上抛
        if _is_dup(e):
            raise OperationAlreadyApplied(operation_id) from e
        raise
    return operation_id


def _row_to_dict(row) -> Dict[str, Any]:
    d = {"operation_id": row[0], "tool_name": row[1], "run_id": row[2],
         "receipt_json": row[3], "created_at": str(row[4]) if row[4] is not None else None}
    if d.get("receipt_json"):
        try:
            d["receipt"] = json.loads(d["receipt_json"])
        except Exception:   # noqa: BLE001 — 坏回执：行仍证明已提交，回执置 None
            d["receipt"] = None
    else:
        d["receipt"] = None
    return d


def fetch_operation(operation_id: str) -> Optional[Dict[str, Any]]:
    """按 id 读台账行（独立连接只读）。无行返回 None；存储异常上抛（调用方定语义）。"""
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT operation_id, tool_name, run_id, receipt_json, created_at "
                f"FROM {_op_db()}.agent_tool_operation WHERE operation_id=%s",
                (operation_id,))
            row = cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def _check(row: Optional[Dict[str, Any]], tool_name: str, operation_id: str) -> Dict[str, Any]:
    """行→三态判决（RDS/InMemory 共用）。"""
    if row is None:
        return {"outcome": "not_applied", "receipt": None,
                "detail": "台账无行（同事务写台账的工具=副作用未提交）"}
    if row.get("tool_name") != tool_name:
        logger.error("操作台账账实错位：operation %s 台账归属 %s ≠ 查证方 %s（绝不自动处置）",
                     operation_id, row.get("tool_name"), tool_name)
        return {"outcome": "unknown", "receipt": None,
                "detail": f"台账行归属 {row.get('tool_name')}（错配，须人工核查）"}
    return {"outcome": "applied", "receipt": row.get("receipt"),
            "detail": "台账有行（副作用已随同事务提交）"}


class RDSOperationLedger:
    """默认后端（工具构造期注入座缝，测试换 InMemoryOperationLedger）。"""

    def insert_tx(self, cur, *, operation_id: str, tool_name: str,
                  run_id: Optional[str] = None,
                  receipt: Optional[Dict[str, Any]] = None) -> str:
        # cur=None（InMemory 主存储走座缝时的约定值=测试/sim 配对）：无 RDS 事务可
        # 加入、也无 durable 副作用需要 fence → 诚实 no-op。要在内存配对下测台账语义，
        # 注入 InMemoryOperationLedger（它无视 cur 恒记账）。
        if cur is None:
            logger.debug("操作台账：无事务游标（内存主存储配对），跳过台账写（op=%s）",
                         operation_id)
            return operation_id
        return insert_operation_tx(cur, operation_id=operation_id, tool_name=tool_name,
                                   run_id=run_id, receipt=receipt)

    def fetch(self, operation_id: str) -> Optional[Dict[str, Any]]:
        return fetch_operation(operation_id)

    def check(self, tool_name: str, operation_id: str) -> Dict[str, Any]:
        """三态查证。存储不可达 → unknown（fail-safe：绝不把基础设施故障当"未生效"）。"""
        try:
            row = fetch_operation(operation_id)
        except Exception:   # noqa: BLE001
            logger.warning("操作台账查证失败（存储不可达，按 unknown 留人工通道）", exc_info=True)
            return {"outcome": "unknown", "receipt": None, "detail": "台账查证失败（存储不可达）"}
        return _check(row, tool_name, operation_id)


class InMemoryOperationLedger:
    """测试/sim 后端（与 RDSOperationLedger 同契约）。insert_tx 的 cur 参数被忽略
    ——内存后端无事务，fence 语义由调用方「写台账先于副作用 mutation」保证。"""

    def __init__(self) -> None:
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def insert_tx(self, cur, *, operation_id: str, tool_name: str,
                  run_id: Optional[str] = None,
                  receipt: Optional[Dict[str, Any]] = None) -> str:
        with self._lock:
            if operation_id in self._rows:
                raise OperationAlreadyApplied(operation_id)
            self._rows[operation_id] = {
                "operation_id": operation_id, "tool_name": tool_name, "run_id": run_id,
                "receipt": json.loads(json.dumps(receipt)) if receipt is not None else None,
                "created_at": None}
        return operation_id

    def fetch(self, operation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._rows.get(operation_id)
            return dict(row) if row else None

    def check(self, tool_name: str, operation_id: str) -> Dict[str, Any]:
        return _check(self.fetch(operation_id), tool_name, operation_id)
