# -*- coding: utf-8 -*-
"""Regenerate the _PRODUCTION_WORKSHOP_DEPTS allow-list from the live DingTalk org.

dingtalk_identity._PRODUCTION_WORKSHOP_DEPTS is an explicit snapshot of every dept
name in the 生产中心 (production center) subtree — every production-subline user must
normalize to the 'production' umbrella ACL group (risk ④). The org tree drifts as
workshops are renamed / added, so re-run this after org changes and paste the printed
frozenset back into opensearch_pipeline/dingtalk_identity.py.

READ ONLY — only DingTalk department listsub calls; no writes.

Requires DingTalk app creds in .env (DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET).
Usage:  python scripts/gen_production_dept_names.py
"""
import os
from pathlib import Path

import requests

# 生产中心 root dept_id + the one structural exception that keeps its own mapping
# (资材部 sits under 生产中心 but is [supply, pmc] per the 权限单 — never 'production').
PROD_CENTER_ID = 599318766
EXCLUDE = {"资材部"}

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    for line in (_REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
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
    return resp.json()["accessToken"]


def _listsub(token: str, dept_id: int) -> list:
    resp = requests.post(
        f"https://oapi.dingtalk.com/topapi/v2/department/listsub?access_token={token}",
        json={"dept_id": dept_id}, timeout=10,
    ).json()
    return resp.get("result", []) if resp.get("errcode") == 0 else []


def _walk(token: str, dept_id: int, names: set) -> None:
    for d in _listsub(token, dept_id):
        nm = (d.get("name") or "").strip()
        if nm and nm not in EXCLUDE:
            names.add(nm)
        _walk(token, d["dept_id"], names)


def main() -> None:
    _load_env()
    token = _access_token()
    names: set = set()
    _walk(token, PROD_CENTER_ID, names)
    print(f"# 生产中心 (dept_id {PROD_CENTER_ID}) subtree; count={len(names)}; "
          f"excluded={sorted(EXCLUDE)}")
    print("_PRODUCTION_WORKSHOP_DEPTS = frozenset({")
    for nm in sorted(names):
        print(f'    "{nm}",')
    print("})")


if __name__ == "__main__":
    main()
