# -*- coding: utf-8 -*-
"""
invariants.py — 本体不变量对账 reaper（PR-C，P0-05 验收④）。

原子化（store 复合写）之后残余的半状态只能来自历史脏数据/未知 bug——本模块周期性
扫出并**阻断级上报**（非零退出码），而不是让它们静默腐蚀身份脊柱。只读，绝不修数
（处置权在工作台/人工——reaper 自动改数会把 bug 掩埋成"自愈"）。

四类不变量：
1. orphan_objects        active 对象没有任何 active 别名（mint+alias 原子化后不应再产生）；
2. alias_open_case       同 (namespace, norm) 既有 active 别名又有 open case（已确认还挂队列）；
3. resolved_case_broken  resolved case 的 resolved_identifier_id 缺失或指向不存在的行；
4. active_alias_dead_target  active 别名指向非 active 对象（retire/merge 级联漏网）。

CLI：`python -m opensearch_pipeline.ontology.invariants`（只读；有违例 → exit 1）。
DataWorks 告警接入 user-gated，另行调度。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

__all__ = ["scan_invariants"]

_LIMIT = 200   # 每类上限——reaper 是告警器不是全量导出器


def scan_invariants(store) -> Dict[str, List[Dict[str, Any]]]:
    """四类不变量扫描。返回 {invariant: [violation, ...]}（全空=健康）。"""
    from opensearch_pipeline.ontology.store import RDSOntologyStore
    if isinstance(store, RDSOntologyStore):
        return _scan_rds(store)
    return _scan_memory(store)


def _scan_rds(store) -> Dict[str, List[Dict[str, Any]]]:
    db = store._db()
    conn = store._conn()
    out: Dict[str, List[Dict[str, Any]]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT o.object_id, o.canonical_ref FROM {db}.ontology_object o "
                f"WHERE o.status='active' AND NOT EXISTS ("
                f"  SELECT 1 FROM {db}.ontology_identifier i "
                f"  WHERE i.target_object_id=o.object_id AND i.status='active') "
                f"LIMIT {_LIMIT}")
            out["orphan_objects"] = [
                {"object_id": r[0], "canonical_ref": r[1]} for r in cur.fetchall()]
            cur.execute(
                f"SELECT c.case_id, c.namespace, c.norm_value "
                f"FROM {db}.ontology_resolution_case c "
                f"JOIN {db}.ontology_identifier i "
                f"  ON i.namespace=c.namespace AND i.norm_value=c.norm_value "
                f" AND i.status='active' "
                f"WHERE c.status='open' LIMIT {_LIMIT}")
            out["alias_open_case"] = [
                {"case_id": r[0], "namespace": r[1], "norm_value": r[2]}
                for r in cur.fetchall()]
            cur.execute(
                f"SELECT c.case_id FROM {db}.ontology_resolution_case c "
                f"LEFT JOIN {db}.ontology_identifier i "
                f"  ON i.identifier_id=c.resolved_identifier_id "
                f"WHERE c.status='resolved' AND (c.resolved_identifier_id IS NULL "
                f"  OR i.identifier_id IS NULL) LIMIT {_LIMIT}")
            out["resolved_case_broken"] = [{"case_id": r[0]} for r in cur.fetchall()]
            cur.execute(
                f"SELECT i.identifier_id, i.namespace, i.norm_value, o.status "
                f"FROM {db}.ontology_identifier i "
                f"JOIN {db}.ontology_object o ON o.object_id=i.target_object_id "
                f"WHERE i.status='active' AND o.status<>'active' LIMIT {_LIMIT}")
            out["active_alias_dead_target"] = [
                {"identifier_id": r[0], "namespace": r[1], "norm_value": r[2],
                 "target_status": r[3]} for r in cur.fetchall()]
        return out
    finally:
        conn.close()


def _scan_memory(store) -> Dict[str, List[Dict[str, Any]]]:
    with store._lock:
        objs = {oid: dict(o) for oid, o in store._objects.items()}
        idents = [dict(i) for i in store._identifiers.values()]
        cases = [dict(c) for c in store._cases.values()]
    active_targets = {i["target_object_id"] for i in idents if i["status"] == "active"}
    active_keys = {(i["namespace"], i["norm_value"]) for i in idents if i["status"] == "active"}
    ident_ids = {i["identifier_id"] for i in idents}
    return {
        "orphan_objects": [
            {"object_id": oid, "canonical_ref": o["canonical_ref"]}
            for oid, o in objs.items()
            if o["status"] == "active" and oid not in active_targets][:_LIMIT],
        "alias_open_case": [
            {"case_id": c["case_id"], "namespace": c["namespace"],
             "norm_value": c["norm_value"]}
            for c in cases if c["status"] == "open"
            and (c["namespace"], c["norm_value"]) in active_keys][:_LIMIT],
        "resolved_case_broken": [
            {"case_id": c["case_id"]} for c in cases if c["status"] == "resolved"
            and (not c.get("resolved_identifier_id")
                 or c["resolved_identifier_id"] not in ident_ids)][:_LIMIT],
        "active_alias_dead_target": [
            {"identifier_id": i["identifier_id"], "namespace": i["namespace"],
             "norm_value": i["norm_value"],
             "target_status": (objs.get(i["target_object_id"]) or {}).get("status")}
            for i in idents if i["status"] == "active"
            and (objs.get(i["target_object_id"]) or {}).get("status") != "active"][:_LIMIT],
    }


def main() -> int:
    import json

    from opensearch_pipeline.ontology.store import RDSOntologyStore
    report = scan_invariants(RDSOntologyStore())
    total = sum(len(v) for v in report.values())
    print(json.dumps({"violations": total, "detail": report}, ensure_ascii=False, indent=2))
    if total:
        print(f"❌ 本体不变量违例 {total} 条——请经工作台/人工处置（本工具只读不修数）")
        return 1
    print("✅ 本体不变量全部成立")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
