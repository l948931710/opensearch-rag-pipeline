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
    """drain 用桩游标：SELECT 待处理行返回预置（批次8 起 3 列含 generation）；
    其余（UPDATE）记入 store['writes']，rowcount 可按 store['rowcount_zero_for'] 脚本化
    （命中子串的 UPDATE 返回 rowcount=0，模拟 mid-drain 复活的 CAS 落空）。"""
    def __init__(self, store):
        self.store = store
        self._fetch = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store["sql"].append((sql, params))
        if sql.lstrip().startswith("SELECT") and "kb_acl_projection_outbox" in sql:
            if self.store.get("no_gen_col") and "generation" in sql:
                raise RuntimeError("(1054, \"Unknown column 'generation' in 'field list'\")")
            self._fetch = list(self.store["pending"])
            self.rowcount = len(self._fetch)
        else:
            self._fetch = []
            self.store["writes"].append((sql, params))
            zero_for = self.store.get("rowcount_zero_for") or ()
            self.rowcount = 0 if any(t in sql for t in zero_for) else 1

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


def _stub_drain(monkeypatch, *, flag=True, pending=(), status_by_doc=None, materialize_raises=(),
                rowcount_zero_for=(), no_gen_col=False):
    _pending = [tuple(r) + (0,) * (3 - len(r)) for r in pending]   # 2 元组旧调用点补 generation=0
    store = {"sql": [], "writes": [], "pending": _pending, "commits": 0, "rollbacks": 0,
             "rowcount_zero_for": tuple(rowcount_zero_for), "no_gen_col": bool(no_gen_col)}

    class _Rag:
        allowed_depts_acl = flag

    class _Rds:
        database = "fuling_knowledge"   # _kb_db() 解析库名（drain 的 outbox SQL 现走 {_kb_db()}.）

    class _Cfg:
        rag = _Rag()
        rds = _Rds()

    monkeypatch.setattr("opensearch_pipeline.config.get_config", lambda: _Cfg())
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _DrainConn(store))

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
    import inspect

    from opensearch_pipeline import api

    # 经 api re-export 取函数源码（F-A2 后实现体在 routes/kb_access.py；inspect 跟随
    # 函数对象而非源文件路径，后续搬移不再破坏本不变量测试）
    body = inspect.getsource(api._kb_access_decide)
    # C3′/062（2026-08-03）：入口从 enqueue_acl_projection 升级为
    # record_acl_projection_invalidation —— 同事务里 **bump acl_epoch + 入队** 两件事一起做。
    # 原子性不变量不变（仍须在 status 变更之后、同游标），故本断言只换名不放松。
    assert "SET status=%s" in body, "decide 应改 kb_access_request.status"
    assert "record_acl_projection_invalidation(cur" in body, \
        "decide 应同游标登记投影失效（bump epoch + 入队 outbox）"
    assert body.index("SET status=%s") < body.index("record_acl_projection_invalidation(cur"), \
        "登记必须在 status 变更之后（同事务原子，读己写）"
    # bump **不得受 flag 门控**（Sam 2026-08-03 拍板）：flag 关闭期的权威变更若不 bump，
    # 水位永久丢失，开 flag 后这批文档永远判 unchanged = 原样复现 C3。
    _i_gate = body.find("rag.allowed_depts_acl")
    _i_bump = body.index("record_acl_projection_invalidation(cur")
    assert _i_gate == -1 or _i_bump < _i_gate, \
        "record_acl_projection_invalidation 被 allowed_depts_acl 门控了——flag 关闭期水位会丢"


# ── 批次8（ultra schema/009:34）：代次 CAS——mid-drain 复活的撤销不再被擦掉 ─────


def test_enqueue_bumps_generation_on_resurrect():
    from opensearch_pipeline import access_grants
    cur = _Cur([])
    access_grants.enqueue_acl_projection(cur, "D1", reason="revoked")
    assert "generation=generation+1" in cur.sql, "复活必须 +1 代次（drain CAS 的判据）"


def test_drain_done_mark_is_cas_by_generation(monkeypatch):
    """done 标记必须带 (id, generation) CAS——无代次时任何 mid-drain 复活都会被无条件
    done 覆盖（撤销静默丢失，HA3 残留陈旧 allowed_depts 直到全扫 reconcile）。"""
    from opensearch_pipeline import access_grants
    store = _stub_drain(monkeypatch, pending=[(1, "D1", 5)], status_by_doc={"D1": "retracted"})
    res = access_grants.drain_acl_projection_outbox()
    assert res["done"] == 1
    done = [w for w in store["writes"] if "done_at=NOW()" in w[0]]
    assert done and "AND generation=%s" in done[0][0]
    assert done[0][1] == (1, 5)


def test_drain_raced_resurrection_left_pending(monkeypatch):
    """CAS 落空（done UPDATE rowcount=0 = 撤销在物化与标记之间复活了该行）→ 不计 done、
    行保持待处理，下轮按新代次重新物化（撤销必达）。"""
    from opensearch_pipeline import access_grants
    store = _stub_drain(monkeypatch, pending=[(1, "D1", 5)], status_by_doc={"D1": "retracted"},
                        rowcount_zero_for=("done_at=NOW()",))
    res = access_grants.drain_acl_projection_outbox()
    assert res["done"] == 0 and res.get("raced") == 1
    assert any("done_at=NOW()" in w[0] for w in store["writes"])   # 尝试过但 CAS 落空


def test_drain_falls_back_when_generation_column_absent(monkeypatch):
    """049 未 apply（1054 Unknown column）→ 回退旧无代次语义（代码可先部署，🔒 apply user-gated）。"""
    from opensearch_pipeline import access_grants
    store = _stub_drain(monkeypatch, pending=[(1, "D1", 0)], status_by_doc={"D1": "unchanged"},
                        no_gen_col=True)
    res = access_grants.drain_acl_projection_outbox()
    assert res["done"] == 1
    done = [w for w in store["writes"] if "done_at=NOW()" in w[0]]
    assert done and "generation" not in done[0][0] and done[0][1] == (1,)


# ── C3′/062（2026-08-03）：投影失效代次的写方语义 ─────────────────────────────
class _EpochCur:
    """记录所有 execute 的假游标；可模拟 062 未 apply（acl_epoch 列不存在 → 1054）。"""

    def __init__(self, has_epoch=True):
        self.has_epoch = has_epoch
        self.sqls = []

    def execute(self, sql, params=None):
        self.sqls.append((" ".join(sql.split()), params))
        if not self.has_epoch and "acl_epoch" in sql:
            raise Exception(1054, "Unknown column 'acl_epoch' in 'field list'")

    def fetchone(self):
        return None

    def fetchall(self):
        return []


def test_record_invalidation_bumps_epoch_and_enqueues_same_cursor():
    """★ 唯一登记入口必须【同游标】做两件事：acl_epoch+1 与 outbox 入队。"""
    from opensearch_pipeline.access_grants import record_acl_projection_invalidation
    cur = _EpochCur()
    record_acl_projection_invalidation(cur, "D1", reason="unit")
    joined = " || ".join(s for s, _ in cur.sqls)
    assert "SET acl_epoch = acl_epoch + 1" in joined, "未 bump 投影失效代次"
    assert "kb_acl_projection_outbox" in joined, "未入队 outbox"
    # bump 必须在入队之前/同事务内（顺序不强制，但两者都必须发生在同一游标）
    assert len(cur.sqls) >= 2


def test_record_invalidation_degrades_when_062_not_applied():
    """062 未 apply（1054）⇒ 只入队不 bump，**不得阻断调用方事务**（与 049/050 同型降级）。"""
    from opensearch_pipeline.access_grants import record_acl_projection_invalidation
    cur = _EpochCur(has_epoch=False)
    record_acl_projection_invalidation(cur, "D1", reason="unit")   # 不应抛
    joined = " || ".join(s for s, _ in cur.sqls)
    assert "kb_acl_projection_outbox" in joined, "降级路径仍必须入队"


def test_record_invalidation_reraises_non_1054():
    """非 1054 的错误照抛 —— 持久保证不吞（与 enqueue 同纪律）。"""
    import pytest as _pt
    from opensearch_pipeline.access_grants import record_acl_projection_invalidation

    class _Boom(_EpochCur):
        def execute(self, sql, params=None):
            raise Exception(1213, "Deadlock found")

    with _pt.raises(Exception):
        record_acl_projection_invalidation(_Boom(), "D1")


def test_stage2_chunk_insert_stamps_epoch_and_is_capability_aware():
    """★ stamp 点③：新 chunk 出生即带 acl_epoch；062 未 apply 时降级为 27 列不带该列。

    漏这处 ⇒ 每篇新摄取文档的 chunk 天生 NULL、永久 dirty，sweep 对新文档永不收敛
    （codex 第一批 BLOCKER）。capability 探测保证「先部署后 apply 安全」。
    """
    import inspect
    from opensearch_pipeline import pipeline_nodes
    src = inspect.getsource(pipeline_nodes.node_write_chunk_meta)
    assert "_has_epoch_col" in src, "缺 capability 探测 ⇒ 062 未 apply 的库会 1054 打挂整个 chunk 写入"
    # ⚠️ 钉住【条件表达式本身】——只查 "acl_epoch" in src 会被注释/批查语句满足（假绿）
    assert '", acl_epoch" if _has_epoch_col' in src, "INSERT 列未按 capability 条件带 acl_epoch"
    assert '", %s" if _has_epoch_col' in src, "INSERT 占位符未随列条件变化"
    assert "_epoch_by_doc.get(chunk.doc_id)" in src, "参数元组未 stamp 权威 epoch"
    assert "{_epoch_col}" in src and "{_epoch_ph}" in src, "insert_sql 未插入条件列/占位符"
    # projection_complete：解析失败即不 stamp
    assert "_proj_ok" in src, "缺 projection_complete 守卫"
    assert src.index("_proj_ok = False") > 0
    # 姿态 A：RDS 投影不再受 flag 门控
    assert "if valid_chunks:" in src, "allowed_depts 计算仍被 flag 门控（应为姿态 A：始终计算）"


# ── C3′/062 B2：certify-only（值对但 epoch 落后 ⇒ 只盖章不重推）────────────────
def test_projection_rows_all_match_rejects_union_masking():
    """★ 逐行校验必须拒绝「并集恰好等于期望」的半投影状态。

    codex 反例：期望 ["finance"]，一行 ["finance"]、一行 NULL —— `current_allowed_for_doc`
    的并集仍等于期望，据此盖章就把「一半 chunk 根本没投影」认证成了「已按权威投影」。
    """
    from opensearch_pipeline.access_grants import projection_rows_all_match

    class _Cur:
        def __init__(self, rows): self.rows = rows
        def execute(self, sql, params=None): pass
        def fetchall(self): return self.rows

    # 全等 ⇒ 可认证
    assert projection_rows_all_match(
        _Cur([("finance", '["finance"]'), ("finance", '["finance"]')]), "D", 1, ["finance"], "finance")
    # 并集相等但逐行不等 ⇒ **必须拒绝**
    assert not projection_rows_all_match(
        _Cur([("finance", '["finance"]'), ("finance", None)]), "D", 1, ["finance"], "finance")
    # owner 漂移 ⇒ 拒绝
    assert not projection_rows_all_match(
        _Cur([("finance", '["finance"]'), ("production", '["finance"]')]), "D", 1, ["finance"], "finance")
    # 坏 JSON ⇒ 拒绝（fail-closed）
    assert not projection_rows_all_match(
        _Cur([("finance", "{坏的")]), "D", 1, ["finance"], "finance")
    # 无 active chunk ⇒ 没有可认证对象
    assert not projection_rows_all_match(_Cur([]), "D", 1, ["finance"], "finance")


def test_materialize_has_certify_path_before_returning_unchanged():
    """★ 值相等时不得直接 return unchanged —— 必须先尝试 certify（只盖章、不动 index_status）。

    这正是 72c9e22 落码时的缺口：两个分支都在 stamp 之前 return，
    「值对但 acl_epoch IS NULL」的文档永远盖不上章、永久 dirty。
    """
    import inspect
    from opensearch_pipeline import access_grants
    src = inspect.getsource(access_grants.materialize_doc_allowed_depts)
    assert src.count('"certified"') == 2, "node 与 legacy 两个分支都必须有 certify 路径"
    assert "projection_rows_all_match" in src, "certify 未用逐行校验（并集口径会误认证）"
    # certify 只写 epoch，绝不置 NOT_INDEXED（否则会无谓触发全量重推 HA3）
    for seg in src.split('"certified"')[:-1]:
        tail = seg[-400:]
        assert "SET acl_epoch=%s" in tail, "certify 分支未只写 acl_epoch"
        assert "NOT_INDEXED" not in tail.split("SET acl_epoch=%s")[-1], \
            "certify 分支不得置 NOT_INDEXED（值没变就不该重推）"


def test_reconcile_commits_certified_status():
    """★ reconcile 只在 materialized/retracted 时 commit —— 漏掉 certified 会让刚写的 epoch
    要么丢、要么被下一篇的 commit 意外带上（B1 的事务语义在此露头）。"""
    import inspect
    from opensearch_pipeline import allowed_depts_reconcile
    src = inspect.getsource(allowed_depts_reconcile.reconcile_allowed_depts)
    assert 'status == "certified"' in src, "reconcile 未识别 certified"
    seg = src.split('status == "certified"')[1][:400]
    assert "conn.commit()" in seg, "certified 分支未提交 —— epoch 会丢"
