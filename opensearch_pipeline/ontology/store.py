# -*- coding: utf-8 -*-
"""
store.py — 本体表族（schema/027–029）的持久层：RDSOntologyStore + MemoryOntologyStore。

双后端同一契约（沿 session_store Memory/Redis 双后端惯例）：Memory 供单测/SIM 注入，
RDS 走 `db._get_db_conn()`（池 + GuardedDBConnection 写守卫——PROD-RO/非生产→生产写
在连接层被拦，本层不再自建环境判断）。

写入语义（外评 S1–S4 的持久层承载）：
- **本层只做存取与状态机，不做消解决策**——在线 resolve() 纯读；谁能调用写方法由上层
  （seeding/回填 worker/工作台 routes/受治理 Action）与审计负责（PR6 落 agent_audit_log）。
- 至多一行 active 别名（uk_ns_norm_active）→ 重复插入抛 DuplicateActiveIdentifier；
- 至多一个 open case（uk_ns_norm_open）→ upsert_case 命中即 seen_count+1（观测聚合）；
- 状态迁移一律 CAS（UPDATE … WHERE status=旧值，rowcount=0 即让位并发方）；
- golden_json 乐观锁（version CAS），冲突返回 False 由调用方重取。
"""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

from opensearch_pipeline.ontology.ids import format_ref, new_ulid

__all__ = [
    "DuplicateActiveIdentifier",
    "MemoryOntologyStore",
    "RDSOntologyStore",
]

_IDENTIFIER_END_STATES = ("rejected", "superseded")


class DuplicateActiveIdentifier(Exception):
    """同 (namespace, norm_value) 已有 active 别名——撞 uk_ns_norm_active。"""


def _dump(obj: Any) -> Optional[str]:
    return None if obj is None else json.dumps(obj, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
# RDS 后端
# ══════════════════════════════════════════════════════════════════════════════
class RDSOntologyStore:
    """ontology_*（schema/027–029）的 RDS 实现。DB 访问沿 registry_store 惯例：
    `db._get_db_conn()`、`%s` 占位、f-string 库前缀、显式 commit/rollback/close。"""

    def _conn(self):
        from opensearch_pipeline.db import _get_db_conn
        return _get_db_conn()

    @staticmethod
    def _db() -> str:
        from opensearch_pipeline.config import get_config
        return get_config().rds.operation_database

    @staticmethod
    def _rows_to_dicts(cur) -> List[Dict[str, Any]]:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    @staticmethod
    def _is_dup(exc: Exception) -> bool:
        return bool(getattr(exc, "args", None)) and exc.args[0] == 1062

    # ── 对象 ────────────────────────────────────────────────────────────────
    def mint_object(self, object_type: str, title: str, *, owner_dept: str,
                    golden: Optional[Dict[str, Any]] = None,
                    data_classification: str = "internal",
                    source_of_record: str = "ontology",
                    lifecycle_state: str = "draft") -> Dict[str, Any]:
        """铸 canonical 对象：ref_seq 原子取号 + INSERT，同一事务。
        object_type 未在 ontology_ref_seq 登记 → ValueError（登记类型码是治理动作，不静默造号）。"""
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT type_code FROM {db}.ontology_ref_seq "
                            "WHERE object_type=%s", (object_type,))
                row = cur.fetchone()
                if row is None:
                    raise ValueError(f"object_type {object_type!r} 未在 ontology_ref_seq 登记类型码")
                type_code = row[0]
                cur.execute(f"UPDATE {db}.ontology_ref_seq "
                            "SET next_no=LAST_INSERT_ID(next_no+1) WHERE object_type=%s",
                            (object_type,))
                cur.execute("SELECT LAST_INSERT_ID()")
                seq_no = int(cur.fetchone()[0])
                object_id = new_ulid()
                canonical_ref = format_ref(type_code, seq_no)
                cur.execute(
                    f"INSERT INTO {db}.ontology_object "
                    "(object_id, object_type, canonical_ref, title, golden_json, lifecycle_state, "
                    " owner_dept, data_classification, source_of_record) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (object_id, object_type, canonical_ref, title, _dump(golden or {}),
                     lifecycle_state, owner_dept, data_classification, source_of_record))
            conn.commit()
            return {"object_id": object_id, "object_type": object_type,
                    "canonical_ref": canonical_ref, "title": title}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_object(self, object_id: str) -> Optional[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_object WHERE object_id=%s", (object_id,))
                rows = self._rows_to_dicts(cur)
            return rows[0] if rows else None
        finally:
            conn.close()

    def find_objects(self, object_type: str, *, title_like: Optional[str] = None,
                     status: str = "active", limit: int = 50) -> List[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        limit = max(1, min(int(limit), 200))
        try:
            with conn.cursor() as cur:
                sql = (f"SELECT object_id, object_type, canonical_ref, title, lifecycle_state, "
                       f"owner_dept, data_classification, status FROM {db}.ontology_object "
                       "WHERE object_type=%s AND status=%s")
                params: List[Any] = [object_type, status]
                if title_like:
                    sql += " AND title LIKE %s"
                    params.append(f"%{title_like}%")
                cur.execute(sql + " ORDER BY canonical_ref LIMIT %s", (*params, limit))
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def update_golden(self, object_id: str, golden: Dict[str, Any], *,
                      expected_version: int) -> bool:
        """乐观锁 CAS：版本不匹配返回 False（调用方重取重试）。"""
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.ontology_object SET golden_json=%s, version=version+1 "
                    "WHERE object_id=%s AND version=%s",
                    (_dump(golden), object_id, expected_version))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def retire_object(self, object_id: str) -> bool:
        """S3 最小纠错：active→retired（CAS）。审计留痕在调用方（工作台 routes）。"""
        return self._object_transition(object_id, to_status="retired")

    def mark_duplicate(self, object_id: str, merged_into: str) -> bool:
        """S3 最小纠错：active→merged + merged_into 标记（**不做关系/黄金记录传播**，全量=P2）。"""
        if object_id == merged_into:
            raise ValueError("merged_into 不能指向自身")
        return self._object_transition(object_id, to_status="merged", merged_into=merged_into)

    def _object_transition(self, object_id: str, *, to_status: str,
                           merged_into: Optional[str] = None) -> bool:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.ontology_object SET status=%s, merged_into=%s "
                    "WHERE object_id=%s AND status='active'",
                    (to_status, merged_into, object_id))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 别名（已确认映射；候选一律走 case，S2）──────────────────────────────
    def get_active_identifier(self, namespace: str, norm_value: str) -> Optional[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_identifier "
                            "WHERE namespace=%s AND norm_value=%s AND status='active'",
                            (namespace, norm_value))
                rows = self._rows_to_dicts(cur)
            return rows[0] if rows else None
        finally:
            conn.close()

    def get_identifier(self, identifier_id: str) -> Optional[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_identifier "
                            "WHERE identifier_id=%s", (identifier_id,))
                rows = self._rows_to_dicts(cur)
            return rows[0] if rows else None
        finally:
            conn.close()

    def insert_identifier(self, namespace: str, raw_value: str, norm_value: str,
                          target_object_id: str, *, method: str, relation: str = "alias",
                          target_revision: Optional[str] = None, confidence: float = 1.0,
                          confirmed_by: Optional[str] = None,
                          source_case_id: Optional[str] = None,
                          approval_request_id: Optional[str] = None) -> str:
        """铸一条 active 别名（持久化确认——仅播种/回填/工作台/受治理 Action 四路径可达，S1）。"""
        db, conn = self._db(), self._conn()
        identifier_id = new_ulid()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {db}.ontology_identifier "
                    "(identifier_id, namespace, raw_value, norm_value, target_object_id, "
                    " target_revision, relation, resolution_method, confidence, status, "
                    " source_case_id, confirmed_by, approval_request_id, confirmed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,NOW(3))",
                    (identifier_id, namespace, raw_value, norm_value, target_object_id,
                     target_revision, relation, method, confidence,
                     source_case_id, confirmed_by, approval_request_id))
            conn.commit()
            return identifier_id
        except Exception as e:
            conn.rollback()
            if self._is_dup(e):
                raise DuplicateActiveIdentifier(f"{namespace}:{norm_value} 已有 active 别名")
            raise
        finally:
            conn.close()

    def deactivate_identifier(self, identifier_id: str, *, status: str = "rejected") -> bool:
        """S3：active→rejected|superseded（CAS）。误配纠正的第一动作。"""
        if status not in _IDENTIFIER_END_STATES:
            raise ValueError(f"非法终态 {status!r}（合法：{_IDENTIFIER_END_STATES}）")
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {db}.ontology_identifier SET status=%s "
                            "WHERE identifier_id=%s AND status='active'", (status, identifier_id))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def repoint_identifier(self, identifier_id: str, new_target_object_id: str, *,
                           by: str, new_target_revision: Optional[str] = None) -> str:
        """S3 原子改指：同一事务内 旧行 active→superseded(+superseded_by) + 插新 active 行。
        返回新行 identifier_id；旧行非 active → ValueError（先查明现状再纠错）。"""
        db, conn = self._db(), self._conn()
        new_id = new_ulid()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_identifier "
                            "WHERE identifier_id=%s FOR UPDATE", (identifier_id,))
                rows = self._rows_to_dicts(cur)
                if not rows or rows[0]["status"] != "active":
                    raise ValueError(f"identifier {identifier_id} 不存在或非 active，无法改指")
                old = rows[0]
                cur.execute(f"UPDATE {db}.ontology_identifier "
                            "SET status='superseded', superseded_by=%s "
                            "WHERE identifier_id=%s AND status='active'", (new_id, identifier_id))
                if cur.rowcount != 1:   # 并发方先动了手：让位
                    raise ValueError(f"identifier {identifier_id} 已被并发处置")
                cur.execute(
                    f"INSERT INTO {db}.ontology_identifier "
                    "(identifier_id, namespace, raw_value, norm_value, target_object_id, "
                    " target_revision, relation, resolution_method, confidence, status, "
                    " source_case_id, confirmed_by, confirmed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'manual',1.000,'active',%s,%s,NOW(3))",
                    (new_id, old["namespace"], old["raw_value"], old["norm_value"],
                     new_target_object_id, new_target_revision, old["relation"],
                     old["source_case_id"], by))
            conn.commit()
            return new_id
        except Exception as e:
            conn.rollback()
            if self._is_dup(e):   # 理论不可达（旧 active 刚被本事务 supersede）；防御留位
                raise DuplicateActiveIdentifier(
                    f"{identifier_id} 改指撞 active 唯一键（并发插入）")
            raise
        finally:
            conn.close()

    def list_identifiers_for_target(self, target_object_id: str,
                                    status: Optional[str] = None) -> List[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                sql = f"SELECT * FROM {db}.ontology_identifier WHERE target_object_id=%s"
                params: List[Any] = [target_object_id]
                if status:
                    sql += " AND status=%s"
                    params.append(status)
                cur.execute(sql + " ORDER BY first_seen_at", params)
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    # ── 消解 case / 候选（S2 承载层）────────────────────────────────────────
    def upsert_case(self, namespace: str, raw_value: str, norm_value: str, *,
                    object_type_hint: Optional[str] = None,
                    evidence: Optional[Dict[str, Any]] = None) -> str:
        """观测聚合：已有 open case → seen_count+1 + last_seen（evidence 保首次快照不覆盖）；
        无 → 新建。刻意不用 SELECT … FOR UPDATE（空行取间隙锁，并发首插会 1213 死锁）——
        乐观路径 + 撞 uk_ns_norm_open(1062) 回退聚合。"""
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT case_id FROM {db}.ontology_resolution_case "
                            "WHERE namespace=%s AND norm_value=%s AND status='open'",
                            (namespace, norm_value))
                row = cur.fetchone()
                if row:
                    cur.execute(f"UPDATE {db}.ontology_resolution_case "
                                "SET seen_count=seen_count+1, last_seen_at=NOW(3) "
                                "WHERE case_id=%s AND status='open'", (row[0],))
                    if cur.rowcount == 1:
                        conn.commit()
                        return row[0]
                    # 窄竞态：查到后刚被处置 → 落到新建路径
                case_id = new_ulid()
                dup = False
                try:
                    cur.execute(
                        f"INSERT INTO {db}.ontology_resolution_case "
                        "(case_id, namespace, raw_value, norm_value, object_type_hint, "
                        " status, evidence_json) VALUES (%s,%s,%s,%s,%s,'open',%s)",
                        (case_id, namespace, raw_value, norm_value, object_type_hint,
                         _dump(evidence)))
                except Exception as e:
                    if not self._is_dup(e):
                        raise
                    dup = True   # 并发首插输家：改走聚合
                if not dup:
                    conn.commit()
                    return case_id
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {db}.ontology_resolution_case "
                            "SET seen_count=seen_count+1, last_seen_at=NOW(3) "
                            "WHERE namespace=%s AND norm_value=%s AND status='open'",
                            (namespace, norm_value))
                cur.execute(f"SELECT case_id FROM {db}.ontology_resolution_case "
                            "WHERE namespace=%s AND norm_value=%s AND status='open'",
                            (namespace, norm_value))
                row2 = cur.fetchone()
            conn.commit()
            if row2 is None:   # 赢家已被处置：极窄窗口，交回调用方重试
                raise RuntimeError(f"case ({namespace},{norm_value}) 竞态后不可见，请重试")
            return row2[0]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_candidate(self, case_id: str, target_object_id: str, *, method: str,
                      confidence: float, target_revision: Optional[str] = None,
                      features: Optional[Dict[str, Any]] = None) -> str:
        """候选幂等：同 (case, target, method) 重复提出 → 置信取大、依据取新。返回现行 candidate_id。"""
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {db}.ontology_resolution_candidate "
                    "(candidate_id, case_id, target_object_id, target_revision, method, "
                    " confidence, features_json) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE "
                    "confidence=GREATEST(confidence, VALUES(confidence)), "
                    "features_json=COALESCE(VALUES(features_json), features_json)",
                    (new_ulid(), case_id, target_object_id, target_revision, method,
                     confidence, _dump(features)))
                cur.execute(
                    f"SELECT candidate_id FROM {db}.ontology_resolution_candidate "
                    "WHERE case_id=%s AND target_object_id=%s AND method=%s",
                    (case_id, target_object_id, method))
                candidate_id = cur.fetchone()[0]
            conn.commit()
            return candidate_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_resolution_case "
                            "WHERE case_id=%s", (case_id,))
                rows = self._rows_to_dicts(cur)
            return rows[0] if rows else None
        finally:
            conn.close()

    def list_open_cases(self, *, namespace: Optional[str] = None,
                        object_type_hint: Optional[str] = None, order: str = "freq",
                        limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """工作台队列：order=freq（高频未解析优先）| recent。"""
        db, conn = self._db(), self._conn()
        limit = max(1, min(int(limit), 200))
        order_sql = "seen_count DESC, last_seen_at DESC" if order == "freq" \
            else "last_seen_at DESC"
        try:
            with conn.cursor() as cur:
                sql = f"SELECT * FROM {db}.ontology_resolution_case WHERE status='open'"
                params: List[Any] = []
                if namespace:
                    sql += " AND namespace=%s"
                    params.append(namespace)
                if object_type_hint:
                    sql += " AND object_type_hint=%s"
                    params.append(object_type_hint)
                cur.execute(sql + f" ORDER BY {order_sql} LIMIT %s OFFSET %s",
                            (*params, limit, max(0, int(offset))))
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def list_candidates(self, case_id: str) -> List[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_resolution_candidate "
                            "WHERE case_id=%s ORDER BY confidence DESC", (case_id,))
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def resolve_case(self, case_id: str, *, identifier_id: str, by: str,
                     note: Optional[str] = None) -> bool:
        return self._case_transition(case_id, to_status="resolved", by=by, note=note,
                                     identifier_id=identifier_id)

    def dismiss_case(self, case_id: str, *, by: str, note: str) -> bool:
        """驳回/终止必须给理由（工作台契约）——空 note 直接 ValueError。"""
        if not (note or "").strip():
            raise ValueError("dismiss 必须给 resolution_note（处置理由）")
        return self._case_transition(case_id, to_status="dismissed", by=by, note=note)

    def _case_transition(self, case_id: str, *, to_status: str, by: str,
                         note: Optional[str], identifier_id: Optional[str] = None) -> bool:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.ontology_resolution_case "
                    "SET status=%s, resolved_identifier_id=%s, resolved_by=%s, "
                    "    resolved_at=NOW(3), resolution_note=%s "
                    "WHERE case_id=%s AND status='open'",
                    (to_status, identifier_id, by, note, case_id))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 溯源目录 / stewardship（种子经 upsert，代码即声明）──────────────────
    def upsert_attribute_sources(self, rows: List[Dict[str, Any]]) -> int:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        f"INSERT INTO {db}.ontology_attribute_source "
                        "(object_type, attribute, sor_system, sync_mode, freshness, notes) "
                        "VALUES (%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE sor_system=VALUES(sor_system), "
                        "sync_mode=VALUES(sync_mode), freshness=VALUES(freshness), "
                        "notes=VALUES(notes)",
                        (r["object_type"], r["attribute"], r["sor_system"], r["sync_mode"],
                         r.get("freshness"), r.get("notes")))
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_attribute_sources(self) -> List[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_attribute_source "
                            "ORDER BY object_type, attribute")
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def upsert_stewardship(self, rows: List[Dict[str, Any]]) -> int:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        f"INSERT INTO {db}.ontology_stewardship "
                        "(scope_type, scope_key, steward_dept, backup_dept, notes) "
                        "VALUES (%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE steward_dept=VALUES(steward_dept), "
                        "backup_dept=VALUES(backup_dept), notes=VALUES(notes)",
                        (r["scope_type"], r["scope_key"], r["steward_dept"],
                         r.get("backup_dept"), r.get("notes")))
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_stewardship(self) -> List[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_stewardship "
                            "ORDER BY scope_type, scope_key")
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    # ── 覆盖率 / 积压统计 ────────────────────────────────────────────────────
    def coverage(self, object_type: Optional[str] = None) -> Dict[str, Any]:
        """resolution_coverage = active / (active + open)——分母是"观测到且尚未确认"的近似。
        auto_active 单列（抽检队列规模）；人工审核率 = 1 - auto/active。"""
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                if object_type:
                    cur.execute(
                        f"SELECT COUNT(*), SUM(i.confirmed_by='auto') "
                        f"FROM {db}.ontology_identifier i "
                        f"JOIN {db}.ontology_object o ON o.object_id=i.target_object_id "
                        "WHERE i.status='active' AND o.object_type=%s", (object_type,))
                else:
                    cur.execute(
                        f"SELECT COUNT(*), SUM(confirmed_by='auto') "
                        f"FROM {db}.ontology_identifier WHERE status='active'")
                active, auto_active = cur.fetchone()
                active, auto_active = int(active or 0), int(auto_active or 0)
                sql = (f"SELECT status, COUNT(*) FROM {db}.ontology_resolution_case "
                       "WHERE 1=1")
                params: List[Any] = []
                if object_type:
                    sql += " AND object_type_hint=%s"
                    params.append(object_type)
                cur.execute(sql + " GROUP BY status", params)
                by_status = {row[0]: int(row[1]) for row in cur.fetchall()}
            open_n = by_status.get("open", 0)
            denom = active + open_n
            return {
                "active_identifiers": active,
                "auto_active": auto_active,
                "open_cases": open_n,
                "resolved_cases": by_status.get("resolved", 0),
                "dismissed_cases": by_status.get("dismissed", 0),
                "resolution_coverage": (active / denom) if denom else None,
            }
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Memory 后端（单测/SIM 注入；语义与 RDS 逐条对齐，由同一套契约测试钉住）
# ══════════════════════════════════════════════════════════════════════════════
class MemoryOntologyStore:
    _SEED_TYPES = (("product", "P"), ("sku", "S"), ("mold", "M"),
                   ("material", "MT"), ("calc_rule", "CR"))

    def __init__(self):
        self._lock = threading.RLock()
        self._objects: Dict[str, Dict[str, Any]] = {}
        self._identifiers: Dict[str, Dict[str, Any]] = {}
        self._cases: Dict[str, Dict[str, Any]] = {}
        self._candidates: Dict[str, Dict[str, Any]] = {}
        self._attr_sources: Dict[tuple, Dict[str, Any]] = {}
        self._stewardship: Dict[tuple, Dict[str, Any]] = {}
        self._seq: Dict[str, list] = {t: [code, 0] for t, code in self._SEED_TYPES}
        self._clock = 0   # 单调伪时钟：排序用（first/last_seen）

    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    # ── 对象 ────────────────────────────────────────────────────────────────
    def mint_object(self, object_type, title, *, owner_dept, golden=None,
                    data_classification="internal", source_of_record="ontology",
                    lifecycle_state="draft"):
        with self._lock:
            if object_type not in self._seq:
                raise ValueError(f"object_type {object_type!r} 未在 ontology_ref_seq 登记类型码")
            self._seq[object_type][1] += 1
            code, seq_no = self._seq[object_type]
            object_id = new_ulid()
            row = {"object_id": object_id, "object_type": object_type,
                   "canonical_ref": format_ref(code, seq_no), "title": title,
                   "golden_json": _dump(golden or {}), "lifecycle_state": lifecycle_state,
                   "owner_dept": owner_dept, "data_classification": data_classification,
                   "source_of_record": source_of_record, "version": 1,
                   "status": "active", "merged_into": None}
            self._objects[object_id] = row
            return {"object_id": object_id, "object_type": object_type,
                    "canonical_ref": row["canonical_ref"], "title": title}

    def get_object(self, object_id):
        with self._lock:
            row = self._objects.get(object_id)
            return dict(row) if row else None

    def find_objects(self, object_type, *, title_like=None, status="active", limit=50):
        with self._lock:
            out = [dict(o) for o in self._objects.values()
                   if o["object_type"] == object_type and o["status"] == status
                   and (title_like is None or title_like in o["title"])]
            return sorted(out, key=lambda o: o["canonical_ref"])[:max(1, min(int(limit), 200))]

    def update_golden(self, object_id, golden, *, expected_version):
        with self._lock:
            row = self._objects.get(object_id)
            if not row or row["version"] != expected_version:
                return False
            row["golden_json"] = _dump(golden)
            row["version"] += 1
            return True

    def retire_object(self, object_id):
        return self._object_transition(object_id, to_status="retired")

    def mark_duplicate(self, object_id, merged_into):
        if object_id == merged_into:
            raise ValueError("merged_into 不能指向自身")
        return self._object_transition(object_id, to_status="merged", merged_into=merged_into)

    def _object_transition(self, object_id, *, to_status, merged_into=None):
        with self._lock:
            row = self._objects.get(object_id)
            if not row or row["status"] != "active":
                return False
            row["status"] = to_status
            row["merged_into"] = merged_into
            return True

    # ── 别名 ────────────────────────────────────────────────────────────────
    def get_active_identifier(self, namespace, norm_value):
        with self._lock:
            for row in self._identifiers.values():
                if (row["namespace"], row["norm_value"], row["status"]) == \
                        (namespace, norm_value, "active"):
                    return dict(row)
            return None

    def insert_identifier(self, namespace, raw_value, norm_value, target_object_id, *,
                          method, relation="alias", target_revision=None, confidence=1.0,
                          confirmed_by=None, source_case_id=None, approval_request_id=None):
        with self._lock:
            if self.get_active_identifier(namespace, norm_value) is not None:
                raise DuplicateActiveIdentifier(f"{namespace}:{norm_value} 已有 active 别名")
            identifier_id = new_ulid()
            self._identifiers[identifier_id] = {
                "identifier_id": identifier_id, "namespace": namespace,
                "raw_value": raw_value, "norm_value": norm_value,
                "target_object_id": target_object_id, "target_revision": target_revision,
                "relation": relation, "resolution_method": method,
                "confidence": confidence, "status": "active", "superseded_by": None,
                "source_case_id": source_case_id, "confirmed_by": confirmed_by,
                "approval_request_id": approval_request_id, "first_seen_at": self._tick()}
            return identifier_id

    def get_identifier(self, identifier_id):
        with self._lock:
            row = self._identifiers.get(identifier_id)
            return dict(row) if row else None

    def deactivate_identifier(self, identifier_id, *, status="rejected"):
        if status not in _IDENTIFIER_END_STATES:
            raise ValueError(f"非法终态 {status!r}（合法：{_IDENTIFIER_END_STATES}）")
        with self._lock:
            row = self._identifiers.get(identifier_id)
            if not row or row["status"] != "active":
                return False
            row["status"] = status
            return True

    def repoint_identifier(self, identifier_id, new_target_object_id, *, by,
                           new_target_revision=None):
        with self._lock:
            row = self._identifiers.get(identifier_id)
            if not row or row["status"] != "active":
                raise ValueError(f"identifier {identifier_id} 不存在或非 active，无法改指")
            row["status"] = "superseded"   # 同锁内先让位再插新行（对齐 RDS 同事务原子性）
            try:
                new_id = self.insert_identifier(
                    row["namespace"], row["raw_value"], row["norm_value"],
                    new_target_object_id, method="manual", relation=row["relation"],
                    target_revision=new_target_revision, confidence=1.0, confirmed_by=by,
                    source_case_id=row["source_case_id"])
            except Exception:
                row["status"] = "active"   # 回滚让位
                raise
            row["superseded_by"] = new_id
            return new_id

    def list_identifiers_for_target(self, target_object_id, status=None):
        with self._lock:
            out = [dict(r) for r in self._identifiers.values()
                   if r["target_object_id"] == target_object_id
                   and (status is None or r["status"] == status)]
            return sorted(out, key=lambda r: r["first_seen_at"])

    # ── case / 候选 ─────────────────────────────────────────────────────────
    def upsert_case(self, namespace, raw_value, norm_value, *, object_type_hint=None,
                    evidence=None):
        with self._lock:
            for row in self._cases.values():
                if (row["namespace"], row["norm_value"], row["status"]) == \
                        (namespace, norm_value, "open"):
                    row["seen_count"] += 1
                    row["last_seen_at"] = self._tick()
                    return row["case_id"]
            case_id = new_ulid()
            now = self._tick()
            self._cases[case_id] = {
                "case_id": case_id, "namespace": namespace, "raw_value": raw_value,
                "norm_value": norm_value, "object_type_hint": object_type_hint,
                "status": "open", "evidence_json": _dump(evidence), "seen_count": 1,
                "first_seen_at": now, "last_seen_at": now, "resolved_identifier_id": None,
                "resolved_by": None, "resolution_note": None}
            return case_id

    def add_candidate(self, case_id, target_object_id, *, method, confidence,
                      target_revision=None, features=None):
        with self._lock:
            for row in self._candidates.values():
                if (row["case_id"], row["target_object_id"], row["method"]) == \
                        (case_id, target_object_id, method):
                    row["confidence"] = max(row["confidence"], confidence)
                    if features is not None:
                        row["features_json"] = _dump(features)
                    return row["candidate_id"]
            candidate_id = new_ulid()
            self._candidates[candidate_id] = {
                "candidate_id": candidate_id, "case_id": case_id,
                "target_object_id": target_object_id, "target_revision": target_revision,
                "method": method, "confidence": confidence, "features_json": _dump(features)}
            return candidate_id

    def get_case(self, case_id):
        with self._lock:
            row = self._cases.get(case_id)
            return dict(row) if row else None

    def list_open_cases(self, *, namespace=None, object_type_hint=None, order="freq",
                        limit=50, offset=0):
        with self._lock:
            rows = [dict(r) for r in self._cases.values() if r["status"] == "open"
                    and (namespace is None or r["namespace"] == namespace)
                    and (object_type_hint is None or r["object_type_hint"] == object_type_hint)]
            key = (lambda r: (-r["seen_count"], -r["last_seen_at"])) if order == "freq" \
                else (lambda r: -r["last_seen_at"])
            rows.sort(key=key)
            lo = max(0, int(offset))
            return rows[lo:lo + max(1, min(int(limit), 200))]

    def list_candidates(self, case_id):
        with self._lock:
            rows = [dict(r) for r in self._candidates.values() if r["case_id"] == case_id]
            return sorted(rows, key=lambda r: -r["confidence"])

    def resolve_case(self, case_id, *, identifier_id, by, note=None):
        return self._case_transition(case_id, to_status="resolved", by=by, note=note,
                                     identifier_id=identifier_id)

    def dismiss_case(self, case_id, *, by, note):
        if not (note or "").strip():
            raise ValueError("dismiss 必须给 resolution_note（处置理由）")
        return self._case_transition(case_id, to_status="dismissed", by=by, note=note)

    def _case_transition(self, case_id, *, to_status, by, note, identifier_id=None):
        with self._lock:
            row = self._cases.get(case_id)
            if not row or row["status"] != "open":
                return False
            row.update(status=to_status, resolved_identifier_id=identifier_id,
                       resolved_by=by, resolution_note=note)
            return True

    # ── 溯源目录 / stewardship ──────────────────────────────────────────────
    def upsert_attribute_sources(self, rows):
        with self._lock:
            for r in rows:
                self._attr_sources[(r["object_type"], r["attribute"])] = dict(r)
            return len(rows)

    def list_attribute_sources(self):
        with self._lock:
            return [dict(v) for k, v in sorted(self._attr_sources.items())]

    def upsert_stewardship(self, rows):
        with self._lock:
            for r in rows:
                self._stewardship[(r["scope_type"], r["scope_key"])] = dict(r)
            return len(rows)

    def list_stewardship(self):
        with self._lock:
            return [dict(v) for k, v in sorted(self._stewardship.items())]

    # ── 统计 ────────────────────────────────────────────────────────────────
    def coverage(self, object_type=None):
        with self._lock:
            def _type_of(target_id):
                obj = self._objects.get(target_id)
                return obj["object_type"] if obj else None

            act = [r for r in self._identifiers.values() if r["status"] == "active"
                   and (object_type is None or _type_of(r["target_object_id"]) == object_type)]
            cases = [r for r in self._cases.values()
                     if object_type is None or r["object_type_hint"] == object_type]
            open_n = sum(1 for r in cases if r["status"] == "open")
            denom = len(act) + open_n
            return {
                "active_identifiers": len(act),
                "auto_active": sum(1 for r in act if r["confirmed_by"] == "auto"),
                "open_cases": open_n,
                "resolved_cases": sum(1 for r in cases if r["status"] == "resolved"),
                "dismissed_cases": sum(1 for r in cases if r["status"] == "dismissed"),
                "resolution_coverage": (len(act) / denom) if denom else None,
            }
