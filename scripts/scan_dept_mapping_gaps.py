# -*- coding: utf-8 -*-
"""scan_dept_mapping_gaps.py — 全钉钉组织树 × ACL 映射覆盖扫描（只读）。

回答：「哪些部门【未命中映射】且【有在册用户】？」——设计新权限映射前的地基盘点。

三个数据面：
  A. 钉钉组织树全遍历（department/listsub 递归 + user/listid 逐部门直属人数）：
     每个部门按 dingtalk_identity 的现行三层规则分类——
       explicit  = 命中 _DEPT_NAME_TO_GROUPS（显式映射）
       umbrella  = 命中 _PRODUCTION_WORKSHOP_DEPTS（生产中心子树 → production 伞组）
       passthru  = 名字本身就是合法组码（罕见）
       UNMAPPED  = 三者皆未命中 → 该部门用户 fail-closed 仅 public
  B. RDS user_role（is_active=1）逐行归一化：dept_code CSV 全未命中 → 该用户现在
     只看得到 public——这是「真实受影响的在册用户」权威名单（比部门人数更准：
     多部门用户任一部门命中即有组）。
  C. 映射死键：_DEPT_NAME_TO_GROUPS / _PRODUCTION_WORKSHOP_DEPTS 里在现网树上
     已不存在的名字（组织调整留下的漂移，重设计时可清理）。

READ ONLY：钉钉只调 listsub / listid / oauth2 token；RDS 只 SELECT；不落任何写。
用法：python3 scripts/scan_dept_mapping_gaps.py [--no-db] [--out DIR]
需要 .env 里的 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET（+ RDS 凭证，Part B）。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# RAG_SCAN_REPO_ROOT：从别处（如 worktree）调用时指向真正带 .env.prod_ro 的主 checkout，
# 让 opensearch_pipeline.config 的 RAG_ENV overlay 从那里解析（Part B 连生产只读）。
_REPO_ROOT = Path(os.environ.get("RAG_SCAN_REPO_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(_REPO_ROOT))

ROOT_DEPT_ID = 1          # 钉钉根部门恒为 1
SLEEP_S = 0.05            # 温和限速（topapi QPS 富裕，纯保险）


def _load_env() -> None:
    # worktree 里没有 .env（gitignored）→ 兜底读主 checkout 的（凭证单一来源）。
    candidates = [_REPO_ROOT / ".env",
                  Path("/Users/laijunchen/Projects/opensearch-rag-pipeline/.env")]
    env = next((p for p in candidates if p.exists()), None)
    if env is None:
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _access_token() -> str:
    resp = requests.post(
        "https://api.dingtalk.com/v1.0/oauth2/accessToken",
        json={"appKey": os.environ["DINGTALK_CLIENT_ID"],
              "appSecret": os.environ["DINGTALK_CLIENT_SECRET"]},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["accessToken"]


def _listsub(token: str, dept_id: int) -> list:
    r = requests.post(
        f"https://oapi.dingtalk.com/topapi/v2/department/listsub?access_token={token}",
        json={"dept_id": dept_id}, timeout=10,
    ).json()
    return r.get("result", []) if r.get("errcode") == 0 else []


def _dept_userids(token: str, dept_id: int) -> list:
    """该部门【直属】成员 userid 列表（不递归；user/listid 一次全量返回）。"""
    r = requests.post(
        f"https://oapi.dingtalk.com/topapi/user/listid?access_token={token}",
        json={"dept_id": dept_id}, timeout=10,
    ).json()
    if r.get("errcode") == 0:
        return (r.get("result") or {}).get("userid_list") or []
    return []


def _dept_name(token: str, dept_id: int) -> str:
    """department/get 取部门名（根部门 listsub 不含自身，报告行需要真名）。失败回空。"""
    r = requests.post(
        f"https://oapi.dingtalk.com/topapi/v2/department/get?access_token={token}",
        json={"dept_id": dept_id}, timeout=10,
    ).json()
    if r.get("errcode") == 0:
        return ((r.get("result") or {}).get("name") or "").strip()
    return ""


def walk_tree(token: str):
    """BFS 全树。返回 [{dept_id, name, path, member_ids}]（含根部门自身及其直属成员——
    高管/新员工常直挂公司根节点，listsub 只给子部门，漏掉根行会系统性少算
    「仅 public」人群，而那正是本扫描要找的）。"""
    out = []
    root_name = _dept_name(token, ROOT_DEPT_ID) or "（根部门）"
    root_ids = _dept_userids(token, ROOT_DEPT_ID)
    time.sleep(SLEEP_S)
    out.append({"dept_id": ROOT_DEPT_ID, "name": root_name, "path": root_name,
                "member_ids": root_ids})
    queue = [(ROOT_DEPT_ID, "")]          # (dept_id, parent_path)
    seen = set()
    while queue:
        dept_id, ppath = queue.pop(0)
        if dept_id in seen:
            continue
        seen.add(dept_id)
        subs = _listsub(token, dept_id)
        time.sleep(SLEEP_S)
        for d in subs:
            nm = (d.get("name") or "").strip()
            did = d["dept_id"]
            path = f"{ppath}/{nm}" if ppath else nm
            ids = _dept_userids(token, did)
            time.sleep(SLEEP_S)
            out.append({"dept_id": did, "name": nm, "path": path, "member_ids": ids})
            queue.append((did, path))
    return out


def classify(name: str, explicit: dict, umbrella: frozenset, valid: frozenset) -> tuple:
    """按现行 _normalize_dept_to_codes 的裁决顺序分类单个部门名 → (kind, groups)。"""
    if name in explicit:
        if "*" in explicit[name]:      # 全组哨兵（总经办类）：运行时展开为全量白名单，
            return "explicit", sorted(valid)   # 报告必须同口径——否则最高权限映射被误报成 []
        return "explicit", [c for c in explicit[name] if c in valid]
    if name in umbrella:
        return "umbrella", ["production"]
    if name in valid:
        return "passthru", [name]
    return "UNMAPPED", []


def scan_user_role(normalize) -> tuple:
    """Part B：user_role 在册用户逐行归一化，【只聚合、零 PII 落盘】。

    刻意不取 user_id/user_name：本报告回答"多少在册用户、卡在哪些部门名上"，
    按 dept_code（部门名 CSV）聚合计数即可；个人名单不属于本报告的输出面。
    返回 (impacted_by_dept: {dept_code_csv: count}, impacted_total, total_active, err)。
    """
    try:
        from opensearch_pipeline.config import get_config
        from opensearch_pipeline.db import _get_db_conn
        kb_db = get_config().rds.database
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT dept_code FROM {kb_db}.user_role WHERE is_active = 1")
                rows = cur.fetchall()
        finally:
            conn.close()
        by_dept: dict = {}
        impacted_total = 0
        for (dept_code,) in rows:
            if normalize(dept_code):
                continue
            impacted_total += 1
            key = (dept_code or "").strip() or "（空 dept_code）"
            by_dept[key] = by_dept.get(key, 0) + 1
        return by_dept, impacted_total, len(rows), None
    except Exception as e:  # noqa: BLE001 —— 网络/白名单不可达时如实报告，不装成"无数据"
        return {}, 0, 0, f"{type(e).__name__}: {e}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-db", action="store_true", help="跳过 RDS user_role 扫描（Part B）")
    ap.add_argument("--out", default=str(_REPO_ROOT / "scratch" / f"dept_mapping_scan_{datetime.now():%Y%m%d}"))
    args = ap.parse_args()

    _load_env()
    from opensearch_pipeline.dingtalk_identity import (  # noqa: E402
        _DEPT_NAME_TO_GROUPS, _PRODUCTION_WORKSHOP_DEPTS, _normalize_dept_to_codes,
    )
    # 白名单单一来源，绝不带本地 fallback 副本：漂移检测工具自己拿着一份会漂移的
    # 复制品（上一版 10 组 fallback 在 15 组扩容后已经陈旧）比 import 失败更糟——
    # import 失败会硬崩（上面 dingtalk_identity 的 import 本就依赖 retriever），可诊断。
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS

    print("① 遍历钉钉组织树 …", flush=True)
    token = _access_token()
    depts = walk_tree(token)
    print(f"   共 {len(depts)} 个部门", flush=True)

    tree_names = set()
    for d in depts:
        kind, groups = classify(d["name"], _DEPT_NAME_TO_GROUPS, _PRODUCTION_WORKSHOP_DEPTS, _VALID_ACL_GROUPS)
        d["kind"], d["groups"], d["members"] = kind, groups, len(d["member_ids"])
        tree_names.add(d["name"])

    unmapped_with_users = sorted(
        (d for d in depts if d["kind"] == "UNMAPPED" and d["members"] > 0),
        key=lambda x: -x["members"])
    unmapped_empty = [d for d in depts if d["kind"] == "UNMAPPED" and d["members"] == 0]
    mapped = [d for d in depts if d["kind"] != "UNMAPPED"]

    # Part C：映射死键（现网树上已无此名）
    dead_explicit = sorted(k for k in _DEPT_NAME_TO_GROUPS if k not in tree_names)
    dead_umbrella = sorted(k for k in _PRODUCTION_WORKSHOP_DEPTS if k not in tree_names)

    # Part B：user_role 在册用户（仅聚合，零 PII）
    impacted_by_dept, impacted_total, total_active, db_err = ({}, 0, 0, "skipped(--no-db)") if args.no_db \
        else scan_user_role(_normalize_dept_to_codes)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scan.json").write_text(json.dumps({
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "dept_total": len(depts), "mapped": len(mapped),
        "unmapped_with_users": unmapped_with_users,
        "unmapped_empty": [d["path"] for d in unmapped_empty],
        "dead_explicit_keys": dead_explicit, "dead_umbrella_keys": dead_umbrella,
        "user_role_total_active": total_active,
        "user_role_public_only_total": impacted_total,
        "user_role_public_only_by_dept": impacted_by_dept, "user_role_error": db_err,
        "all_depts": [{k: d[k] for k in ("dept_id", "name", "path", "members", "kind", "groups")} for d in depts],
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    md = [f"# 部门 → ACL 映射覆盖扫描（{datetime.now():%Y-%m-%d %H:%M}）\n",
          f"- 全树部门 **{len(depts)}** 个：命中映射 {len(mapped)}（explicit "
          f"{sum(1 for d in depts if d['kind']=='explicit')} / umbrella "
          f"{sum(1 for d in depts if d['kind']=='umbrella')} / passthru "
          f"{sum(1 for d in depts if d['kind']=='passthru')}），未命中 "
          f"{len(unmapped_with_users) + len(unmapped_empty)}（有人 {len(unmapped_with_users)} / 空 {len(unmapped_empty)}）",
          f"- user_role 在册（is_active=1）**{total_active}** 人，其中归一化为空=仅 public "
          f"**{impacted_total}** 人" + (f"（Part B 未跑：{db_err}）" if db_err else ""),
          "\n## ❗ 未命中映射且有在册用户的部门（按直属人数降序）\n",
          "| 直属人数 | 部门 | 全路径 | dept_id |", "|---:|---|---|---|"]
    md += [f"| {d['members']} | {d['name']} | {d['path']} | {d['dept_id']} |" for d in unmapped_with_users]
    md += ["\n## user_role 仅 public 的在册用户 · 按缓存部门名聚合（零个人字段）\n",
           "| 人数 | 缓存的 dept_code（部门名 CSV） |", "|---:|---|"]
    md += [f"| {n} | {k} |" for k, n in sorted(impacted_by_dept.items(), key=lambda kv: -kv[1])]
    md += ["\n## 映射死键（树上已无此名，重设计时清理）\n",
           f"- explicit：{'、'.join(dead_explicit) or '无'}",
           f"- umbrella 快照：{'、'.join(dead_umbrella) or '无'}",
           f"\n## 未命中且当前无人的部门（{len(unmapped_empty)} 个，组织调整后可能进人）\n"]
    md += [f"- {p}" for p in sorted(d["path"] for d in unmapped_empty)]
    (out_dir / "report.md").write_text("\n".join(md), encoding="utf-8")

    print(f"② 未命中且有人的部门：{len(unmapped_with_users)} 个")
    for d in unmapped_with_users:
        print(f"   {d['members']:>4} 人  {d['path']}")
    print(f"③ user_role 仅 public 用户：{impacted_total}/{total_active}（按部门聚合，零个人字段）"
          + (f"（Part B: {db_err}）" if db_err else ""))
    print(f"→ 报告：{out_dir}/report.md + scan.json")


if __name__ == "__main__":
    main()
