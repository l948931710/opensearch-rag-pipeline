# -*- coding: utf-8 -*-
"""verify_acl_coverage.py — dept→ACL组 覆盖率现网核查(prod 只读,零写入)。

用途:`RAG_ACL_ANCESTRY` 翻转前/后各跑一次做对照,或日常体检发现「组织漂移导致失映射」。
2026-08-04 建:那次漂移(资材部→采购部 等 9 个死键)是被 supply 受众=0 偶然发现的,
此前无任何常规手段能看见 —— 本脚本就是补上那个可观测面。

判据全部来自权威表(dept_dim/staff_dim 快照 + 代码里的映射常量),不做投影推断。
输出三段:
  A. 名字表 keys 与 dept_dim 现名的失配(死键 = 改名/重组的直接信号)
  B. 三口径覆盖对比:名字口径 / 祖先制(现锚) —— 无组员工数与按顶层子树的残余分布
  C. 各 ACL 组的受众人数(supply=0 这类归零信号一眼可见)

⚠️ 口径边界:本脚本读 RDS 的 dept_dim/staff_dim(org_sync 日快照),而 serving 运行时
   走钉钉活 API。两者应当一致,不一致本身即为发现(org_sync 未跑/延迟)。
⚠️ 不模拟 seeded user_role 覆盖(个人级授权优先于自动映射),故受众数是下界。

用法:
    python3 scripts/verify_acl_coverage.py            # 名字口径(现网 flag OFF 的行为)
    python3 scripts/verify_acl_coverage.py --ancestry # 额外跑祖先制口径做对照
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opensearch_pipeline import prod_access
from opensearch_pipeline.dept_ancestry import (
    ANCHOR_GROUPS_BY_DEPT_ID, build_parent_index, parent_getter_from_index, resolve_dept_ids)
from opensearch_pipeline.dingtalk_identity import _DEPT_NAME_TO_GROUPS, _PRODUCTION_WORKSHOP_DEPTS
from opensearch_pipeline.retriever import _VALID_ACL_GROUPS, groups_that_can_read_owner


def _load():
    conn = prod_access.get_prod_readonly_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT dept_id, parent_id, name FROM dept_dim WHERE is_active=1")
            depts = cur.fetchall()
            cur.execute("SELECT staff_id, dept_ids FROM staff_dim WHERE is_active=1")
            staff = cur.fetchall()
            cur.execute("""SELECT owner_dept, COUNT(*) c FROM document_meta
                           WHERE permission_level='dept_internal' GROUP BY owner_dept""")
            owners = {r["owner_dept"] or "NULL": r["c"] for r in cur.fetchall()}
    finally:
        conn.close()
    return depts, staff, owners


def _name_groups(name, *, expand_star=True):
    """部门名 → 组集合。expand_star=False 时【不展开】"*" 全组哨兵。

    ⚠️ 两个口径都必须有:运行时确实把 "*" 展开为全量白名单(总经办类全可见),所以算
    "谁能读到这篇文档" 必须展开;但算 "这个组是否失去了它的本职受众" 必须【不】展开——
    否则只要总经办还在,任何组都不会归零,归零告警永远不会响(2026-08-04 的漂移
    正是这样从 supply 受众数里溜过去的:附件脚本没展开哨兵才偶然看见 0)。
    """
    if name in _DEPT_NAME_TO_GROUPS:
        m = _DEPT_NAME_TO_GROUPS[name]
        if "*" in m:
            return set(sorted(_VALID_ACL_GROUPS)) if expand_star else set()
        return set(m)
    return {"production"} if name in _PRODUCTION_WORKSHOP_DEPTS else set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ancestry", action="store_true", help="额外用锚表口径跑一遍做对照")
    ap.add_argument("--json", action="store_true", help="只输出 JSON(便于留痕/diff)")
    args = ap.parse_args()

    depts, staff, owner_docs = _load()
    by_id = {r["dept_id"]: r for r in depts}
    name_by_id = {r["dept_id"]: r["name"] for r in depts}
    get_parent = parent_getter_from_index(build_parent_index(depts))

    def ids_of(s):
        return [int(t) for t in (s["dept_ids"] or "").split(",") if t.strip().isdigit()]

    def root_of(dept_id):
        seen, cur = set(), dept_id
        while cur in by_id and cur not in seen:
            seen.add(cur)
            nxt = by_id[cur]["parent_id"]
            if nxt not in by_id:
                break
            cur = nxt
        return cur

    out = {}

    # ── A. 名字表死键 ──
    active_names = collections.Counter(r["name"] for r in depts)
    staff_per_dept = collections.Counter()
    for s in staff:
        for d in ids_of(s):
            staff_per_dept[d] += 1
    staff_per_name = collections.Counter()
    for r in depts:
        staff_per_name[r["name"]] += staff_per_dept.get(r["dept_id"], 0)
    dead = {k: sorted(v) if not isinstance(v, str) else v
            for k, v in _DEPT_NAME_TO_GROUPS.items() if active_names.get(k, 0) == 0}
    out["A_dead_name_keys"] = dead
    out["A_dead_key_count"] = len(dead)

    # ── B. 覆盖率 ──
    def groups_name_path(s, *, expand_star=True):
        g = set()
        for d in ids_of(s):
            if d in name_by_id:
                g |= _name_groups(name_by_id[d], expand_star=expand_star)
        return g

    def groups_ancestry(s, *, expand_star=True):
        ids = ids_of(s)
        codes, partial, undecided = resolve_dept_ids(
            [str(i) for i in ids], get_parent, anchors=ANCHOR_GROUPS_BY_DEPT_ID)
        if partial or (not codes and undecided):
            return groups_name_path(s, expand_star=expand_star)   # 与接线层同契约:兜底名字口径
        if not expand_star and set(codes) == set(_VALID_ACL_GROUPS):
            return set()                    # 全组=哨兵展开的结果,本职受众口径下不计
        return set(codes)

    modes = {"name_path": groups_name_path}
    if args.ancestry:
        modes["ancestry"] = groups_ancestry

    out["B_coverage"] = {}
    resolved = {}
    for label, fn in modes.items():
        gs = {s["staff_id"]: fn(s) for s in staff}
        resolved[label] = gs
        residual = collections.Counter()
        for s in staff:
            if gs[s["staff_id"]]:
                continue
            for rt in {name_by_id.get(root_of(d), f"⟨{d}⟩") for d in ids_of(s)} or {"⟨无部门⟩"}:
                residual[rt] += 1
        out["B_coverage"][label] = {
            "staff_active": len(staff),
            "no_group": sum(1 for g in gs.values() if not g),
            "residual_by_root_tree": dict(residual.most_common()),
        }

    # ── C. 各组受众 + 该组 owner 的文档数 ──
    # staff_any      = 运行时真能读到该组内容的人数(含总经办等 "*" 全可见单位)
    # staff_specific = 靠【本职映射】拿到该组的人数(不含哨兵)——归零告警只看这个,
    #                  否则只要总经办还在,任何组都不会归零,漏映射永远告不出来。
    out["C_audience"] = {}
    for label, fn in modes.items():
        specific = {s["staff_id"]: fn(s, expand_star=False) for s in staff}
        gs = resolved[label]
        aud = {}
        for grp in sorted(_VALID_ACL_GROUPS):
            aud[grp] = {
                "staff_any": sum(1 for g in gs.values() if grp in g),
                "staff_specific": sum(1 for g in specific.values() if grp in g),
                "readable_dept_internal_docs": sum(
                    c for o, c in owner_docs.items()
                    if o != "NULL" and grp in groups_that_can_read_owner(o)),
            }
        out["C_audience"][label] = aud
        out["C_audience"][label + "_ZERO_AUDIENCE_ALERT"] = [
            g for g, v in aud.items()
            if v["staff_specific"] == 0 and v["readable_dept_internal_docs"]]

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    # 有文档却零受众 = 一定有人失映射 → 非零退出,便于挂进巡检
    alerts = [v for k, v in out["C_audience"].items() if k.endswith("_ZERO_AUDIENCE_ALERT") and v]
    return 2 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
