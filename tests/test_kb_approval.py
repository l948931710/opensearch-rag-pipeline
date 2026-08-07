# -*- coding: utf-8 -*-
"""test_kb_approval.py — /api/kb/approve 与 /api/kb/reject 的授权与状态行为（全程 sim）。

评审 P2-14「封住权限提升零测试缺口」：这两个端点此前**零路由测试**——仓内仅有的
`kb_approve` 字样在 test_destructive_guard.py 里只是把它当作守卫的 op 名字符串，
与端点本身无关。审批放行意味着把 PENDING_APPROVAL 版本推进入库队列（下一批就进检索），
是纯粹的提权动作，其「仅 kb_admin」这道闸门此前没有任何测试看守。

同时钉住 F-37 纵深防御：文档已退役时不得放行任何 pending 版本——kb_retire 只把 current
版本置 retired，更早的 pending 版本可能仍 status=active，放行后会被 stage-1 认领**复活**。
"""
import pytest


def _skip_if_not_sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((" ".join(sql.split()), params))
        self._last = sql
        return self.conn.rowcount

    def fetchone(self):
        if "document_meta" in self._last and "FOR UPDATE" in self._last:
            return self.conn.meta_row
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, meta_row=("active",), rowcount=2):
        self.meta_row = meta_row
        self.rowcount = rowcount
        self.calls = []
        self.committed = False

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def audit(monkeypatch):
    """审计是旁路：捕获而非落库（write_audit 在端点内惰性 import）。"""
    seen = []
    monkeypatch.setattr("opensearch_pipeline.audit_log.write_audit",
                        lambda **kw: seen.append(kw))
    return seen


def _install(monkeypatch, conn):
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    return conn


# C8 §4.1（Sam 2026-08-04）：审批/驳回**强制单版本**，故辅助函数默认给具体版本；
# 「省略 version_no」现在是 400，由 test_approve_requires_explicit_version 专门验。
def _approve(doc_id="DOC_X", version_no=3, user_id="u1"):
    from opensearch_pipeline import api
    return api.kb_approve(api.KbApprovalRequest(doc_id=doc_id, version_no=version_no),
                          request=None, identity=api.Identity(user_id=user_id))


def _reject(doc_id="DOC_X", version_no=3, reason=None, user_id="u1"):
    from opensearch_pipeline import api
    return api.kb_reject(api.KbApprovalRequest(doc_id=doc_id, version_no=version_no,
                                               reason=reason),
                         request=None, identity=api.Identity(user_id=user_id))


def _status(ei):
    return getattr(ei.value, "status_code", None)


def _updates(conn):
    return [(s, p) for s, p in conn.calls if s.startswith("UPDATE")]


# ── 提权闸门：仅 kb_admin（此前零覆盖）────────────────────────────────────────

@pytest.mark.parametrize("role", ["employee", "dept_admin"])
@pytest.mark.parametrize("fn", ["approve", "reject"])
def test_non_kb_admin_cannot_approve_or_reject(monkeypatch, audit, role, fn):
    """dept_admin 也不行——审批放行影响的是全库入库队列，不是本部门内部事务。

    ⚠️ 关键在于：**403 必须发生在任何 DB 写之前**。只断言状态码不够——若哪天有人
    把角色判定挪到 UPDATE 之后，状态码仍是 403 而版本已经被放行了。
    """
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", role)
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    conn = _install(monkeypatch, _Conn())
    with pytest.raises(Exception) as ei:
        (_approve if fn == "approve" else _reject)()
    assert _status(ei) == 403
    assert conn.calls == [], f"403 之前不得触碰 DB，实际执行了 {conn.calls}"
    assert not conn.committed
    assert audit == [], "被拒的请求不该写审计"


@pytest.mark.parametrize("fn", ["approve", "reject"])
def test_kb_admin_missing_doc_id_is_400_not_a_blind_update(monkeypatch, audit, fn):
    """缺 doc_id → 400，且不得下发任何 UPDATE（谓词只剩状态列 = 全表放行）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn())
    with pytest.raises(Exception) as ei:
        (_approve if fn == "approve" else _reject)(doc_id="")
    assert _status(ei) == 400
    assert conn.calls == []


# ── approve 行为 ────────────────────────────────────────────────────────────

def test_approve_only_touches_pending_versions(monkeypatch, audit):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(rowcount=2))
    out = _approve()
    assert out == {"status": "ok", "approved": 2}
    ups = _updates(conn)
    assert len(ups) == 1
    sql, params = ups[0]
    assert "content_process_status='PENDING_APPROVAL'" in sql, (
        "必须只放行待审版本——丢了这个谓词会把 FAILED/REJECTED 版本一并复活")
    assert "SET content_process_status='NOT_STARTED'" in sql
    assert "approval_status='APPROVED'" in sql
    # C8 §4.1：审批恒带版本谓词（`_approve()` 默认 version_no=3）——
    # 改前省略即放行该文档全部 pending 版本，参数只有 ("DOC_X",)。
    assert params == ("DOC_X", 3)
    assert conn.committed
    assert audit and audit[0]["action_type"] == "APPROVE"


def test_approve_locks_document_meta_before_deciding(monkeypatch, audit):
    """F-37 与 kb_retire 的串行化靠 document_meta FOR UPDATE——且必须**先于** UPDATE。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn())
    _approve()
    kinds = ["lock" if ("document_meta" in s and "FOR UPDATE" in s)
             else "update" if s.startswith("UPDATE") else "other" for s, _ in conn.calls]
    assert "lock" in kinds and "update" in kinds
    assert kinds.index("lock") < kinds.index("update")


def test_approve_refuses_to_revive_a_retired_doc(monkeypatch, audit):
    """F-37：文档已退役 ⇒ 一个 pending 版本都不放行（否则被 stage-1 认领复活）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(meta_row=("retired",)))
    out = _approve()
    assert out["approved"] == 0 and "退役" in out.get("note", "")
    assert _updates(conn) == [], "退役文档下不得下发任何放行 UPDATE"


def test_approve_version_scoped_vs_all_pending(monkeypatch, audit):
    """★ C8 §4.1：审批/驳回**必须**精确到单个版本；省略 version_no 一律 400。

    改前：`vfilter = "AND version_no=%s" if req.version_no else ""` ⇒ 省略即放行该文档
    **全部** pending 版本。前端一直传具体版本（useKb.ts:1002），但**API 安全边界不能
    依赖前端** —— 直连 API 省掉该字段就能一次放行多版。

    与内容绑定的关系：绑定把「审批放行的字节」钉在**某一个版本**上；若审批能一次覆盖多版，
    绑定语义当场残缺（批的是哪一版的内容？）。所以这条是内容绑定的**前提**。

    ⚠️ 本用例此前断言的是旧行为（"不带 → 放行全部"）—— 那是**把漏洞写成了规范**。
    """
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(rowcount=1))
    # 带版本 → 谓词收窄到该版本
    _approve(version_no=3)
    sql, params = _updates(conn)[0]
    assert "AND version_no=%s" in sql and params == ("DOC_X", 3)

    # 不带版本 → 400，且**在任何 DB 写之前**
    for fn in (_approve, _reject):
        conn2 = _install(monkeypatch, _Conn(rowcount=1))
        with pytest.raises(Exception) as ei:
            fn(version_no=None)
        assert _status(ei) == 400
        assert "version_no" in str(getattr(ei.value, "detail", ""))
        assert _updates(conn2) == [], "省略 version_no 时不得下发任何 UPDATE"


# ── reject 行为 ─────────────────────────────────────────────────────────────

def test_reject_marks_rejected_and_truncates_reason(monkeypatch, audit):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(rowcount=1))
    out = _reject(reason="x" * 900)
    assert out == {"status": "ok", "rejected": 1}
    sql, params = _updates(conn)[0]
    assert "content_process_status='PENDING_APPROVAL'" in sql
    assert "SET content_process_status='REJECTED'" in sql and "approval_status='REJECTED'" in sql
    assert len(params[0]) == 500, "content_process_error 列为 VARCHAR(500)，必须截断"
    assert audit and audit[0]["action_type"] == "REJECT"


def test_reject_defaults_reason_when_absent(monkeypatch, audit):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn())
    _reject(reason=None)
    assert _updates(conn)[0][1][0] == "rejected"




# ── P2-11：review-tasks 分页（此前只回 items，limit=20 静默截断）──────────────

def _review_tasks(monkeypatch, n_rows, *, limit=20, offset=0, include_closed=False):
    """桩：SELECT review_task 回 n_rows 行（端点多取一条判 has_more）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline.routes import kb_console

    seen = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def execute(self, sql, params=None):
            seen["sql"] = " ".join(sql.split())
            seen["params"] = params

        def fetchall(self):
            return [(f"T{i}", "D1", "标题", 1, "spot_check_mismatch", "r", "hr",
                     "restricted", "PENDING", "", "2026-08-03", 3) for i in range(n_rows)]

        def fetchone(self): return None

    class _Cn:
        def cursor(self): return _C()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _Cn())
    monkeypatch.setattr(kb_console, "_dashboard_cache_get", lambda k: None)
    monkeypatch.setattr(kb_console, "_dashboard_cache_put", lambda k, v: None)
    from opensearch_pipeline import api
    out = api.kb_review_tasks(request=None, limit=limit, offset=offset,
                              include_closed=include_closed,
                              identity=api.Identity(user_id="kb1"))
    return out, seen


def test_review_tasks_reports_has_more_and_trims_the_probe_row(monkeypatch):
    """多取一条判 has_more，但**不得**把那条渲染出去（否则每页多一条、翻页错位）。"""
    out, seen = _review_tasks(monkeypatch, 21, limit=20)
    assert out.has_more is True
    assert len(out.items) == 20, "探测行必须被裁掉"
    assert "LIMIT %s OFFSET %s" in seen["sql"]
    assert seen["params"] == (21, 0), "取 limit+1 条"


def test_review_tasks_last_page_has_no_more(monkeypatch):
    out, _ = _review_tasks(monkeypatch, 20, limit=20)
    assert out.has_more is False and len(out.items) == 20


def test_review_tasks_order_by_has_unique_tiebreaker(monkeypatch):
    """新增 OFFSET 分页不得重演 d2c8e12：created_at 是秒精度，必须带 task_id。"""
    _, seen_open = _review_tasks(monkeypatch, 1, include_closed=False)
    _, seen_closed = _review_tasks(monkeypatch, 1, include_closed=True)
    from tests.test_pagination_stability import _parse_term, _split_top_level
    for tag, seen in (("open", seen_open), ("closed", seen_closed)):
        order = seen["sql"].split("ORDER BY")[1].split("LIMIT")[0]
        assert "t.task_id" in order, f"{tag} 分支 ORDER BY 缺唯一 tiebreaker：{order}"
        # ★ 2026-08-06（codex 补评审）：不只要"出现"，还必须**位于末位**。
        # 唯一项后面还挂东西时（如 `…, t.task_id ASC, 0 ASC`），只比较"最后两项方向"的
        # 守卫会被整体绕过；要求收尾即堵死这类构造。
        terms = [_parse_term(t) for t in _split_top_level(order)]
        # `_parse_term` 保留 alias（`m.doc_id` 与 `x.doc_id` 必须可区分），故比 "t.task_id"
        assert terms and terms[-1] and terms[-1][0] == "t.task_id", (
            f"{tag} 分支的唯一 tiebreaker 不在 ORDER BY 末位：{order}")


def test_review_tasks_cache_key_includes_offset(monkeypatch):
    """offset 必须进 cache key —— 漏了它第 2 页会命中第 1 页缓存（加载更多变成重复追加）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline.routes import kb_console
    keys = []
    monkeypatch.setattr(kb_console, "_dashboard_cache_get", lambda k: keys.append(k) or None)

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): pass
        def fetchall(self): return []
        def fetchone(self): return None

    class _Cn:
        def cursor(self): return _C()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _Cn())
    monkeypatch.setattr(kb_console, "_dashboard_cache_put", lambda k, v: None)
    from opensearch_pipeline import api
    api.kb_review_tasks(request=None, offset=0, identity=api.Identity(user_id="kb1"))
    api.kb_review_tasks(request=None, offset=20, identity=api.Identity(user_id="kb1"))
    assert keys[0] != keys[1], f"两页 cache key 相同 ⇒ 第 2 页会吃第 1 页缓存：{keys}"


def _review_tasks_raising(monkeypatch, offset):
    """桩：cursor.execute 抛异常（现网形态：1146 review_task 表缺失）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline.routes import kb_console

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            raise RuntimeError("1146 Table 'fuling_knowledge.review_task' doesn't exist")
        def fetchall(self): return []
        def fetchone(self): return None

    class _Cn:
        def cursor(self): return _C()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _Cn())
    monkeypatch.setattr(kb_console, "_dashboard_cache_get", lambda k: None)
    monkeypatch.setattr(kb_console, "_dashboard_cache_put", lambda k, v: None)
    from opensearch_pipeline import api
    return api.kb_review_tasks(request=None, limit=20, offset=offset,
                               include_closed=False, identity=api.Identity(user_id="kb1"))


@pytest.mark.parametrize("offset", [0, 20])
def test_review_tasks_failopen_marks_itself_degraded(monkeypatch, offset):
    """🔴 B7 补评审（2026-08-06，codex BLOCKER）：fail-open 早于 has_more 存在，于是
    查询失败时端点会**顺带断言「没有更多」**——前端据此撤掉「加载更多」、把截断的列表
    当成全部，还因为是 200 连错误横幅都不出（实测：offset=0 与 20 都返回 items=0/has_more=False）。

    契约：仍 fail-open（2026-07-15 现网：漏建 review_task 表把 kb_admin 管理台整页打 500），
    但必须自陈 `degraded=True`，让消费方把 items/has_more 整体判为非业务数据。

    ⚠️ 这条与前端那条（`review-task-degraded.spec.ts`）**互不可伪造**：这条钉后端在真异常下
    确实发 degraded，那条钉前端拿到 degraded 后确实不当真。少任一条都能造出假绿。
    """
    out = _review_tasks_raising(monkeypatch, offset)
    assert out.items == [] and out.has_more is False
    assert out.degraded is True, "fail-open 必须自陈降级，否则空响应=「没有更多」的断言"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_review_tasks_is_the_only_paginated_dashboard_cache_user():
    """🔴 B7 复核（2026-08-04）：`_dashboard_cache` 只准有一个带 offset 的使用者。

    缓存 × OFFSET 分页会把「他人并发处置导致下一页漏行」的窗口从网络往返放大到
    ≤TTL（默认 60s）。今天只有 `/api/kb/review-tasks` 处在这个组合里，且其后果已在
    调用点写明并有前端契约兜底（自己处置时本地与服务端前缀同步收缩，真库实测 0 漏）。

    这条守卫防的是**再加第二个** —— 那时就得逐个重新论证，而不是默认沿用本端点的结论。
    """
    import pathlib
    import re
    src = pathlib.Path("opensearch_pipeline/routes/kb_console.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    marks = [(i, m.group(2)) for i, ln in enumerate(lines)
             if (m := re.search(r'@router\.(get|post)\("([^"]+)"', ln))]
    paginated = []
    for i, ln in enumerate(lines):
        if "_dashboard_cache_get(" not in ln or "def " in ln:
            continue
        before = [p for p in marks if p[0] < i]
        if not before:
            continue
        ep = max(before, key=lambda x: x[0])
        if "offset" in "\n".join(lines[ep[0]:i]):
            paginated.append(ep[1])
    assert paginated == ["/api/kb/review-tasks"], (
        f"带 offset 的 _dashboard_cache 使用者变成了 {paginated} —— "
        "新加的那个必须单独论证「缓存 TTL 内翻页看到跨时刻快照」的后果，不能沿用 review-tasks 的结论")


# ── 批 B（2026-08-06 补评审）：审批状态机的三条谓词/竞态缺陷 ────────────────────

def test_reject_requires_pending_approval_precondition_in_sql(monkeypatch, audit):
    """B2：驳回必须同时要求 CPS 与 **approval_status='PENDING'** 前态。

    退役**有意不改** `content_process_status`（kb_console.py:3419-3421），所以退役后
    该版本仍满足 CPS 谓词 ⇒ 过期页面的 reject 能把 WITHDRAWN 改写成 REJECTED，
    而 kb_restore 的 `WITHDRAWN→PENDING` 还原从此永远匹配不上。
    """
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(rowcount=1))
    _reject()
    upd = [s for s, _ in _updates(conn) if "document_version" in s]
    assert upd, "驳回没有发出 UPDATE"
    sql = upd[0]
    assert "content_process_status='PENDING_APPROVAL'" in sql
    assert "approval_status='PENDING'" in sql, f"缺前态谓词 ⇒ 可覆盖 WITHDRAWN：{sql}"


def test_reject_zero_rows_is_409_not_200_and_writes_no_audit(monkeypatch, audit):
    """B2：0 行 = 竞态输了，必须让调用方看见。

    此前返回 200 + `rejected:0`，**三个端**（Vue / 小程序 / legacy）都把它当成功
    —— 与 approve 侧 2026-08-06 现网 20 个僵尸条目同一形态。
    ⚠️ 同时钉两件事：
      · **不是 500** —— 409 若写在 `try` 内会被 `except Exception` 吞掉改写成 500；
      · **不写 REJECT 审计** —— 此前 write_audit 无条件执行，等于把没发生的动作写进审计。
    """
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _install(monkeypatch, _Conn(rowcount=0))
    # 用宽 Exception + _status（同文件既有范式）：这样「抛了别的异常」也会因 status 为 None 被抓到
    with pytest.raises(Exception) as ei:
        _reject()
    assert _status(ei) == 409, f"0 行驳回必须 409（500 = 被 except Exception 吞了）：{ei.value!r}"
    assert not [a for a in audit if a.get("action_type") == "REJECT"], \
        "0 行驳回不得留下 REJECT 审计——那是记录了一个没发生的动作"


def test_reject_success_still_200_and_audited(monkeypatch, audit):
    """反向锚：正常驳回必须仍是 200 + rejected 计数 + 审计（别把 409 改成无差别拒绝）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _install(monkeypatch, _Conn(rowcount=1))
    out = _reject()
    assert out == {"status": "ok", "rejected": 1}
    assert [a for a in audit if a.get("action_type") == "REJECT"], "成功驳回必须留审计"


def test_retire_withdraw_is_scoped_to_pending_approval_versions(monkeypatch, audit):
    """B1：退役撤销审批时必须带 CPS 谓词，否则与 kb_restore 的还原不配平。

    `approval_status` 的 DDL 默认就是 'PENDING'（schema/001:124），管线两条写入路径都不
    显式设置 ⇒ **普通版本天然是 PENDING + 非 PENDING_APPROVAL**。不带谓词就会把它们一起
    打成 WITHDRAWN，而恢复要求 `content_process_status='PENDING_APPROVAL'` ⇒ 永久僵尸。
    """
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    # meta_row 要够 kb_retire 解包（owner_dept, permission_level, status, current_version_no）
    conn = _install(monkeypatch, _Conn(meta_row=("hr", "dept_internal", "active", 1), rowcount=1))
    from opensearch_pipeline import api
    try:
        api.kb_retire(api.KbRetireRequest(doc_id="DOC_X"), request=None,
                      identity=api.Identity(user_id="u1"))
    except Exception:   # noqa: BLE001
        pass          # 退役链路后段可能因桩不全而中断；本测试只看那条 UPDATE 的谓词
    wd = [s for s, _ in _updates(conn) if "approval_status='WITHDRAWN'" in s]
    assert wd, "退役没有发出审批撤销 UPDATE"
    assert "content_process_status='PENDING_APPROVAL'" in wd[0], \
        f"撤销面未收窄 ⇒ 普通版本被误伤且永远恢复不了：{wd[0]}"
