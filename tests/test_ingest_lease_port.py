# -*- coding: utf-8 -*-
"""tests/test_ingest_lease_port.py — PR-4 租约 main 移植版共识测试（codex 六阶段，2026-08-02）。

移植版与 op0 原版的三处刻意差异，各配决定性测试：
  R1  分类三路径保持 main 的 meta→dv 写序，dv 归属判定改 verify_for_update（行锁+
      holder/epoch 相等），**不以 rowcount 判租**——frozen 路径 SET 全为确定性字面量，
      重试必 changed-rows=0，op0 的 check_fenced_write 在此必然假阳（B1）。
  墓碑 非自愿失锁后 fence/verify 恒拒绝（op0 版 discard 后退化 no-op，僵尸后续写全裸）；
      自愿 discard（终态已落）保持释放后无栅栏旧语义。
  R3  parity 的 dv FAILED 回滚块整体挂 flag 门（off 臂与 pre-port main 逐字节一致）；
      on 臂 ownership 前置过滤：全丢锁不写不抛，混合批仅幸存项写回+计数+照常 raise。

另含：off 臂 splice 邻接源检（字节等价 by construction）、A3 探针两臂、R2 源结构、
stale 清扫文本条件化。全 mock，无真库（真库故障注入在 test_ingest_lease_db.py）。
"""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("RAG_SIMULATE", "true")

from opensearch_pipeline import ingest_lease
from opensearch_pipeline.ingest_lease import LeaseLost, LeaseSet

from tests.test_unfrozen_rechunk_guard import (  # noqa: E402 — 复用同一 mock 语义
    DB_PATH,
    LLM_PATH,
    _doc,
    _FakeConn,
    _FakeCursor,
    _good_classification,
)


class _NoopCursor:
    rowcount = 1

    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


@pytest.fixture
def lease_on(monkeypatch):
    monkeypatch.setenv("RAG_INGEST_LEASE_ENABLE", "true")


# ─────────────────────────────────────────────────────────────────────
# 1. 墓碑语义（非自愿失锁恒拒绝 / 自愿释放回归旧语义 / 重领清墓碑 / off 优先）
# ─────────────────────────────────────────────────────────────────────


def _registered_set(key=("d", 1), epoch=7):
    ls = LeaseSet()
    ls.fetch_and_register(_NoopCursor(rows=[(key[0], key[1], epoch)]), [key])
    assert ls.epoch(key) == epoch
    return ls


def _lose_via_renew_all(ls, key):
    """renew_all 短缺回读发现被接管——op0 版此路径 discard 后栅栏退化 no-op（真洞）。"""

    class _RenewCur(_NoopCursor):
        rowcount = 0          # UPDATE 短缺

        def fetchall(self):   # 回读 holder=me 存活集：空 ⇒ 全丢
            return []

    assert ls.renew_all(_RenewCur()) == 1
    assert ls.epoch(key) is None


def test_involuntary_loss_leaves_tombstone_all_helpers_refuse(lease_on):
    key = ("d", 1)
    ls = _registered_set(key)
    _lose_via_renew_all(ls, key)
    with pytest.raises(LeaseLost):
        ls.fence_where_sql(key)
    with pytest.raises(LeaseLost):
        ls.fence_where_params(key)
    with pytest.raises(LeaseLost):
        ls.check_fenced_write(_NoopCursor(), key)
    with pytest.raises(LeaseLost):
        ls.verify_for_update(_NoopCursor(), key)
    cur = _NoopCursor()
    assert ls.verify_still_held(cur, key) is False
    assert cur.executed == [], "墓碑判定不触库"


def test_tombstoned_key_not_renewed(lease_on):
    key = ("d", 1)
    ls = _registered_set(key)
    _lose_via_renew_all(ls, key)
    cur = _NoopCursor()
    ls.maybe_renew(cur, key)          # 不在 _epochs ⇒ no-op
    assert ls.renew_all(cur) == 0     # lost 键不参与批量续租
    assert cur.executed == []


def test_voluntary_discard_restores_legacy_noop(lease_on):
    """终态已落后的 discard()（DONE→vlm 覆写等点位依赖）：释放后辅助写走无栅栏旧语义。"""
    key = ("d", 1)
    ls = _registered_set(key)
    ls.discard(key)
    assert ls.fence_where_sql(key) == ""
    assert ls.fence_where_params(key) == ()
    ls.check_fenced_write(_NoopCursor(rows=[]), key)      # no-op，不 raise
    ls.verify_for_update(_NoopCursor(), key)              # no-op
    assert ls.verify_still_held(_NoopCursor(), key) is True


def test_voluntary_discard_also_clears_tombstone(lease_on):
    key = ("d", 1)
    ls = _registered_set(key)
    _lose_via_renew_all(ls, key)
    ls.discard(key)                   # 显式放手：墓碑一并清除（回归未登记语义）
    assert ls.fence_where_sql(key) == ""


def test_reclaim_clears_tombstone(lease_on):
    key = ("d", 1)
    ls = _registered_set(key, epoch=7)
    _lose_via_renew_all(ls, key)
    # 同 run 合法重认领（新 epoch 到手）⇒ 墓碑清除、栅栏恢复
    ls.fetch_and_register(_NoopCursor(rows=[(key[0], key[1], 9)]), [key])
    assert ls.epoch(key) == 9
    assert ls.fence_where_params(key) == (ingest_lease.holder_id(), 9)


def test_off_arm_precedence_over_tombstone(lease_on, monkeypatch):
    """flag on 期间失锁留墓碑，随后 flag 关——off 早退优先，运行期残留墓碑零影响。"""
    key = ("d", 1)
    ls = _registered_set(key)
    _lose_via_renew_all(ls, key)
    monkeypatch.delenv("RAG_INGEST_LEASE_ENABLE", raising=False)
    assert ls.fence_where_sql(key) == ""
    assert ls.fence_where_params(key) == ()
    ls.check_fenced_write(_NoopCursor(), key)
    ls.verify_for_update(_NoopCursor(), key)
    assert ls.verify_still_held(_NoopCursor(), key) is True


# ─────────────────────────────────────────────────────────────────────
# 2. R1：分类持久化 = main 写序（meta→dv）+ verify_for_update，不以 rowcount 判租
# ─────────────────────────────────────────────────────────────────────


class _LeaseCursor(_FakeCursor):
    """_FakeCursor + 租约 SELECT 应答脚本。

    · fetch_and_register 的 lease_epoch SELECT → [(doc, ver, epoch)]
    · verify_for_update 的 FOR UPDATE SELECT → (verify_holder, verify_epoch)
    · 分类 dv UPDATE 的 rowcount 可脚本化（R1：为 0 也不得判 LeaseLost）
    """

    def __init__(self, existing, executed, state):
        super().__init__(existing, executed)
        self._st = state

    def execute(self, sql, params=None):
        s = (sql or "").strip().upper()
        if s.startswith("SELECT") and "LEASE_EPOCH" in s:
            self._executed.append((sql, params))
            if "FOR UPDATE" in s:
                self._last = [self._st["verify_row"]]
            else:
                self._last = list(self._st["register_rows"])
            self.rowcount = len(self._last)
            return
        super().execute(sql, params)
        if "UPDATE DOCUMENT_VERSION" in s and "CONTENT_CLASSIFIED" in s:
            self.rowcount = self._st.get("dv_rowcount", 1)
        else:
            self.rowcount = 1


class _LeaseConn(_FakeConn):
    def __init__(self, existing, executed, state):
        super().__init__(existing, executed)
        self._st = state
        self.rollbacks = 0

    def cursor(self, *a, **k):
        return _LeaseCursor(self._existing, self._executed, self._st)

    def rollback(self):
        self.rollbacks += 1


def _run_classify(monkeypatch, mock_db, mock_llm, state):
    monkeypatch.setenv("RAG_INGEST_LEASE_ENABLE", "true")
    mock_llm.return_value = _good_classification()
    executed = []
    conns = []

    def _factory(*a, **k):
        c = _LeaseConn(set(), executed, state)
        conns.append(c)
        return c

    mock_db.side_effect = _factory
    docs = [_doc("d1")]
    ctx = {"canonicals": docs, "simulate_db": False, "simulate_api": False}
    import opensearch_pipeline.pipeline_nodes as pn
    pn.node_classify_and_risk_assess(ctx)
    return ctx, executed, conns


@patch(LLM_PATH)
@patch(DB_PATH)
def test_classify_success_keeps_main_order_meta_then_fenced_dv(
        mock_db, mock_llm, monkeypatch, llm_key_present):
    """R1：meta 先（main 全局 meta→dv 纪律）→ FOR UPDATE 验租 → dv 带栅栏。"""
    from opensearch_pipeline.ingest_lease import holder_id
    st = {"register_rows": [("d1", 1, 7)], "verify_row": (None, None), "dv_rowcount": 1}
    st["verify_row"] = (holder_id(), 7)
    ctx, executed, _ = _run_classify(monkeypatch, mock_db, mock_llm, st)
    meta_ix = [i for i, (s, _p) in enumerate(executed)
               if "document_meta" in s and "category_l1" in s]
    verify_ix = [i for i, (s, _p) in enumerate(executed)
                 if "FOR UPDATE" in s.upper() and "LEASE_EPOCH" in s.upper()]
    dv_ix = [i for i, (s, _p) in enumerate(executed)
             if "UPDATE document_version" in s and "CONTENT_CLASSIFIED" in s
             and "classification_confidence" in s]
    assert meta_ix and verify_ix and dv_ix
    assert meta_ix[0] < dv_ix[0], "R1：保持 main 的 meta→dv 写序（不采 op0 dv-first）"
    assert any(v < dv_ix[0] for v in verify_ix), "dv 写前必须 FOR UPDATE 验租"
    dv_sql, dv_params = executed[dv_ix[0]]
    assert "lease_holder = %s AND lease_epoch = %s" in dv_sql
    assert tuple(dv_params[-2:]) == (holder_id(), 7)
    assert [d["doc_id"] for d in ctx["canonicals"]] == ["d1"]


@patch(LLM_PATH)
@patch(DB_PATH)
def test_classify_same_value_rowcount_zero_is_not_lease_lost(
        mock_db, mock_llm, monkeypatch, llm_key_present):
    """R1 回归（B1）：dv UPDATE 同值 changed-rows=0 且归属有效 ⇒ 不判 LeaseLost。
    op0 版 check_fenced_write 在此假阳（frozen 重试 100% 触发）。"""
    from opensearch_pipeline.ingest_lease import holder_id
    st = {"register_rows": [("d1", 1, 7)], "verify_row": (holder_id(), 7),
          "dv_rowcount": 0}
    ctx, executed, conns = _run_classify(monkeypatch, mock_db, mock_llm, st)
    assert [d["doc_id"] for d in ctx["canonicals"]] == ["d1"], \
        "同值 UPDATE 不得被误判为丢锁弃单"
    assert all(c.rollbacks == 0 for c in conns)


@patch(LLM_PATH)
@patch(DB_PATH)
def test_classify_lease_lost_rolls_back_and_abandons_doc(
        mock_db, mock_llm, monkeypatch, llm_key_present):
    """验租失败（他方 holder）⇒ 整事务回滚（先写的 meta 一并不落）、弃单继续批、不抛。"""
    st = {"register_rows": [("d1", 1, 7)],
          "verify_row": ("zombie-hunter-9-deadbeef", 8), "dv_rowcount": 1}
    ctx, executed, conns = _run_classify(monkeypatch, mock_db, mock_llm, st)
    assert ctx["canonicals"] == [], "丢租约文档必须从 canonicals 剔除（归新持有者）"
    assert not any("UPDATE document_version" in s and "CONTENT_CLASSIFIED" in s
                   and "classification_confidence" in s for s, _p in executed), \
        "验租失败后 dv UPDATE 不得发出"
    assert any(c.rollbacks >= 1 for c in conns), \
        "meta 已写但必须随事务回滚（LeaseLost ⇒ rollback）"


# ─────────────────────────────────────────────────────────────────────
# 3. R3：parity 两臂
# ─────────────────────────────────────────────────────────────────────


class _ParityCur:
    def __init__(self, executed, verify_rows):
        self._executed = executed
        self._verify_rows = verify_rows  # {(doc,ver): row}
        self._last = None
        self.rowcount = 1

    def execute(self, sql, params=None):
        self._executed.append((sql, params))
        s = (sql or "").strip().upper()
        if s.startswith("SELECT") and "FOR UPDATE" in s:
            self._last = self._verify_rows.get((params[0], params[1]))

    def fetchone(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ParityConn:
    def __init__(self, executed, verify_rows):
        self._executed = executed
        self._verify_rows = verify_rows
        self.rollbacks = 0
        self.commits = 0

    def cursor(self):
        return _ParityCur(self._executed, self._verify_rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _parity_harness(monkeypatch, verify_rows, budget_calls):
    import opensearch_pipeline.pipeline_nodes as pn
    executed = []
    conn = _ParityConn(executed, verify_rows)
    monkeypatch.setattr(pn, "_get_db_conn", lambda **k: conn)
    monkeypatch.setattr(
        pn, "_fail_chunks_with_retry_budget",
        lambda cur, ids, **k: (budget_calls.append(list(ids)), [])[1])
    monkeypatch.setattr(pn, "_mark_docs_needs_review_for_dead", lambda cur, dead: None)
    monkeypatch.setattr("opensearch_pipeline.env_guard.assert_destructive_write_allowed",
                        lambda *a, **k: None)
    return executed, conn


def _pchunk(cid, doc, ver):
    return SimpleNamespace(chunk_id=cid, doc_id=doc, version_no=ver,
                           index_status=None, index_error_code=None,
                           index_error_message=None)


def test_parity_off_arm_never_writes_document_version(monkeypatch):
    """B3 硬约束：flag off 时 parity 与 pre-port main 逐字节一致——零 DV 写、照常 raise。"""
    import opensearch_pipeline.pipeline_nodes as pn
    monkeypatch.delenv("RAG_INGEST_LEASE_ENABLE", raising=False)
    budget_calls = []
    executed, conn = _parity_harness(monkeypatch, {}, budget_calls)
    ch = _pchunk("c1", "D1", 2)
    ctx = {}
    cfg = SimpleNamespace(rds=SimpleNamespace(host="localhost"))
    with pytest.raises(RuntimeError, match="Stage-3 parity"):
        pn._persist_parity_failed_and_raise(ctx, cfg, [ch], [], 2)
    assert not any("document_version" in (s or "") for s, _p in executed), \
        "off 臂不得新增任何 document_version 写（pre-port main 没有）"
    assert budget_calls == [["c1"]]
    assert ("D1", 2) in ctx["failed_doc_versions"]
    assert ch.index_status is not None, "off 臂内存状态同步照旧"


def test_parity_on_arm_all_lost_returns_without_write_or_raise(monkeypatch, lease_on):
    import opensearch_pipeline.pipeline_nodes as pn
    budget_calls = []
    executed, conn = _parity_harness(monkeypatch, {("D1", 2): ("thief", 9)}, budget_calls)
    ch = _pchunk("c1", "D1", 2)
    ctx = {}
    ls = ingest_lease.get_lease_set(ctx)
    ls.fetch_and_register(_NoopCursor(rows=[("D1", 2, 5)]), [("D1", 2)])
    cfg = SimpleNamespace(rds=SimpleNamespace(host="localhost"))
    pn._persist_parity_failed_and_raise(ctx, cfg, [ch], [], 2)   # 不 raise
    assert budget_calls == [], "全丢锁：chunk_meta 零写"
    assert not any((s or "").strip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
                   for s, _p in executed), "全丢锁：零写语句（SELECT FOR UPDATE 验租除外）"
    assert ch.index_status is None, "全丢锁：内存状态零改动"
    assert "failed_doc_versions" not in ctx, "全丢锁：零失败计数"
    assert conn.rollbacks >= 1, "验租行锁必须释放（rollback）"


def test_parity_on_arm_mixed_batch_writes_survivors_only(monkeypatch, lease_on):
    from opensearch_pipeline.ingest_lease import holder_id
    import opensearch_pipeline.pipeline_nodes as pn
    budget_calls = []
    verify_rows = {("LIVE", 1): (holder_id(), 5), ("LOST", 1): ("thief", 9)}
    executed, conn = _parity_harness(monkeypatch, verify_rows, budget_calls)
    live, lost = _pchunk("cl", "LIVE", 1), _pchunk("cx", "LOST", 1)
    ctx = {}
    ls = ingest_lease.get_lease_set(ctx)
    ls.fetch_and_register(
        _NoopCursor(rows=[("LIVE", 1, 5), ("LOST", 1, 5)]), [("LIVE", 1), ("LOST", 1)])
    cfg = SimpleNamespace(rds=SimpleNamespace(host="localhost"))
    with pytest.raises(RuntimeError, match="Stage-3 parity"):
        pn._persist_parity_failed_and_raise(ctx, cfg, [live, lost], [], 2)
    assert budget_calls == [["cl"]], "丢锁 doc 的 chunk 不得写回"
    assert lost.index_status is None, "丢锁 doc 内存状态零改动"
    dv_sqls = [" ".join((s or "").split()) for s, p in executed
               if "UPDATE document_version" in (s or "")]
    assert len(dv_sqls) == 1 and "AND index_status = 'PROCESSING'" in dv_sqls[0], \
        "仅幸存 doc 走 dv PROCESSING→FAILED CAS"
    assert ctx["failed_doc_versions"] == {("LIVE", 1)}, "失败计数只含幸存键"


# ─────────────────────────────────────────────────────────────────────
# 4. 源检：off 臂 splice 邻接（字节等价 by construction）+ R2/其余点位结构
# ─────────────────────────────────────────────────────────────────────


def test_pipeline_nodes_splice_adjacency():
    """所有 claim/clear/fence 都以「空串 splice」形式贴在原字面量上——off 臂渲染即旧 SQL。"""
    import inspect
    import opensearch_pipeline.pipeline_nodes as pn
    src = inspect.getsource(pn)
    for marker in (
        "SET content_process_status = 'PROCESSING'{ingest_lease.claim_set_sql()}",
        "SET index_status = '{DocVersionIndexStatus.PROCESSING}'{ingest_lease.claim_set_sql()}",
        "SET index_status = '{DocVersionIndexStatus.FAILED}'{ingest_lease.clear_set_sql()}",
        "processed_at = NOW(){ingest_lease.clear_set_sql()}",
        "AND {ingest_lease.takeover_where_sql()}",
    ):
        assert marker in src, f"splice 邻接缺失: {marker}"


def test_orchestrator_splice_and_probe_arms(monkeypatch):
    import inspect
    import opensearch_pipeline.dataworks_orchestrator as orch
    src = inspect.getsource(orch)
    assert 'OR {ingest_lease.takeover_where_sql("dv.")}' in src, "loader/计数谓词走 seam"
    assert ">2h without progress; reset for retry" in src, \
        "stale 清扫 off 臂留痕文本必须逐字节保留（R4a）"
    assert "lease expired / >2h no progress" in src, "on 臂文本存在"
    # A3 live-lock 探针两臂（R4b）：off 返回逐字节旧三元组
    entry = orch._UNCLAIMABLE_PROBES[3][1]
    assert callable(entry)
    monkeypatch.delenv("RAG_INGEST_LEASE_ENABLE", raising=False)
    label, sql, hint = entry()
    assert label == "stage-3 版本锁未释放 (index_status=PROCESSING 且未满 2h)"
    assert "updated_at >= NOW() - INTERVAL 2 HOUR" in sql and "NOT (" not in sql
    assert hint.startswith("2h 内的锁按设计不可抢占")
    monkeypatch.setenv("RAG_INGEST_LEASE_ENABLE", "true")
    label2, sql2, _hint2 = entry()
    assert "NOT (" in sql2 and "lease_expires_at" in sql2
    assert "租约" in label2


def test_deactivate_failure_txn_verifies_before_all_three_writes():
    """R2：conn_fail 事务逐 doc 先 verify_for_update，LeaseLost ⇒ 跳过三组写。"""
    import inspect
    import opensearch_pipeline.pipeline_nodes as pn
    src = inspect.getsource(pn.node_deactivate_old_chunks)
    fail_blk = src[src.index("conn_fail = _get_db_conn"):]
    v = fail_blk.index("verify_for_update")
    assert v < fail_blk.index("SET index_status = '{DocVersionIndexStatus.FAILED}'")
    assert v < fail_blk.index("SET index_status = '{ChunkIndexStatus.FAILED}'"
                              .replace("ChunkIndexStatus.FAILED",
                                       "ChunkIndexStatus.FAILED"))
    lost_arm = fail_blk[fail_blk.index("except ingest_lease.LeaseLost"):]
    assert "continue" in lost_arm[:300], "丢锁 doc 必须 continue 跳过三组写"


def test_dag3_rollback_carries_lease_discipline():
    import inspect
    from opensearch_pipeline.dataworks_orchestrator import run_stage
    src = inspect.getsource(run_stage)
    rb = src[src.index("Rolling back PROCESSING locks"):]
    assert "fence_where_sql" in rb, "回滚 WHERE 必须带 epoch 栅栏（防覆盖新持有者在跑锁）"
    assert "clear_set_sql" in rb, "回滚 SET 必须清租约列（FAILED 行残留租约=takeover 地雷）"
    assert ".epoch(" in rb and "lease already lost" in rb, \
        "节点内已判 LeaseLost 的 key 必须跳过（归新持有者收敛）"


def test_update_index_status_txn_head_verify_and_skip():
    import inspect
    import opensearch_pipeline.pipeline_nodes as pn
    src = inspect.getsource(pn.node_update_index_status)
    assert "_l3_lost" in src and "verify_for_update" in src
    assert src.index("verify_for_update") < src.index("opensearch_bulk_job"), \
        "验租必须在任何回写之前（事务首）"
    assert "in _l3_lost:" in src, "丢锁 (doc,ver) 的回写必须跳过"
