# -*- coding: utf-8 -*-
"""test_access_grants.py — Phase D allowed_depts 聚合 helper（单一注入点）+ to_ha3_doc 推送门控。

helper 只消费调用方游标（不建池），故无需 DB：桩游标返回 (doc_id, requester_depts) 行。
"""


class _Cur:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self._rows


def test_resolve_allowed_depts_aggregates_and_whitelists():
    from opensearch_pipeline import access_grants
    rows = [
        ("D1", "marketing"),
        ("D1", "production,quality"),   # 多 approved 行 → 并集
        ("D2", "hr"),
        ("D3", "typo_dept"),            # 非白名单 → fail-closed 丢弃 → D3 不出现
    ]
    cur = _Cur(rows)
    out = access_grants.resolve_allowed_depts(["D1", "D2", "D3"], cur)
    assert out["D1"] == ["marketing", "production", "quality"]   # 并集 + 去重 + 稳定排序
    assert out["D2"] == ["hr"]
    assert "D3" not in out                                        # 全非白名单 → 无授权
    assert "status='approved'" in cur.sql and "IN (" in cur.sql   # 只查 approved + 参数化 IN
    assert tuple(cur.params) == ("D1", "D2", "D3")


def test_resolve_allowed_depts_dedup_and_stable_sort():
    from opensearch_pipeline import access_grants
    cur = _Cur([("D1", "quality,marketing"), ("D1", "marketing")])
    assert access_grants.resolve_allowed_depts(["D1"], cur)["D1"] == ["marketing", "quality"]


def test_resolve_allowed_depts_empty_inputs():
    from opensearch_pipeline import access_grants
    assert access_grants.resolve_allowed_depts([], _Cur([])) == {}
    assert access_grants.resolve_allowed_depts_one("DX", _Cur([])) == []


def test_resolve_allowed_depts_one():
    from opensearch_pipeline import access_grants
    assert access_grants.resolve_allowed_depts_one("DX", _Cur([("DX", "finance")])) == ["finance"]


def test_resolve_allowed_depts_partial_bad_codes_kept_clean():
    """一文档里混白名单 + 非白名单：保留白名单部分，丢弃坏码（不整条作废）。"""
    from opensearch_pipeline import access_grants
    cur = _Cur([("D1", "marketing,typo_dept,hr")])
    assert access_grants.resolve_allowed_depts(["D1"], cur)["D1"] == ["hr", "marketing"]


# ── gate_by_permission：纵深守卫，只有 dept_internal 文档物化 allowed_depts（审计 Step 4 backstop a）──
def test_gate_by_permission_keeps_only_dept_internal():
    from opensearch_pipeline import access_grants
    allowed = {"D1": ["finance"], "D2": ["hr"], "D3": ["quality"], "D4": ["marketing"]}
    perm = {"D1": "dept_internal", "D2": "restricted", "D3": "public", "D4": None}
    out = access_grants.gate_by_permission(allowed, perm)
    assert out == {"D1": ["finance"]}                       # 仅 dept_internal 保留
    assert "D2" not in out and "D3" not in out and "D4" not in out   # restricted/public/未知全丢


def test_gate_by_permission_missing_doc_dropped():
    """permission_by_doc 缺该 doc（无 active chunk 等）→ 视为非 dept_internal，丢弃（fail-closed）。"""
    from opensearch_pipeline import access_grants
    assert access_grants.gate_by_permission({"DX": ["finance"]}, {}) == {}


def test_gate_by_permission_empty():
    from opensearch_pipeline import access_grants
    assert access_grants.gate_by_permission({}, {"D1": "dept_internal"}) == {}


# ── to_ha3_doc 推送门控（默认不推；显式 True 才推）──
def test_to_ha3_doc_gates_allowed_depts():
    from opensearch_pipeline.chunker import Chunk
    c = Chunk(chunk_id="c1", doc_id="D1", version_no=1, chunk_index=0,
              chunk_type="text", chunk_text="x", token_count=1,
              owner_dept="hr", permission_level="dept_internal",
              allowed_depts=["marketing", "production"])
    assert "allowed_depts" not in c.to_ha3_doc("id")                 # 默认不推（HA3 加字段前安全）
    d = c.to_ha3_doc("id", include_allowed_depts=True)
    assert d["allowed_depts"] == ["marketing", "production"]         # 显式 True → 推组码数组

    c2 = Chunk(chunk_id="c2", doc_id="D2", version_no=1, chunk_index=0,
               chunk_type="text", chunk_text="x", token_count=1)
    assert c2.to_ha3_doc("id", include_allowed_depts=True)["allowed_depts"] == []  # 空授权 → 推空数组（可清）


# ── 投影 outbox：enqueue（decide 同事务入队）+ drain（stage-3 幂等重试）──
def test_enqueue_acl_projection_upserts():
    """入队 = INSERT...ON DUPLICATE 复活（done_at=NULL, attempts=0），一行一 doc。"""
    from opensearch_pipeline import access_grants
    cur = _Cur([])
    access_grants.enqueue_acl_projection(cur, "D1", reason="revoked")
    assert "INSERT INTO fuling_knowledge.kb_acl_projection_outbox" in cur.sql
    assert "ON DUPLICATE KEY UPDATE done_at=NULL" in cur.sql and "attempts=0" in cur.sql
    assert cur.params == ("D1", "revoked")


def test_enqueue_acl_projection_skips_empty():
    from opensearch_pipeline import access_grants
    cur = _Cur([])
    access_grants.enqueue_acl_projection(cur, "", reason="x")
    assert cur.sql is None   # 空 doc_id → no-op


class _DrainCur:
    """drain 用桩游标：SELECT 待处理行返回预置；其余（UPDATE）记入 store['writes']。"""
    def __init__(self, store):
        self.store = store
        self._fetch = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store["sql"].append((sql, params))
        if sql.lstrip().startswith("SELECT") and "kb_acl_projection_outbox" in sql:
            self._fetch = list(self.store["pending"])
        else:
            self._fetch = []
            self.store["writes"].append((sql, params))

    def fetchall(self):
        return self._fetch


class _DrainConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _DrainCur(self.store)

    def commit(self):
        self.store["commits"] += 1

    def rollback(self):
        self.store["rollbacks"] += 1

    def close(self):
        pass


def _stub_drain(monkeypatch, *, flag=True, pending=(), status_by_doc=None, materialize_raises=()):
    store = {"sql": [], "writes": [], "pending": list(pending), "commits": 0, "rollbacks": 0}

    class _Rag:
        allowed_depts_acl = flag

    class _Cfg:
        rag = _Rag()

    monkeypatch.setattr("opensearch_pipeline.config.get_config", lambda: _Cfg())
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn", lambda: _DrainConn(store))

    def _mat(cur, doc_id, apply=True):
        if doc_id in materialize_raises:
            raise RuntimeError("materialize boom")
        return {"status": (status_by_doc or {}).get(doc_id, "unchanged"),
                "reset_chunks": 0, "version_no": 1}

    monkeypatch.setattr("opensearch_pipeline.access_grants.materialize_doc_allowed_depts", _mat)
    return store


def test_drain_outbox_flag_off_skipped(monkeypatch):
    from opensearch_pipeline import access_grants
    _stub_drain(monkeypatch, flag=False)
    res = access_grants.drain_acl_projection_outbox()
    assert res["skipped"] is True and res["processed"] == 0


def test_drain_outbox_marks_done_on_success(monkeypatch):
    from opensearch_pipeline import access_grants
    store = _stub_drain(monkeypatch, pending=[(1, "D1"), (2, "D2")],
                        status_by_doc={"D1": "retracted", "D2": "unchanged"})
    res = access_grants.drain_acl_projection_outbox()
    assert res["processed"] == 2 and res["done"] == 2 and res["locked"] == 0 and res["failed"] == 0
    done_updates = [w for w in store["writes"] if "done_at=NOW()" in w[0]]
    assert len(done_updates) == 2 and store["commits"] >= 2


def test_drain_outbox_skipped_locked_retries_not_done(monkeypatch):
    """skipped_locked（current version 正跑 stage-3）→ attempts++ 留 done_at=NULL 待下轮，不标 done。"""
    from opensearch_pipeline import access_grants
    store = _stub_drain(monkeypatch, pending=[(9, "DL")], status_by_doc={"DL": "skipped_locked"})
    res = access_grants.drain_acl_projection_outbox()
    assert res["locked"] == 1 and res["done"] == 0
    assert any("attempts=attempts+1" in w[0] and "skipped_locked" in w[0] for w in store["writes"])
    assert not any("done_at=NOW()" in w[0] for w in store["writes"])


def test_drain_outbox_failure_records_attempt_not_done(monkeypatch):
    """materialize 抛错 → 记 errors + attempts++（last_error），不标 done，不连累其余文档。"""
    from opensearch_pipeline import access_grants
    store = _stub_drain(monkeypatch, pending=[(1, "BAD"), (2, "OK")],
                        status_by_doc={"OK": "materialized"}, materialize_raises=("BAD",))
    res = access_grants.drain_acl_projection_outbox()
    assert res["failed"] == 1 and res["done"] == 1 and res["errors"]
    assert any("attempts=attempts+1" in w[0] and "last_error" in w[0].lower() for w in store["writes"])


def test_drain_outbox_preview_no_writes(monkeypatch):
    """commit=False 预览：不标 done/不写 outbox（只统计）。"""
    from opensearch_pipeline import access_grants
    store = _stub_drain(monkeypatch, pending=[(1, "D1")], status_by_doc={"D1": "retracted"})
    res = access_grants.drain_acl_projection_outbox(commit=False)
    assert res["processed"] == 1
    assert not any("done_at=NOW()" in w[0] for w in store["writes"])   # 预览不写


# ── P0-3 收尾：投影 outbox 的 DDL 契约 + decide 同事务原子入队 ────────────────
def test_acl_projection_outbox_table_has_committed_ddl():
    """enqueue/drain 的 INSERT/SELECT 目标表 kb_acl_projection_outbox 必须有已提交的 schema DDL。
    否则 fresh 部署/迁移上 decide 端点撞 errno 1146（表不存在）→ 同事务回滚 → 每次
    approve/reject/revoke 全失败。DDL 不可只存在于未提交的工作区文件（009 必须随代码入库）。"""
    from pathlib import Path
    schema_dir = Path(__file__).resolve().parent.parent / "schema"
    ddl = "".join(p.read_text(encoding="utf-8") for p in sorted(schema_dir.glob("*.sql")))
    assert "kb_acl_projection_outbox" in ddl, \
        "kb_acl_projection_outbox 缺 DDL：请提交 schema/009_acl_projection_outbox.sql"
    # enqueue/drain 实际读写的列都必须在 DDL 里（防列契约漂移）
    for col in ("doc_id", "reason", "attempts", "last_error", "enqueued_at", "done_at"):
        assert col in ddl, f"kb_acl_projection_outbox DDL 缺列 {col}"


def test_decide_enqueues_outbox_same_transaction_after_status_change():
    """撤权必达原子性（P0-3）：_kb_access_decide 在改 kb_access_request.status 的同一游标/事务内
    调用 enqueue_acl_projection，且 enqueue 在 status 变更【之后】（权威变更与投影意图原子提交）。
    enqueue 刻意不吞异常 → 失败则整笔回滚，绝不出现权威已改而 outbox 缺行的撕裂。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "opensearch_pipeline" / "api.py").read_text(encoding="utf-8")
    i = src.index("def _kb_access_decide(")
    body = src[i:i + 4000]   # 函数体（decide 约 80 行，4k 字符足够覆盖）
    assert "SET status=%s" in body, "decide 应改 kb_access_request.status"
    assert "enqueue_acl_projection(cur" in body, "decide 应同游标入队投影 outbox"
    assert body.index("SET status=%s") < body.index("enqueue_acl_projection(cur"), \
        "enqueue 必须在 status 变更之后（同事务原子入队，读己写）"
