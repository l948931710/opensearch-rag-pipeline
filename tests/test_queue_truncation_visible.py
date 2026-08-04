# -*- coding: utf-8 -*-
"""硬 LIMIT 队列的截断必须**如实告知**，不得静默（P3-3，2026-08-04）。

## 治的是什么

六个列表端点用的是「SQL 硬 `LIMIT N`、响应里只有 `items`」的形态。队列长度超过 N 时，
使用者看到的「就这些」其实只是前 N 条，**且无从知道**：

| 端点 | N | 被截掉意味着 |
|---|---|---|
| `/api/kb/pending-approvals`   | 100 | 上传审批永远没人处理 |
| `/api/kb/access-requests`     | 100 | 跨部门授权申请永远没人处理 |
| `/api/kb/admin-node-candidates` | 200 | 节点管理员候选永远不被裁决 |
| `/api/kb/access-grants`       | 200 | 已授权存量看不全（撤销时漏项） |
| `/api/kb/my-access-requests`  | 100 | 申请人看不到自己更早的申请 |
| `/api/kb/gaps/dismissed`      | 100 | **撤销忽略的唯一入口**，看不到=撤不回 |

与 B8（差评复核两层截断）同族。做法一致：`LIMIT N+1` 取一行探针，多出来即
`truncated=True`，items 仍只给 N 条。**刻意不做真分页** —— 那需要稳定排序键
（`DATETIME` 只到秒，OFFSET 翻页会漏/重，见 `d2c8e12` 的实测）+ 前端翻页，属设计变更。

## 为什么测的是 Python 侧切片，而不是 SQL 的 LIMIT

桩游标不执行 SQL，`LIMIT` 对它无效 ⇒ 喂 N+1 行正好**只**驱动 Python 侧的
「切片 + 置位」逻辑，这正是本次改动的实质。SQL 里的 `N+1` 另用文本断言守住
（少了它，探针行根本取不回来，Python 侧再对也永远 truncated=False）。
"""
import pytest


def _skip_if_not_sim():
    import os
    if os.getenv("RAG_SIMULATE", "true").lower() not in ("true", "1", "yes"):
        pytest.skip("需 simulate 模式")


def _stub(monkeypatch, rows):
    """桩游标：fetchall 恒返回 rows（不实现 LIMIT——见模块 docstring）。"""
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): self.sql = sql
        def fetchall(self): return rows
        def fetchone(self): return None

    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())


# ── 各端点：(调用, 造行, 上限) ────────────────────────────────────────────────

def _call_pending(api, n):
    return api.kb_pending_approvals(request=None, identity=api.Identity(user_id="k"))


def _row_pending(i):
    return (f"D{i}", 1, f"t{i}", "f.docx", "hr", "public", "n", None)


def _call_node_candidates(api, n):
    from opensearch_pipeline.routes import kb_access
    return kb_access.kb_node_candidates_list(request=None, identity=api.Identity(user_id="k"))


def _row_candidate(i):
    return (i, f"u{i}", "n", 7, "研发", "r", 3, None)


def _call_gaps_dismissed(api, n):
    from opensearch_pipeline.routes import contribution
    return contribution.kb_gaps_dismissed(request=None, identity=api.Identity(user_id="k"))


def _row_dismissed(i):
    return (f"h{i}", "q", "r", "n", None)


def _call_access_requests(api, n):
    from opensearch_pipeline.routes import kb_access
    return kb_access.kb_access_requests_list(request=None, identity=api.Identity(user_id="k"))


def _row_access_request(i):
    return (i, f"D{i}", "t", "hr", "rd", "pending", "r", None, None, 1)


def _call_access_grants(api, n):
    from opensearch_pipeline.routes import kb_access
    return kb_access.kb_access_grants_list(request=None, identity=api.Identity(user_id="k"))


def _row_access_grant(i):
    return (i, f"D{i}", "t", "hr", "rd", "n", "dept_internal", "r", None)


CASES = [
    ("pending-approvals", _call_pending, _row_pending, 100),
    ("access-requests", _call_access_requests, _row_access_request, 100),
    ("access-grants", _call_access_grants, _row_access_grant, 200),
    ("admin-node-candidates", _call_node_candidates, _row_candidate, 200),
    ("gaps/dismissed", _call_gaps_dismissed, _row_dismissed, 100),
]

# ⚠️ `/api/kb/my-access-requests` **不在** CASES：它的同步态派生（E#40 批量预取 +
# 逐行回退）要桩住 chunk_meta / allowed_depts 多轮查询，桩成本远超所验内容。
# 它的两层改动分别由：SQL 侧 → 下面的 `test_sql_actually_fetches_the_probe_row`
# （kb_access 需 2 处 `LIMIT 101`，缺一即红）；Python 侧 → 无独立覆盖，**如实记在此处**。


@pytest.mark.parametrize("name,call,mkrow,cap", CASES, ids=[c[0] for c in CASES])
def test_over_cap_reports_truncated(monkeypatch, name, call, mkrow, cap):
    """★ 超上限：`truncated=True`，且 items 仍只给 cap 条（不把探针行泄给前端）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub(monkeypatch, [mkrow(i) for i in range(cap + 1)])
    from opensearch_pipeline import api
    r = call(api, cap + 1)
    assert r.truncated is True, f"{name}: 队列超上限却报 truncated=False —— 截断又静默了"
    assert len(r.items) == cap, f"{name}: 探针行泄进了 items（{len(r.items)} != {cap}）"


@pytest.mark.parametrize("name,call,mkrow,cap", CASES, ids=[c[0] for c in CASES])
def test_exactly_at_cap_is_not_truncated(monkeypatch, name, call, mkrow, cap):
    """反向：**恰好** cap 条不算截断。

    这条防的是「truncated 恒 True」式的假通过 —— 用 `>=` 而不是 `>` 判定就会在这里翻车。
    """
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub(monkeypatch, [mkrow(i) for i in range(cap)])
    from opensearch_pipeline import api
    r = call(api, cap)
    assert r.truncated is False, f"{name}: 恰好 {cap} 条被误报截断"
    assert len(r.items) == cap


def test_sql_actually_fetches_the_probe_row():
    """🔴 SQL 侧守卫：六处必须是 `LIMIT N+1`。

    少了探针行，Python 侧的判定再对也永远 `truncated=False` —— 一个**完全静默**的假绿。
    桩游标不执行 SQL，这一层只能靠文本断言。
    """
    import pathlib
    import re
    # 按**出现次数**断言，不是"存在即可" —— kb_access 里 101 / 201 各有两处
    # （101: access-requests + my-access-requests；201: access-grants + admin-node-candidates），
    # 只要"存在"的话退化掉其中一处另一处仍然满足，守卫就漏了。
    want = {
        "opensearch_pipeline/routes/kb_console.py": {101: 1},        # pending-approvals
        "opensearch_pipeline/routes/kb_access.py": {101: 2, 201: 2},
        "opensearch_pipeline/routes/contribution.py": {101: 1},      # gaps/dismissed
    }
    for f, expected in want.items():
        src = pathlib.Path(f).read_text(encoding="utf-8")
        found = [int(x) for x in re.findall(r"LIMIT (\d+)", src)]
        for n, times in expected.items():
            assert found.count(n) == times, (
                f"{f}: 期望 `LIMIT {n}`（N+1 探针行）出现 {times} 次，实得 {found.count(n)} 次")
