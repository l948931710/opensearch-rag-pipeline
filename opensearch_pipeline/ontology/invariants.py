# -*- coding: utf-8 -*-
"""
invariants.py — 本体不变量对账 reaper（PR-C，P0-05 验收④）。

原子化（store 复合写）之后残余的半状态只能来自历史脏数据/未知 bug——本模块周期性
扫出并**阻断级上报**（非零退出码），而不是让它们静默腐蚀身份脊柱。只读，绝不修数
（处置权在工作台/人工——reaper 自动改数会把 bug 掩埋成"自愈"）。

八类不变量（1-4 原有；5-8 = unknown-unknowns 批次3c 扩面，P1-09）：
1. orphan_objects        active 对象没有任何 active 别名（mint+alias 原子化后不应再产生）；
2. alias_open_case       同 (namespace, norm) 既有 active 别名又有 open case（已确认还挂队列）；
3. resolved_case_broken  resolved case 的 resolved_identifier_id 缺失或指向不存在的行；
4. active_alias_dead_target  active 别名指向非 active 对象（retire/merge 级联漏网）；
5. superseded_broken     identifier.superseded_by 悬空（指向不存在的行）或二元环——
                         schema/033 有意不给 superseded_by 加 FK（repoint 的 UPDATE→INSERT
                         顺序会 1452），链完整性只能靠本扫描；
6. active_link_dead_endpoint  active link 的 src/dst 非 active（写路径级联退役被绕过/历史行）；
7. normalized_title_null_active  active 对象 normalized_title IS NULL（038 回填缺口：
                         NULL 行不参与等值召回 → 同名漏归并 → 误铸重复品）；
8. population_snapshot_stale  源快照 snapshot_at 早于 RAG_ONTOLOGY_POPULATION_MAX_AGE_D
                         天（默认 35；<=0 关闭）——陈旧分母让 coverage 失真（P0-05 姊妹项）。
（auto 抽检超 SLA 扫描待批次3b 的 review 数据模型落地后加——TODO。）

CLI：`python -m opensearch_pipeline.ontology.invariants`（只读；有违例 → exit 1）。
DataWorks 告警接入 user-gated，另行调度。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

__all__ = ["scan_invariants"]

_LIMIT = 200   # 每类上限——reaper 是告警器不是全量导出器


def _population_max_age_d() -> int:
    try:
        return int(os.environ.get("RAG_ONTOLOGY_POPULATION_MAX_AGE_D", "35") or 35)
    except ValueError:
        return 35


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
            # 5) superseded_by 悬空 / 二元环（033 有意无 FK——链完整性只能靠扫描）
            cur.execute(
                f"SELECT i.identifier_id, i.superseded_by FROM {db}.ontology_identifier i "
                f"LEFT JOIN {db}.ontology_identifier s ON s.identifier_id=i.superseded_by "
                f"WHERE i.superseded_by IS NOT NULL AND s.identifier_id IS NULL "
                f"LIMIT {_LIMIT}")
            broken = [{"identifier_id": r[0], "superseded_by": r[1], "reason": "dangling"}
                      for r in cur.fetchall()]
            cur.execute(
                f"SELECT a.identifier_id, a.superseded_by FROM {db}.ontology_identifier a "
                f"JOIN {db}.ontology_identifier b ON b.identifier_id=a.superseded_by "
                f"WHERE (b.superseded_by=a.identifier_id AND a.identifier_id<b.identifier_id) "
                f"   OR a.superseded_by=a.identifier_id LIMIT {_LIMIT}")
            broken += [{"identifier_id": r[0], "superseded_by": r[1], "reason": "cycle"}
                       for r in cur.fetchall()]
            out["superseded_broken"] = broken[:_LIMIT]
            # 6) active link 的 src/dst 非 active（级联退役被绕过/历史行）
            cur.execute(
                f"SELECT l.link_id, l.link_type, l.src_object_id, l.dst_object_id "
                f"FROM {db}.ontology_link l "
                f"LEFT JOIN {db}.ontology_object s ON s.object_id=l.src_object_id "
                f"LEFT JOIN {db}.ontology_object d ON d.object_id=l.dst_object_id "
                f"WHERE l.status='active' AND (COALESCE(s.status,'missing')<>'active' "
                f"   OR COALESCE(d.status,'missing')<>'active') LIMIT {_LIMIT}")
            out["active_link_dead_endpoint"] = [
                {"link_id": r[0], "link_type": r[1], "src_object_id": r[2],
                 "dst_object_id": r[3]} for r in cur.fetchall()]
            # 7) 038 回填缺口：active 对象 normalized_title 为 NULL（等值召回失明面）
            cur.execute(
                f"SELECT o.object_id, o.object_type FROM {db}.ontology_object o "
                f"WHERE o.status='active' AND o.normalized_title IS NULL LIMIT {_LIMIT}")
            out["normalized_title_null_active"] = [
                {"object_id": r[0], "object_type": r[1]} for r in cur.fetchall()]
            # 8) 源快照陈旧（分母失真面；<=0 关闭）
            max_age = _population_max_age_d()
            if max_age > 0:
                cur.execute(
                    f"SELECT namespace, source, snapshot_at "
                    f"FROM {db}.ontology_source_population "
                    f"WHERE snapshot_at < DATE_SUB(NOW(3), INTERVAL %s DAY) "
                    f"LIMIT {_LIMIT}", (max_age,))
                out["population_snapshot_stale"] = [
                    {"namespace": r[0], "source": r[1], "snapshot_at": str(r[2])}
                    for r in cur.fetchall()]
            else:
                out["population_snapshot_stale"] = []
        return out
    finally:
        conn.close()


def _scan_memory(store) -> Dict[str, List[Dict[str, Any]]]:
    with store._lock:
        objs = {oid: dict(o) for oid, o in store._objects.items()}
        idents = [dict(i) for i in store._identifiers.values()]
        cases = [dict(c) for c in store._cases.values()]
        links = [dict(link) for link in store._links.values()]
    active_targets = {i["target_object_id"] for i in idents if i["status"] == "active"}
    active_keys = {(i["namespace"], i["norm_value"]) for i in idents if i["status"] == "active"}
    ident_ids = {i["identifier_id"] for i in idents}
    sup = {i["identifier_id"]: i.get("superseded_by") for i in idents if i.get("superseded_by")}
    superseded_broken = (
        [{"identifier_id": iid, "superseded_by": tgt, "reason": "dangling"}
         for iid, tgt in sup.items() if tgt not in ident_ids]
        + [{"identifier_id": iid, "superseded_by": tgt, "reason": "cycle"}
           for iid, tgt in sup.items()
           if tgt in sup and (sup.get(tgt) == iid and iid < tgt or tgt == iid)])

    def _obj_status(oid):
        return (objs.get(oid) or {}).get("status")

    return {
        "superseded_broken": superseded_broken[:_LIMIT],
        "active_link_dead_endpoint": [
            {"link_id": link["link_id"], "link_type": link.get("link_type"),
             "src_object_id": link.get("src_object_id"),
             "dst_object_id": link.get("dst_object_id")}
            for link in links if link.get("status") == "active"
            and (_obj_status(link.get("src_object_id")) != "active"
                 or _obj_status(link.get("dst_object_id")) != "active")][:_LIMIT],
        "normalized_title_null_active": [
            {"object_id": oid, "object_type": o.get("object_type")}
            for oid, o in objs.items()
            if o.get("status") == "active" and not o.get("normalized_title")][:_LIMIT],
        # Memory twin 的 snapshot_at 是逻辑 tick 非墙钟——staleness 不可判，恒空
        # （RDS 侧才是生产判据；单测经 RDS 桩/真库覆盖）。
        "population_snapshot_stale": [],
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
