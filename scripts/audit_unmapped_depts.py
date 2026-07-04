# -*- coding: utf-8 -*-
"""扫全钉钉组织树，列出「未命中 ACL 映射且有在册用户」的部门（权限覆盖缺口审计）。

背景
────
用户所属钉钉部门名经 dingtalk_identity._normalize_dept_to_codes 归一化为 ACL 权限组代码。
归一化结果为空（[]）= fail-closed = 该用户只能看 public 内容。映射是一份精选白名单
（_DEPT_NAME_TO_GROUPS + 生产中心子树 _PRODUCTION_WORKSHOP_DEPTS），并非全组织覆盖，
组织树漂移（新增/改名叶子、其它中心的细分节点）会留下覆盖缺口。本脚本把缺口显式列出来。

本脚本【完全复用】线上的 _normalize_dept_to_codes，匹配逻辑与检索授权 100% 一致
（含生产中心子树伞组、_VALID_ACL_GROUPS 白名单、资材部反例）。

READ ONLY —— 只调钉钉 department/listsub、user/listid、（可选 user/get）与 RDS 只读 SELECT，
不写钉钉、不写库。

输出两份报告
────────────
[A] 部门视角（默认）：遍历全树，列出「名字未命中映射 且 直属成员数>0」的部门。
    这是「扫全树列出部门」的直接答案。注意：某部门未命中 ≠ 其成员一定 fail-closed，
    因为多部门用户会取各部门权限组的并集——只要该用户还挂在另一个已映射部门上就不受影响。

[B] 用户视角（加 --verify-users）：对 [A] 中风险部门的每个直属成员，拉取其完整
    dept_id_list，对【全部所属部门名的并集】再归一化一次；仍为空者才是【真正 fail-closed】
    的在册用户。这一步每个用户一次 user/get，仅对风险部门成员发起，开销可控。

[C] RDS 交叉核对（加 --check-rds）：扫 user_role 表（is_active=1）里已缓存的在册用户，
    对其 dept_code 归一化，列出结果为空的行——即「系统已见过、但当前 fail-closed」的用户。

用法
────
    python scripts/audit_unmapped_depts.py                     # 报告 A
    python scripts/audit_unmapped_depts.py --verify-users      # 报告 A + B
    python scripts/audit_unmapped_depts.py --check-rds         # 报告 A + C
    python scripts/audit_unmapped_depts.py --verify-users --check-rds --json out.json

需要 .env 里的钉钉应用凭证（DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET）；
--check-rds 另需 RDS 配置（RAG_RDS_* / RAG_RDS_DATABASE）。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# 复用线上归一化逻辑（与检索授权完全一致）
from opensearch_pipeline.dingtalk_identity import _normalize_dept_to_codes  # noqa: E402

ROOT_DEPT_ID = 1  # 钉钉根部门固定为 1


def _load_env() -> None:
    env = _REPO_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _access_token() -> str:
    cid = os.environ.get("DINGTALK_CLIENT_ID")
    sec = os.environ.get("DINGTALK_CLIENT_SECRET")
    if not cid or not sec:
        sys.exit("✗ 缺少 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET（请在 .env 配置）")
    resp = requests.post(
        "https://api.dingtalk.com/v1.0/oauth2/accessToken",
        json={"appKey": cid, "appSecret": sec}, timeout=10,
    )
    tok = resp.json().get("accessToken")
    if not tok:
        sys.exit(f"✗ 获取 access_token 失败: {resp.text[:300]}")
    return tok


def _post(token: str, path: str, body: dict, retries: int = 3) -> dict:
    """带轻量重试的钉钉 POST（限流 errcode 90018 / 网络抖动退避）。"""
    url = f"https://oapi.dingtalk.com/{path}?access_token={token}"
    for i in range(retries):
        try:
            data = requests.post(url, json=body, timeout=10).json()
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(2 ** i)
            continue
        if data.get("errcode") == 0:
            return data
        # 90018 = 请求过于频繁；退避重试
        if data.get("errcode") in (90018, 88, -1) and i < retries - 1:
            time.sleep(2 ** i)
            continue
        return data
    return {}


def _listsub(token: str, dept_id: int) -> list:
    return _post(token, "topapi/v2/department/listsub", {"dept_id": dept_id}).get("result", []) or []


def _list_member_ids(token: str, dept_id: int) -> list:
    """该部门【直属】成员 userid 列表（不递归子部门）。"""
    return _post(token, "topapi/user/listid", {"dept_id": dept_id}).get("result", {}).get("userid_list", []) or []


def _user_dept_names(token: str, userid: str) -> list:
    """用户所属【全部】部门名（遍历完整 dept_id_list）。用于 [B] 并集复核。"""
    res = _post(token, "topapi/v2/user/get", {"userid": userid}).get("result", {})
    names = []
    for did in res.get("dept_id_list", []) or []:
        nm = _post(token, "topapi/v2/department/get", {"dept_id": did}).get("result", {}).get("name", "")
        if nm:
            names.append(nm)
    return names


def walk_tree(token: str) -> list:
    """BFS 遍历全树，返回 [{id, name, parent_id}]（去重）。"""
    depts, seen, queue = [], set(), [ROOT_DEPT_ID]
    while queue:
        pid = queue.pop(0)
        for d in _listsub(token, pid):
            did = d.get("dept_id")
            if did is None or did in seen:
                continue
            seen.add(did)
            depts.append({"id": did, "name": (d.get("name") or "").strip(), "parent_id": pid})
            queue.append(did)
    return depts


def report_a(token: str, depts: list) -> list:
    """[A] 部门视角：未命中映射 且 有直属成员。"""
    rows = []
    for d in depts:
        name = d["name"]
        if not name:
            continue
        groups = _normalize_dept_to_codes(name)
        if groups:  # 命中映射，跳过
            continue
        member_ids = _list_member_ids(token, d["id"])
        if member_ids:
            rows.append({**d, "member_count": len(member_ids), "member_ids": member_ids})
    rows.sort(key=lambda r: (-r["member_count"], r["name"]))
    return rows


def report_b(token: str, risky_rows: list) -> dict:
    """[B] 用户视角：风险部门成员按【全部门并集】复核，仍为空者=真 fail-closed。"""
    checked, truly_closed = {}, []
    for r in risky_rows:
        for uid in r["member_ids"]:
            if uid in checked:
                continue
            names = _user_dept_names(token, uid)
            groups = _normalize_dept_to_codes(names)
            checked[uid] = groups
            if not groups:
                truly_closed.append({"userid": uid, "dept_names": names})
    return {"total_checked": len(checked), "truly_fail_closed": truly_closed}


def report_c() -> list:
    """[C] RDS 交叉核对：user_role 在册用户中，dept_code 归一化为空者。"""
    from opensearch_pipeline.db import _get_db_conn
    from opensearch_pipeline.config import get_config

    db = get_config().rds.database
    conn = _get_db_conn()
    out = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT user_id, user_name, dept_code, role FROM {db}.user_role WHERE is_active=1"
            )
            for uid, uname, dept_code, role in cur.fetchall():
                if _normalize_dept_to_codes(dept_code):
                    continue
                out.append({"user_id": uid, "user_name": uname or "",
                            "dept_code": dept_code or "", "role": role or "employee"})
    finally:
        conn.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="审计未命中 ACL 映射且有在册用户的钉钉部门")
    ap.add_argument("--verify-users", action="store_true", help="[B] 对风险部门成员按全部门并集复核真 fail-closed")
    ap.add_argument("--check-rds", action="store_true", help="[C] 交叉核对 RDS user_role 在册用户")
    ap.add_argument("--json", metavar="PATH", help="额外把完整结果写入 JSON 文件")
    args = ap.parse_args()

    _load_env()
    token = _access_token()

    print("→ 遍历钉钉组织全树 ...", file=sys.stderr)
    depts = walk_tree(token)
    print(f"  共 {len(depts)} 个部门节点", file=sys.stderr)

    rows = report_a(token, depts)
    result = {"total_depts": len(depts), "unmapped_with_members": rows}

    print("\n" + "=" * 72)
    print("[A] 未命中映射 且 有直属成员的部门（权限覆盖缺口）")
    print("=" * 72)
    if not rows:
        print("✓ 无缺口：所有有成员的部门都命中了 ACL 映射。")
    else:
        print(f"{'成员数':>6}  {'部门ID':>12}  部门名")
        print("-" * 72)
        for r in rows:
            print(f"{r['member_count']:>6}  {r['id']:>12}  {r['name']}")
        print("-" * 72)
        print(f"合计 {len(rows)} 个未命中部门，"
              f"涉及直属成员 {sum(r['member_count'] for r in rows)} 人次"
              f"（人次含多部门重复，真 fail-closed 数见 [B]）")

    if args.verify_users and rows:
        print("\n→ [B] 按用户全部门并集复核 ...", file=sys.stderr)
        b = report_b(token, rows)
        result["user_verification"] = b
        print("\n" + "=" * 72)
        print("[B] 真正 fail-closed 的在册用户（并集归一化后仍为空）")
        print("=" * 72)
        print(f"复核 {b['total_checked']} 名成员，其中真 fail-closed {len(b['truly_fail_closed'])} 名：")
        for u in b["truly_fail_closed"]:
            print(f"  userid={u['userid']}  所属部门={u['dept_names']}")

    if args.check_rds:
        print("\n→ [C] 交叉核对 RDS user_role ...", file=sys.stderr)
        try:
            c = report_c()
            result["rds_registered_fail_closed"] = c
            print("\n" + "=" * 72)
            print("[C] RDS user_role 在册但 fail-closed 的用户")
            print("=" * 72)
            if not c:
                print("✓ 无：所有在册用户 dept_code 都能归一化出权限组。")
            else:
                for u in c:
                    print(f"  {u['user_id']}  {u['user_name']}  "
                          f"role={u['role']}  dept_code={u['dept_code']!r}")
                print(f"合计 {len(c)} 名在册用户当前 fail-closed。")
        except Exception as e:
            print(f"✗ [C] RDS 核对失败（跳过）: {e}", file=sys.stderr)

    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果已写入 {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
