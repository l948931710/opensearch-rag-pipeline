# -*- coding: utf-8 -*-
"""org_sync.py — 钉钉组织架构 → RDS 快照(dept_dim / staff_dim)。

**为什么必须有**:node-ACL 的判定式是「用户祖先链 ∩ 文档授权节点集」。算祖先链要知道
部门父子关系、定位用户要知道人→部门映射;这些只有钉钉有,但每次问答都去调钉钉不现实
(一次链解析最多 5 跳、每跳一次 HTTP,还有限流)。⇒ 落一份本地快照。
**表空 = 节点通道恒关**(`dingtalk_identity.resolve_acl_context` 返回 node_channel_ok=False)。

三个消费方共用同一份快照:
  1. node-ACL 祖先链解析(读路径地基);
  2. 管理台组织树选择器 —— ⚠️ 现有 `/api/kb/org-tree` 读 `scratch/dingtalk_org_tree.json`,
     而 scratch/ 被 .gitignore + .dockerignore 双排除、Dockerfile 只 COPY opensearch_pipeline/
     ⇒ **2026-07-23 起的镜像里该文件不存在,生产恒返回 null**;
  3. AI 奖励统计的部门在册人数分母。

设计要点:
  · **不硬删**:同步中消失的部门/人置 is_active=0。删行会让"授权给已删节点"的文档
    **静默对所有人不可见且查不出原因**;保留行才能检出孤儿授权并告警。
  · **snapshot_rev 当缓存键**:某人被调出子树后,若后代集缓存不失效,他仍能管辖原文档。
    批次号进缓存键 ⇒ 组织一变缓存自动失效。
  · **只存 id 与部门归属,不存员工姓名**(仓库 public,姓名不进代码面;部门名进 RDS 供 UI)。
  · **dry-run 默认**:与 retention/apply_migration 同纪律,真写需 --commit。

用法:
    python -m opensearch_pipeline.org_sync                  # dry-run:只报差异
    RAG_ENV=production python -m opensearch_pipeline.org_sync --commit
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

ROOT_DEPT_ID = 1
_MAX_DEPTH = 15          # 防异常数据无界递归(现网实测最深 5 层)


# ── 钉钉只读拉取 ──────────────────────────────────────────────────────────────
def _token() -> str:
    import requests
    key = os.environ.get("DINGTALK_CLIENT_ID")
    sec = os.environ.get("DINGTALK_CLIENT_SECRET")
    if not (key and sec):
        raise RuntimeError("缺 DINGTALK_CLIENT_ID/SECRET")
    r = requests.post("https://api.dingtalk.com/v1.0/oauth2/accessToken",
                      json={"appKey": key, "appSecret": sec}, timeout=15)
    tok = (r.json() or {}).get("accessToken")
    if not tok:
        raise RuntimeError(f"accessToken 获取失败: {r.status_code} {r.text[:200]}")
    return tok


def _listsub(tok: str, dept_id: int) -> List[dict]:
    import requests
    r = requests.post(
        f"https://oapi.dingtalk.com/topapi/v2/department/listsub?access_token={tok}",
        json={"dept_id": dept_id}, timeout=20).json()
    if r.get("errcode") != 0:
        raise RuntimeError(f"listsub({dept_id}) errcode={r.get('errcode')} {r.get('errmsg')}")
    return r.get("result") or []


def _member_ids(tok: str, dept_id: int) -> List[str]:
    """该部门直属成员 userid(用 user/listid:**只返 id、不返姓名**,权限最低)。"""
    import requests
    r = requests.post(
        f"https://oapi.dingtalk.com/topapi/user/listid?access_token={tok}",
        json={"dept_id": dept_id}, timeout=25).json()
    if r.get("errcode") != 0:
        raise RuntimeError(f"user/listid({dept_id}) errcode={r.get('errcode')} {r.get('errmsg')}")
    return (r.get("result") or {}).get("userid_list") or []


def fetch_org(tok: Optional[str] = None) -> Tuple[Dict[int, dict], Dict[str, Set[int]]]:
    """遍历全树 → ({dept_id: {parent_id,name,depth}}, {staff_id: {dept_id,...}})。

    ⚠️ 任一接口报错都**上抛**:半截快照比没有快照更危险(会把真实在册部门判成"已消失"
    从而置 is_active=0,进而误判孤儿授权)。宁可整轮失败、保留上一轮快照。
    """
    tok = tok or _token()
    depts: Dict[int, dict] = {}
    stack = [(ROOT_DEPT_ID, 0)]
    while stack:
        did, depth = stack.pop()
        if depth > _MAX_DEPTH:
            raise RuntimeError(f"组织树深度超过 {_MAX_DEPTH},疑数据异常(环?)")
        for sub in _listsub(tok, did):
            sid = int(sub["dept_id"])
            if sid in depts:                       # 环/重复:同上,宁可失败
                raise RuntimeError(f"部门 {sid} 重复出现,疑组织树成环")
            depts[sid] = {"parent_id": did, "name": (sub.get("name") or "").strip(),
                          "depth": depth + 1}
            stack.append((sid, depth + 1))

    staff: Dict[str, Set[int]] = {}
    for did in depts:
        for uid in _member_ids(tok, did):
            staff.setdefault(uid, set()).add(did)
        time.sleep(0.02)                            # 轻微退避,避免触发限流
    return depts, staff


# ── 落库 ─────────────────────────────────────────────────────────────────────
def _next_rev(cur) -> int:
    cur.execute("SELECT COALESCE(MAX(snapshot_rev), 0) + 1 FROM dept_dim")
    row = cur.fetchone()
    return int(row[0] if row and row[0] else 1)


def sync(commit: bool = False) -> dict:
    """拉取 + 落库。dry-run 只报差异,不写任何行。返回统计 dict(供调度器判定与告警)。"""
    from opensearch_pipeline.db import _get_db_conn

    depts, staff = fetch_org()
    logger.info("拉取完成:部门 %d / 员工 %d", len(depts), len(staff))

    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            rev = _next_rev(cur)
            cur.execute("SELECT dept_id FROM dept_dim WHERE is_active=1")
            old_depts = {int(r[0]) for r in cur.fetchall()}
            cur.execute("SELECT staff_id FROM staff_dim WHERE is_active=1")
            old_staff = {r[0] for r in cur.fetchall()}

        new_depts, new_staff = set(depts), set(staff)
        stat = {
            "snapshot_rev": rev, "commit": commit,
            "dept_total": len(depts), "staff_total": len(staff),
            "dept_added": sorted(new_depts - old_depts),
            "dept_gone": sorted(old_depts - new_depts),
            "staff_added": len(new_staff - old_staff),
            "staff_gone": len(old_staff - new_staff),
            "orphan_grants": [],
        }

        # 孤儿授权:文档授权指向了本轮已消失的节点 ⇒ 该文档经由此授权对所有人不可见。
        # **只告警不自动删**(可能是组织临时调整;自动删会把管理员的意图悄悄抹掉)。
        if stat["dept_gone"]:
            with conn.cursor() as cur:
                ph = ",".join(["%s"] * len(stat["dept_gone"]))
                try:
                    cur.execute(
                        f"SELECT doc_id, dept_id FROM kb_doc_node_grant "
                        f"WHERE revoked_at IS NULL AND dept_id IN ({ph})",
                        tuple(stat["dept_gone"]))
                    stat["orphan_grants"] = [(r[0], int(r[1])) for r in cur.fetchall()]
                except Exception as e:   # noqa: BLE001 — 060 未 apply 时表不存在
                    logger.debug("孤儿授权检查跳过(表不存在?): %s", e)

        if not commit:
            return stat

        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO dept_dim (dept_id, parent_id, name, depth, is_active, snapshot_rev) "
                "VALUES (%s,%s,%s,%s,1,%s) ON DUPLICATE KEY UPDATE "
                "parent_id=VALUES(parent_id), name=VALUES(name), depth=VALUES(depth), "
                "is_active=1, snapshot_rev=VALUES(snapshot_rev)",
                [(d, v["parent_id"], v["name"], v["depth"], rev) for d, v in depts.items()])
            cur.executemany(
                "INSERT INTO staff_dim (staff_id, dept_ids, primary_dept, is_active, snapshot_rev) "
                "VALUES (%s,%s,%s,1,%s) ON DUPLICATE KEY UPDATE "
                "dept_ids=VALUES(dept_ids), primary_dept=VALUES(primary_dept), "
                "is_active=1, snapshot_rev=VALUES(snapshot_rev)",
                [(s, ",".join(str(x) for x in sorted(ds)), min(ds) if ds else None, rev)
                 for s, ds in staff.items()])
            # 本轮未出现 ⇒ 置 0(**不删行**:留着才能检出孤儿授权、才能解释"为何不可见")
            cur.execute("UPDATE dept_dim SET is_active=0 WHERE snapshot_rev<%s AND is_active=1", (rev,))
            stat["dept_deactivated"] = cur.rowcount
            cur.execute("UPDATE staff_dim SET is_active=0 WHERE snapshot_rev<%s AND is_active=1", (rev,))
            stat["staff_deactivated"] = cur.rowcount
        conn.commit()
        return stat
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="钉钉组织架构 → RDS 快照(dept_dim/staff_dim)")
    ap.add_argument("--commit", action="store_true", help="真写(默认 dry-run 只报差异)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        st = sync(commit=args.commit)
    except Exception as e:   # noqa: BLE001 — 半截快照比没有更危险,整轮失败保留上轮
        print(f"❌ 同步失败(保留上一轮快照): {e}", file=sys.stderr)
        return 3
    mode = "COMMIT" if st["commit"] else "DRY-RUN"
    print(f"[{mode}] rev={st['snapshot_rev']}  部门 {st['dept_total']}  员工 {st['staff_total']}")
    print(f"  新增部门 {len(st['dept_added'])}  消失部门 {len(st['dept_gone'])}  "
          f"新增员工 {st['staff_added']}  离场员工 {st['staff_gone']}")
    if st["dept_gone"]:
        print(f"  ⚠️ 消失部门 id: {st['dept_gone']}")
    if st["orphan_grants"]:
        print(f"  🔴 孤儿授权 {len(st['orphan_grants'])} 条(文档授权指向已消失节点,"
              f"经此授权对所有人不可见)——**只告警不自动删**:")
        for doc_id, dept_id in st["orphan_grants"][:20]:
            print(f"     {doc_id} → dept {dept_id}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ── 组织树读侧(管理台选择器 / 奖励统计分母共用)────────────────────────────────
_TREE_CACHE: dict = {}


def _tree_cache_ttl_s() -> float:
    try:
        return float(os.environ.get("RAG_ORG_TREE_TTL_S", "300") or 0)
    except ValueError:
        return 300.0


# ── 后代展开读侧(阶段 B 管理轴:dept_admin 管辖根 → 子树后代集)─────────────────
# 缓存值是**原子三元组** (snapshot_rev, fresh, children_index)——一次查询一次赋值,
# 绝不分字段更新:组织同步落新 rev 的瞬间,读到的要么整套旧、要么整套新,不存在
# "rev 是新的、children 还是旧的"的混合态(混合态会把已移出子树的部门交给旧管理员)。
_CHILDREN_CACHE: dict = {}


class OrgSnapshotUnavailable(RuntimeError):
    """组织快照不可用(表空/读失败/>48h 过期)——调用方 fail-closed:
    dept_admin 的 node 管辖腿整体失效(kb_admin 不受影响),**绝不回退 legacy 管辖**。"""


def load_children_index() -> Tuple[int, bool, Dict[int, List[int]]]:
    """dept_dim(is_active=1)→ (snapshot_rev, fresh, parent→children 索引)。

    fresh=False ⇔ 快照 >48h(与 dingtalk_identity/load_org_tree 同门槛)——调用方须视同
    不可用(设计裁决:快照过期时自动根与后代展开**同时失效**)。表空/读失败直接抛
    OrgSnapshotUnavailable,不返回半截数据。TTL 进程缓存与 load_org_tree 同旋钮
    (RAG_ORG_TREE_TTL_S);缓存的是含 fresh 的整组值,fresh 判定在**入缓存前**做——
    TTL(默认 300s)远小于 48h 门槛,过期误差最多一个 TTL,可接受。
    """
    ttl = _tree_cache_ttl_s()
    now = time.monotonic()
    hit = _CHILDREN_CACHE.get("v")
    if hit and ttl > 0 and now - hit[0] < ttl:
        return hit[1]

    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dept_id, parent_id, snapshot_rev, "
                    "  TIMESTAMPDIFF(HOUR, synced_at, NOW()) "
                    "FROM dept_dim WHERE is_active=1")
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        raise OrgSnapshotUnavailable(f"dept_dim 读取失败: {e}") from e
    if not rows:
        raise OrgSnapshotUnavailable("dept_dim 为空(组织快照未灌)")

    children: Dict[int, List[int]] = {}
    for r in rows:
        children.setdefault(int(r[1]), []).append(int(r[0]))
    age_h = rows[0][3]
    value = (int(rows[0][2] or 0), bool(age_h is not None and age_h <= 48), children)
    _CHILDREN_CACHE["v"] = (now, value)
    return value


def _children_cache_clear() -> None:
    """测试/组织同步后复位(conftest 每测清空复用)。"""
    _CHILDREN_CACHE.clear()
    _TREE_CACHE.clear()


def load_org_tree() -> dict:
    """RDS 快照 → 管理台组织树选择器载荷(**扁平列表**,前端自行建树)。

    返回 {"nodes":[{dept_id,parent_id,name,depth,staff_count,direct_staff_count}],
          "snapshot_rev", "synced_at", "stale": bool, "staff_total"}。表空/异常 → nodes=[]
    且 stale=True(前端据此提示"组织数据未同步",而不是画一棵空树让人以为公司没部门)。

    **两个人数各对应一种授权语义,前端两个都要用**:

    · `staff_count` = **子树总人数**(本节点直属 + 全部后代),对应「含下级」= `d:<id>`。
      **这个数是防呆的关键**:授权到比员工挂载点更深的空节点时显示 0,当场就能发现
      (实测 119 个部门里 L4/L5 共 17 个,人都挂在更浅的层)。
    · `direct_staff_count` = **仅直挂本节点**的人数,对应「仅本级」= `dx:<id>`。
      2026-08-01 补:此前只回子树数 ⇒ 前端把「仅本级」项一律按 0 计入人数估算,
      勾了中心「仅本级」人数纹丝不动、看着像白勾(生产中心直挂 4 人、注塑事业部 8 人,
      全被显示成 0)。**现网 26 个节点有子部门却仍有直挂人员、合计 171 人次**,这不是边角。
    """
    ttl = _tree_cache_ttl_s()
    now = time.monotonic()
    hit = _TREE_CACHE.get("v")
    if hit and ttl > 0 and now - hit[0] < ttl:
        return hit[1]

    out = {"nodes": [], "snapshot_rev": 0, "synced_at": "", "stale": True, "staff_total": 0}
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dept_id, parent_id, name, depth, snapshot_rev, "
                    "  TIMESTAMPDIFF(HOUR, synced_at, NOW()), synced_at "
                    "FROM dept_dim WHERE is_active=1")
                rows = cur.fetchall()
                cur.execute("SELECT dept_ids FROM staff_dim WHERE is_active=1")
                staff_rows = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 — 表未 apply / DB 抖动 ⇒ stale 空树,不抛
        logger.warning("load_org_tree 失败(返回空树 + stale): %s", e)
        return out
    if not rows:
        return out

    parent = {int(r[0]): int(r[1]) for r in rows}
    # 子树人数:每个人沿自己每个直属部门的父链一路 +1(按人去重,避免多部门重复计)
    direct: Dict[int, Set[str]] = {}
    for idx, csv in enumerate(staff_rows):
        for part in str(csv or "").split(","):
            part = part.strip()
            if part.isdigit():
                direct.setdefault(int(part), set()).add(str(idx))
    subtree: Dict[int, Set[str]] = {}
    for did, people in direct.items():
        cur_id, hops = did, 0
        while cur_id and cur_id != ROOT_DEPT_ID and hops <= _MAX_DEPTH:
            subtree.setdefault(cur_id, set()).update(people)
            cur_id = parent.get(cur_id, ROOT_DEPT_ID)
            hops += 1

    age_h = rows[0][5] if rows and rows[0][5] is not None else None
    out["nodes"] = sorted(
        ({"dept_id": int(r[0]), "parent_id": int(r[1]), "name": r[2] or "",
          "depth": int(r[3] or 0), "staff_count": len(subtree.get(int(r[0]), ())),
          # `direct` 本来就为算子树顺带建好了,这里只是把它也回出去(零额外查询)
          "direct_staff_count": len(direct.get(int(r[0]), ()))}
         for r in rows),
        key=lambda n: (n["depth"], -n["staff_count"], n["dept_id"]))
    out["snapshot_rev"] = int(rows[0][4] or 0)
    out["synced_at"] = str(rows[0][6] or "")
    out["staff_total"] = len({p for s in subtree.values() for p in s})
    # 与 dingtalk_identity 同门槛:>48h 视为过期(节点通道届时亦 fail-closed)
    out["stale"] = age_h is None or age_h > 48
    _TREE_CACHE["v"] = (now, out)
    return out
