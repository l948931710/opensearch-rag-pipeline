# -*- coding: utf-8 -*-
"""ha3_reconcile 单测 —— 聚焦纯函数 _classify_stale 的三道安全闸 + simulate no-op。"""
from opensearch_pipeline.ha3_reconcile import _classify_stale, reconcile_ha3_orphan_pks


def test_classify_dup_orphan_and_g3():
    """dup（替换已在 HA3）删；orphan chunk_id 删；dup（替换缺失，G3）跳过；active 保留。"""
    rds_active_ids = {100, 101, 102}
    rds_active_chunkid = {"A": 100, "B": 101, "C": 102}
    ha3_map = {
        100: ("A", "doc1"),   # kept (G1)
        50:  ("A", "doc1"),   # dup, 替换 100 在 HA3 → 删
        60:  ("X", "doc1"),   # orphan chunk_id（X 不在 active）→ 删
        52:  ("B", "doc1"),   # dup, 替换 101 不在 ha3_map → G3 跳过
        102: ("C", "doc2"),   # kept (G1)
    }
    delete, skipped = _classify_stale(ha3_map, rds_active_ids, rds_active_chunkid)
    assert delete == [50, 60]
    assert skipped["dup_replacement_absent"] == 1


def test_g1_never_deletes_active():
    """G1 硬不变量：active id 永不进删除集（即便它在 HA3 多处出现）。"""
    rds_active_ids = {1, 2, 3}
    rds_active_chunkid = {"a": 1, "b": 2, "c": 3}
    ha3_map = {1: ("a", "d"), 2: ("b", "d"), 3: ("c", "d")}
    delete, _ = _classify_stale(ha3_map, rds_active_ids, rds_active_chunkid)
    assert delete == []


def test_fully_retired_doc_all_stale():
    """文档完全退役（无 active chunk_id）→ 其全部 HA3 行皆删。"""
    delete, _ = _classify_stale(
        {500: ("Z", "deadDoc"), 501: ("Y", "deadDoc")}, set(), {})
    assert delete == [500, 501]


def test_g3_holds_back_until_repush():
    """同一 doc：旧载体的 chunk_id 新 id 缺失 → 全部 G3 跳过，0 删（push-then-purge 不变量）。"""
    rds_active_chunkid = {"A": 900, "B": 901}   # 新 id 900/901
    rds_active_ids = {900, 901}
    ha3_map = {10: ("A", "d"), 11: ("B", "d")}   # 只有旧载体，新 id 还没 push
    delete, skipped = _classify_stale(ha3_map, rds_active_ids, rds_active_chunkid)
    assert delete == []
    assert skipped["dup_replacement_absent"] == 2


def test_reconcile_simulate_is_noop():
    """simulate=True → 不连任何外部资源，返回零报告。"""
    rep = reconcile_ha3_orphan_pks(simulate=True)
    assert rep == {"checked": 0, "stale": 0, "deleted": 0, "skipped": {}, "errors": []}


# ── TOCTOU 二次确认：枚举窗口内并发 push 的新 chunk 不被误删 ──
def _make_conn(first_rows, fresh_rows, max_id):
    class _Cur:
        def __init__(self):
            self._last = ""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self._last = sql

        def fetchall(self):
            # 按 SELECT 列形态路由（勿按 WHERE 子句空格路由——perf#90 后两条读都过滤 is_active）：
            # 二次确认读=两列 `id, chunk_id`；初始快照=三列 `id, chunk_id, is_active`。
            if "SELECT id, chunk_id FROM chunk_meta" in self._last:   # 删除前的【最新】重读
                return fresh_rows
            if "FROM chunk_meta" in self._last:     # 枚举前的初始快照
                return first_rows
            return []

        def fetchone(self):
            if "MAX(id)" in self._last:
                return (max_id,)
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    return _Conn()


class _FakeHa3Client:
    def __init__(self):
        self.deleted = []

    def push_documents(self, table, pk_field, req):
        for d in req.body:
            self.deleted.append(d["fields"][pk_field])

        class _R:
            status_code = 200
            body = ""
            text = ""
        return _R()


def _patch_reconcile_deps(monkeypatch, conn, client, ha3_map):
    import opensearch_pipeline.pipeline_nodes as pn
    import opensearch_pipeline.ha3_reconcile as hr
    import opensearch_pipeline.env_guard as eg
    monkeypatch.setattr(pn, "_get_db_conn", lambda *a, **k: conn)
    monkeypatch.setattr(pn, "_get_opensearch_client", lambda *a, **k: client)
    monkeypatch.setattr(hr, "_enumerate_ha3_pks", lambda *a, **k: ha3_map)
    monkeypatch.setattr(eg, "assert_destructive_write_allowed", lambda *a, **k: None)


def test_reconcile_does_not_delete_chunk_born_during_scan(monkeypatch):
    """竞态：枚举前快照只见 id=1；枚举期间 Stage-3 推入 id=2（已进 HA3+commit chunk_meta）。
    初始快照会把 id=2 误判 orphan；删除前的最新重读使其被 G1 救回 → 绝不删在线 chunk。"""
    first_rows = [(1, "c1", 1)]                     # 枚举前：仅 id 1 active
    fresh_rows = [(1, "c1"), (2, "c2")]             # 删除前重读：id 2 已 active
    ha3_map = {1: ("c1", "d1"), 2: ("c2", "d1")}    # 枚举到 id 2（窗口内新推）
    client = _FakeHa3Client()
    conn = _make_conn(first_rows, fresh_rows, max_id=1)
    _patch_reconcile_deps(monkeypatch, conn, client, ha3_map)

    rep = reconcile_ha3_orphan_pks(simulate=False, dry_run=False)
    assert client.deleted == []                     # id 2 绝不被删
    assert rep["deleted"] == 0
    assert rep["stale"] == 0
    assert rep["skipped"].get("born_during_scan") == 1


def test_reconcile_still_deletes_genuine_orphan(monkeypatch):
    """对照：真孤儿（初始与最新重读都不认账）仍被删除——二次确认不放过真过时行。"""
    first_rows = [(1, "c1", 1)]
    fresh_rows = [(1, "c1")]                         # 最新仍只 id 1 active
    ha3_map = {1: ("c1", "d1"), 9: ("cX", "deadDoc")}  # id 9 = 真孤儿
    client = _FakeHa3Client()
    conn = _make_conn(first_rows, fresh_rows, max_id=1)
    _patch_reconcile_deps(monkeypatch, conn, client, ha3_map)

    rep = reconcile_ha3_orphan_pks(simulate=False, dry_run=False)
    assert client.deleted == [9]
    assert rep["deleted"] == 1


# ── 2026-07-22：删除面 fail-closed + 默认 dry-run ──

def test_dry_run_defaults_to_true_no_arg_call_never_deletes(monkeypatch):
    """无参调用**绝不删除**。此前默认 dry_run=False，spot_checker 的无参调用会静默执行
    不可逆 HA3 删除；HA3 删不可逆 ⇒ 安全默认必须是只预览。"""
    import inspect
    assert inspect.signature(reconcile_ha3_orphan_pks).parameters["dry_run"].default is True

    client = _FakeHa3Client()
    conn = _make_conn([(1, "c1", 1)], [(1, "c1")], max_id=1)
    _patch_reconcile_deps(monkeypatch, conn, client, {1: ("c1", "d1"), 9: ("cX", "dead")})
    rep = reconcile_ha3_orphan_pks(simulate=False)      # 无参 → dry_run 默认
    assert client.deleted == [] and rep["deleted"] == 0
    assert rep["stale"] == 1                            # 仍报数，只是不删


def test_enumeration_unhealthy_forces_zero_delete(monkeypatch):
    """枚举协议不健康 → **零删除** + error 留痕：不可信的候选集 × 不可逆删除 = 绝不允许。"""
    import opensearch_pipeline.ha3_reconcile as hr
    client = _FakeHa3Client()
    conn = _make_conn([(1, "c1", 1)], [(1, "c1")], max_id=1)
    _patch_reconcile_deps(monkeypatch, conn, client, {})

    def _unhealthy(*a, **k):
        raise hr.Ha3EnumerationUnhealthy("bucket [0,500): coveredPercent=0.5")

    monkeypatch.setattr(hr, "_enumerate_ha3_pks", _unhealthy)
    rep = reconcile_ha3_orphan_pks(simulate=False, dry_run=False)   # 显式真删也不许删
    assert client.deleted == [] and rep["deleted"] == 0
    assert rep.get("enum_health") == "unhealthy"
    assert any("零删除" in e for e in rep["errors"])


def test_spot_checker_calls_orphan_reconcile_in_dry_run():
    """spot_checker 必须显式 dry_run=True——删除入口统一收敛到 orchestrator 的
    RAG_STAGE3_ORPHAN_PURGE gate，巡检本身只报告。"""
    import inspect
    from opensearch_pipeline import spot_checker
    src = inspect.getsource(spot_checker)
    assert "reconcile_ha3_orphan_pks(dry_run=True)" in src
    assert "reconcile_ha3_orphan_pks()" not in src, "无参调用会依赖默认值，必须显式表态"
