# -*- coding: utf-8 -*-
"""test_kb_gaps_refusal_param_order.py — #F-kb-gaps-param-order 回归

修复对象：opensearch_pipeline/routes/contribution.py :: kb_gaps 的 REFUSAL 子查询
占位符绑定顺序。该 SQL 文本中 %s 出现的顺序恒为：
    ① owner_dept IN (%s, %s ...)   —— mine_expr 里每个 dept 一个占位符（depts 非空时）
    ② INTERVAL %s DAY              —— 时间窗 win（整型天数）
    ③ LIMIT %s                     —— 候选行上限 cap
因此传给 cursor.execute 的 params 必须是 (*depts, win, cap)。

原缺陷：params 顺序错位（win 排在 depts 前），使 win 落进 owner_dept IN(...)、
部门名字符串落进 `INTERVAL %s DAY` —— MySQL 把非数字字符串按 0 截断，时间窗坍缩为
0 天，REFUSAL 缺口整段静默失真。

本沙箱无真实 MySQL，无法验证 MySQL 的绑定语义，故改为【捕获 execute 的 (sql, params)】
的假 cursor：按 SQL 文本里 %s 的出现位置反推每个占位符应绑定的实参，断言
    - owner_dept IN 的占位符 → 依次绑定 depts
    - INTERVAL 前那个 %s     → 绑定整型 win（而非部门字符串）
    - LIMIT 的 %s            → 绑定 cap
纯 Python 路径，全程 simulate，不触真实 DB/SDK。
"""
import pytest


def _skip_if_not_sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


# ── 捕获型桩 DB：只记录 execute 的 (sql, params)，按 SQL 关键字回放 fetch* ──────────
class _CaptureCur:
    def __init__(self, conn):
        self.conn = conn
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        # 关键：原样记录传入的 params（不做任何规整），供绑定顺序断言
        self.conn.calls.append((sql, params))
        self._last = sql
        return 1

    def fetchone(self):
        s = self._last
        # reconcile 的 EXISTS 短路：返回“无 registered 行”→ 走武装节流早退，避免多余 UPDATE
        if "SELECT EXISTS" in s:
            return (0,)
        # summary 三条 COUNT
        return (0,)

    def fetchall(self):
        s = self._last
        if "answer_status='REFUSAL'" in s:
            return self.conn.refusal_rows
        # NO_RESULT / 覆盖查询等：本用例只关心 REFUSAL 绑定，返回空即可
        return []


class _CaptureConn:
    def __init__(self, refusal_rows):
        self.refusal_rows = refusal_rows
        self.calls = []

    def cursor(self):
        return _CaptureCur(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _ident(groups):
    from opensearch_pipeline import api
    return api.Identity(user_id="u1", acl_groups=list(groups), name="张三")


def test_kb_gaps_refusal_execute_binds_params_in_sql_order(monkeypatch):
    """#F-kb-gaps-param-order：REFUSAL execute 的 params 序列须与 SQL 中 %s 出现顺序一致。"""
    _skip_if_not_sim()
    from opensearch_pipeline import api, kb_authz
    from opensearch_pipeline.routes import contribution as ctr

    # 用两个部门 → owner_dept IN 有 2 个占位符，使“win 错插进 IN”与“dept 错插进 INTERVAL”
    # 两种错位都可被下面的位置断言分辨（单部门也能测，双部门更决定性）。
    groups = ("marketing", "finance")
    depts = kb_authz.sanitize_owner_depts(list(groups))
    assert len(depts) == 2, f"前置：sanitize 后应为 2 个部门，实得 {depts}"
    # ε-4 解耦：kb_gaps 走缺口专属常量（365/2000）；_CONTRIB_* 归 asks（30/400），勿在此读
    win = ctr._GAP_WINDOW_DAYS           # 365（整型时间窗）
    cap = ctr._GAP_CANDIDATE_CAP         # 2000

    # REFUSAL 至少一行，确保聚合路径正常（值本身不参与本断言）
    refusal_rows = [("如何申请密钥", "m1", 3, depts[0])]
    conn = _CaptureConn(refusal_rows)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)

    # 清空 TTL 缓存 + reconcile 节流，确保本次请求真正执行 REFUSAL 查询而非命中缓存
    ctr._gaps_cache.clear()
    with ctr._reconcile_lock:
        ctr._reconcile_state["ts"] = 0.0

    api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups))

    # 定位 REFUSAL 那条 execute（唯一含 answer_status='REFUSAL'）
    refusal_calls = [(s, p) for (s, p) in conn.calls if "answer_status='REFUSAL'" in s]
    assert len(refusal_calls) == 1, f"应恰有一条 REFUSAL execute，实得 {len(refusal_calls)}"
    sql, params = refusal_calls[0]
    params = list(params)

    # 占位符数量必须与实参个数一致（防止漏绑/多绑）
    n_ph = sql.count("%s")
    assert n_ph == len(params) == len(depts) + 2, (
        f"占位符/实参数目不符：%s={n_ph}, params={len(params)}, 期望={len(depts) + 2}")

    # 按 %s 在 SQL 文本中的位置反推每个占位符的角色
    segments = sql.split("%s")  # 长度 = n_ph + 1；segments[i] 是第 i 个 %s 之前的文本
    idx_interval = next(i for i in range(n_ph) if segments[i].rstrip().endswith("INTERVAL"))
    idx_limit = next(i for i in range(n_ph) if segments[i].rstrip().endswith("LIMIT"))

    # 结构性前提：owner_dept IN 的占位符在最前，随后 INTERVAL，最后 LIMIT
    assert idx_interval == len(depts), (
        f"INTERVAL 的 %s 应位于第 {len(depts)} 个（owner_dept IN 之后），实得 {idx_interval}")
    assert idx_limit == idx_interval + 1

    # 核心断言 1：INTERVAL 前的 %s 绑定的是整型 win，而【不是】部门字符串
    interval_val = params[idx_interval]
    assert isinstance(interval_val, int) and not isinstance(interval_val, bool), (
        f"INTERVAL %s 绑定应为整型时间窗，实得 {interval_val!r}（旧代码把部门名绑到了这里）")
    assert interval_val == win, f"INTERVAL %s 应绑定 win={win}，实得 {interval_val!r}"

    # 核心断言 2：owner_dept IN 的占位符依次绑定 depts，而【不是】win
    assert tuple(params[:idx_interval]) == tuple(depts), (
        f"owner_dept IN 的占位符应依次绑定 depts={depts}，实得 {params[:idx_interval]!r}"
        f"（旧代码把 win={win} 绑到了这里）")
    assert win not in params[:idx_interval], "win 不得落入 owner_dept IN(...) 的占位符"

    # 核心断言 3：LIMIT 绑定 cap
    assert params[idx_limit] == cap, f"LIMIT %s 应绑定 cap={cap}，实得 {params[idx_limit]!r}"

    # 收口：整段实参顺序恒等于 (*depts, win, cap)
    assert tuple(params) == tuple(depts) + (win, cap), (
        f"REFUSAL params 顺序错位：期望 {tuple(depts) + (win, cap)!r}，实得 {tuple(params)!r}")


def test_kb_gaps_no_result_binds_gap_window_and_cap(monkeypatch):
    """ε-4 补锚：NO_RESULT execute 的 (win, cap) 此前无专门测试锁定——断言绑定缺口专属
    常量 (365, 2000)，且绝不是 asks 的 (30, 400)（常量解耦的另一半防线）。"""
    _skip_if_not_sim()
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import contribution as ctr

    conn = _CaptureConn(refusal_rows=[])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    ctr._gaps_cache.clear()
    with ctr._reconcile_lock:
        ctr._reconcile_state["ts"] = 0.0

    api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(("marketing",)))

    nr_calls = [(s, p) for (s, p) in conn.calls if "answer_status='NO_RESULT'" in s]
    assert len(nr_calls) == 1
    _sql, params = nr_calls[0]
    params = list(params)
    assert params[0] == ctr._GAP_WINDOW_DAYS == 365
    assert params[-1] == ctr._GAP_CANDIDATE_CAP == 2000
    assert (params[0], params[-1]) != (ctr._CONTRIB_WINDOW_DAYS, ctr._CONTRIB_CANDIDATE_CAP)
