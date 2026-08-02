# -*- coding: utf-8 -*-
"""
test_ha3_verify_repush.py — DAG-3 节点 04b（node_verify_and_repush）单测。

无需阿里云实例：通过 mock HA3 client（fetch/push_documents）+ mock RDS conn 验证：
推送后 HA3 物理存在性校验（**官方 /vector-service/fetch**，PK 相符）→ 有界补推 → 三终态
（all-present / DROP / UNKNOWN）→ 失败写回 FAILED（分类、rowcount 闭环、阻断 node 05）。

⚠️ 2026-07-22 C2：权威判据由零向量 point-read 换成官方 fetch。零向量 query 与任何向量
内积恒为 0 ⇒ 全部得分并列 ⇒ 返回哪个子集由引擎召回逻辑决定，会把**在场行判 missing**
（实测某在场行零向量返 0 命中而 fetch 正常取回）⇒ 无谓补推 + 04b raise 阻断节点 05
⇒ 旧版本永不停用、双版本长存。故本文件的存在性用例一律走 fetch，并断言存在性路径
**零 client.query 调用**。

fetch 的失败粒度是**批**（≤100/批）：批异常、返回请求集外 PK、同批重复 PK 一律使
**整批 UNKNOWN**（绝不误判 missing）——与旧的"逐 PK 隔离"不同，相关用例已按批语义重写。
"""

import sys
import types

import pytest

import opensearch_pipeline.pipeline_nodes as pn
from opensearch_pipeline.chunker import Chunk


# ── HA3 SDK mock（补齐 QueryRequest；与 test_ha3_engine 的注入兼容/互不覆盖）──────────
def _ensure_ha3_mock():
    name = "alibabacloud_ha3engine_vector"
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
        sys.modules[name + ".models"] = types.ModuleType(name + ".models")
    models = sys.modules[name + ".models"]
    if not hasattr(models, "PushDocumentsRequest"):
        class PushDocumentsRequest:
            def __init__(self, body=None):
                self.body = body
        models.PushDocumentsRequest = PushDocumentsRequest
    if not hasattr(models, "QueryRequest"):
        class QueryRequest:
            def __init__(self, table_name=None, vector=None, top_k=None,
                         include_vector=None, output_fields=None, filter=None):
                self.table_name = table_name
                self.vector = vector
                self.top_k = top_k
                self.include_vector = include_vector
                self.output_fields = output_fields
                self.filter = filter
        models.QueryRequest = QueryRequest
    if not hasattr(models, "FetchRequest"):    # C2：权威存在性走官方 fetch
        class FetchRequest:
            def __init__(self, table_name=None, ids=None, include_vector=None,
                         output_fields=None):
                self.table_name = table_name
                self.ids = ids
                self.include_vector = include_vector
                self.output_fields = output_fields
        models.FetchRequest = FetchRequest
    # ⚠️ 本函数把假 models 模块塞进 sys.modules，会**遮蔽真 SDK**（`if name not in sys.modules`
    # 先到先得）。倒排枚举 helper（clients.ha3_enumerate_bucket）运行时 import 这两个类，
    # 缺了就会被判 unhealthy → 同批运行的 reconcile/L6/enumerator 用例集体假红（实测踩过）。
    # 按仓库既有惯例就地补属性，保证谁先注入都不缺件。
    if not hasattr(models, "SearchRequest"):
        class SearchRequest:
            def __init__(self, table_name=None, size=None, from_=None, order=None,
                         output_fields=None, knn=None, text=None, rank=None):
                self.table_name = table_name
                self.size = size
                self.from_ = from_
                self.order = order
                self.output_fields = output_fields
                self.knn = knn
                self.text = text
                self.rank = rank
        models.SearchRequest = SearchRequest
    if not hasattr(models, "TextQuery"):
        class TextQuery:
            def __init__(self, query_string=None, query_params=None, filter=None,
                         weight=None, terminate_after=None):
                self.query_string = query_string
                self.query_params = query_params
                self.filter = filter
                self.weight = weight
                self.terminate_after = terminate_after
        models.TextQuery = TextQuery


_ensure_ha3_mock()


def _resp(items):
    return types.SimpleNamespace(body={"result": items})


# ── Fakes ────────────────────────────────────────────────────────────────────
class FakeHA3:
    """可配置的 HA3 client：官方 fetch(批 ≤100) + push。

    ⚠️ 不实现 `query`——存在性路径若还调零向量 query 会立刻 AttributeError，
    这本身就是"不得回退零向量判存在性"的回归断言。"""

    def __init__(self, present=(), never_heal=(), fetch_raise=(), wrong_id=(),
                 lag_first=(), stale=(), stale_heals=True, texts=None):
        self.present = set(present)            # 当前"真在" HA3 的 pk
        self.never_heal = set(never_heal)      # 补推也不会进 HA3（持久 DROP）
        # ⚠️ 批语义：只要批内**任一** pk 命中 fetch_raise，整批抛异常 → 整批 UNKNOWN
        self.fetch_raise = set(fetch_raise)
        # 返回请求集外的 PK（畸形响应）→ 整批 UNKNOWN（**不是** MISSING/DROP）
        self.wrong_id = set(wrong_id)
        self.lag_first = set(lag_first)        # 在 present 但首次 fetch 不返回（最终一致性滞后）
        self.stale = set(stale)               # present 但 chunk_text_store 陈旧（≠ 内存 t{pk}）→ drift
        self.stale_heals = stale_heals        # 补推后陈旧内容是否变一致
        self.texts = dict(texts or {})        # 显式覆盖某 pk 返回的 chunk_text_store
        self.push_calls = []                   # list[list[int]]
        self.fetch_calls = []                  # list[list[int]]：每批请求的 id 列表
        self._reads = {}
        self._repushed = set()

    @property
    def fetched_pks(self):
        """所有被 fetch 过的 pk（扁平化，供等价于旧 point_reads 的断言）。"""
        return [pk for batch in self.fetch_calls for pk in batch]

    def _text_for(self, pk):
        if pk in self.texts:
            return self.texts[pk]
        if pk in self.stale and not (self.stale_heals and pk in self._repushed):
            return "STALE_CONTENT"
        return f"t{pk}"                        # matches _mk_chunk's chunk_text → no drift

    def fetch(self, req):
        pks = [int(x) for x in req.ids]
        self.fetch_calls.append(pks)
        assert len(pks) <= 100, "fetch 单批上限 100"
        if self.fetch_raise & set(pks):
            raise RuntimeError("simulated fetch batch failure")
        items = []
        for pk in pks:
            self._reads[pk] = self._reads.get(pk, 0) + 1
            lagging = pk in self.lag_first and self._reads[pk] == 1
            if pk not in self.present or lagging:
                continue                        # 不返回 = 该 pk MISSING
            rid = (pk + 100000) if pk in self.wrong_id else pk   # 越集 → 整批 UNKNOWN
            items.append({"id": rid, "fields": {"chunk_id": f"c{pk}", "doc_id": "doc1",
                                                "chunk_text_store": self._text_for(pk)}})
        return _resp(items)

    def push_documents(self, table, pk_field, request):
        pks = [int(d["fields"][pk_field]) for d in request.body]
        self.push_calls.append(pks)
        for pk in pks:
            self._repushed.add(pk)         # re-push heals stale content (if stale_heals)
            if pk not in self.never_heal:
                self.present.add(pk)
        return types.SimpleNamespace(status_code=200, body={})


class FakeCursor:
    def __init__(self, rowcount_override=None):
        self.executed = []
        self.rowcount = 0
        self._override = rowcount_override

    def execute(self, sql, params=None):
        self.executed.append((sql, list(params) if params else []))
        # 真实 UPDATE（G9 后）: params = [max_retries, code, msg, id1, ...] → 改动行 = len(ids)；
        # 旧 SQL（迁移 019 前）: params = [code, msg, id1, ...]。按 SQL 是否含重试列取前缀数。
        if self._override is not None:
            self.rowcount = self._override
        elif params and "UPDATE" in sql.upper():
            _prefix = 3 if "index_retry_count" in sql else 2
            self.rowcount = max(0, len(params) - _prefix)
        else:
            self.rowcount = 0

    def fetchall(self):
        return []  # G9 DEAD-detection SELECT：假 cursor 无死信行

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rowcount_override=None):
        self.cur = FakeCursor(rowcount_override=rowcount_override)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _mk_chunk(rds_id, idx=0, doc_id="doc1", version_no=2, status="INDEXED"):
    return Chunk(
        chunk_id=f"c{rds_id}", doc_id=doc_id, version_no=version_no,
        chunk_index=idx, chunk_type="text_chunk", chunk_text=f"t{rds_id}",
        token_count=2, embedding_vector=[0.1] * 4, rds_id=rds_id, index_status=status,
    )


def _setup(monkeypatch, client, chunks, conn=None, **env):
    """打补丁 + 返回 ctx（不调用 node，便于 raise 用例事后查 ctx）。"""
    monkeypatch.setenv("RAG_STAGE3_PARITY_VERIFY", "true")
    monkeypatch.setenv("RAG_STAGE3_PARITY_SETTLE_SEC", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setattr(pn, "_get_opensearch_client", lambda ctx=None: client)
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: conn if conn is not None else FakeConn())
    import opensearch_pipeline.env_guard as eg
    monkeypatch.setattr(eg, "assert_destructive_write_allowed", lambda *a, **k: None)
    monkeypatch.setattr(pn.time, "sleep", lambda *a, **k: None)
    return {"bulk_batches": [{"chunks": chunks, "job_id": "J", "oss_key": ""}],
            "simulate_opensearch": False}


# ── 1. simulate / MOCK no-op ─────────────────────────────────────────────────
def test_simulate_is_noop(monkeypatch):
    monkeypatch.setenv("RAG_STAGE3_PARITY_VERIFY", "true")
    called = {"client": 0}
    monkeypatch.setattr(pn, "_get_opensearch_client", lambda ctx=None: called.__setitem__("client", called["client"] + 1) or "X")
    ctx = {"bulk_batches": [{"chunks": [_mk_chunk(1)]}], "simulate_opensearch": True}
    pn.node_verify_and_repush(ctx)            # 不应抛异常
    assert called["client"] == 0              # simulate 早返回，根本没取 client


def test_mock_client_is_noop(monkeypatch):
    monkeypatch.setenv("RAG_STAGE3_PARITY_VERIFY", "true")
    monkeypatch.setattr(pn, "_get_opensearch_client", lambda ctx=None: "MOCK_HA3_CLIENT")
    ctx = {"bulk_batches": [{"chunks": [_mk_chunk(1)]}], "simulate_opensearch": False}
    pn.node_verify_and_repush(ctx)            # MOCK → no-op，不抛异常


# ── 2. flag OFF ──────────────────────────────────────────────────────────────
def test_flag_explicit_off_returns_even_in_real_mode(monkeypatch):
    # F-12: 默认已翻为常开（opt-out），显式设 0/false/no/off 才 no-op。
    monkeypatch.setenv("RAG_STAGE3_PARITY_VERIFY", "0")
    client = FakeHA3(present={1})
    monkeypatch.setattr(pn, "_get_opensearch_client", lambda ctx=None: client)
    ctx = {"bulk_batches": [{"chunks": [_mk_chunk(1)]}], "simulate_opensearch": False}
    pn.node_verify_and_repush(ctx)
    assert client.fetch_calls == [] and client.push_calls == []


def test_flag_default_on_runs_verify_in_real_mode(monkeypatch):
    # F-12: 未设 flag（默认常开）→ 真实模式下必须真正跑校验（点读），堵住不经 DataWorks
    # stage3_node（笔记本重灌等路径）的 96 例类静默丢失复发面。
    monkeypatch.delenv("RAG_STAGE3_PARITY_VERIFY", raising=False)
    monkeypatch.setenv("RAG_STAGE3_PARITY_SETTLE_SEC", "0")
    client = FakeHA3(present={1})
    monkeypatch.setattr(pn, "_get_opensearch_client", lambda ctx=None: client)
    ctx = {"bulk_batches": [{"chunks": [_mk_chunk(1)]}], "simulate_opensearch": False}
    pn.node_verify_and_repush(ctx)
    assert client.fetch_calls != []  # 默认常开 → 真的读了 HA3(官方 fetch)


# ── 3. all present ───────────────────────────────────────────────────────────
def test_all_present_no_repush_no_raise(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11), _mk_chunk(12)]
    client = FakeHA3(present={10, 11, 12})
    ctx = _setup(monkeypatch, client, chunks)
    pn.node_verify_and_repush(ctx)
    assert sorted(client.fetched_pks) == [10, 11, 12]
    assert client.push_calls == []


# ── 4. N missing → healed + in-memory restored ───────────────────────────────
def test_missing_then_healed(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11), _mk_chunk(12)]
    client = FakeHA3(present={10, 12})          # 11 dropped
    ctx = _setup(monkeypatch, client, chunks)
    pn.node_verify_and_repush(ctx)              # 不抛异常
    assert client.push_calls == [[11]]          # 只补推丢失的 11
    healed = chunks[1]
    assert healed.rds_id == 11
    assert healed.index_status == "INDEXED"
    assert healed.index_error_code is None


# ── 5. confirmed-DROP stays missing → raise + RDS committed ───────────────────
def test_persistent_drop_raises_and_persists(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10}, never_heal={11})
    conn = FakeConn()
    ctx = _setup(monkeypatch, client, chunks, conn=conn, RAG_STAGE3_PARITY_MAX_RETRIES=2)
    with pytest.raises(RuntimeError, match="parity"):
        pn.node_verify_and_repush(ctx)
    assert client.push_calls == [[11], [11]]    # 有界补推 2 次
    assert chunks[1].index_status == "FAILED"
    assert chunks[1].index_error_code == "PARITY_DROP"
    assert conn.committed and not conn.rolled_back
    upd = [e for e in conn.cur.executed if "UPDATE chunk_meta" in e[0]]
    assert len(upd) == 1 and upd[0][1][1] == "PARITY_DROP"   # params=[max_retries, code, msg, *ids] (G9)
    assert ("doc1", 2) in ctx["failed_doc_versions"]


# ── 6. UNKNOWN（fetch 批异常）→ raise, distinct code, NO re-push ───────────────
def test_unknown_blocks_without_repush(monkeypatch):
    """fetch 的失败粒度是**批**：批内任一 PK 读失败 ⇒ 整批 UNKNOWN。
    这是有意的保守方向——读不到就绝不敢断言"缺失"，宁可整批阻断节点 05。"""
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11}, fetch_raise={11})
    conn = FakeConn()
    ctx = _setup(monkeypatch, client, chunks, conn=conn, RAG_STAGE3_PARITY_MAX_RETRIES=2)
    with pytest.raises(RuntimeError, match="parity"):
        pn.node_verify_and_repush(ctx)
    assert client.push_calls == []              # UNKNOWN 不补推
    for c in chunks:                            # 同批两条都进 UNKNOWN 桶
        assert c.index_status == "FAILED"
        assert c.index_error_code == "PARITY_UNKNOWN"
    assert conn.committed
    assert ("doc1", 2) in ctx["failed_doc_versions"]


# ── 7. fetch 返回请求集外 PK（畸形）→ 整批 UNKNOWN，**不是** DROP ───────────────
def test_pk_mismatch_is_batch_unknown_not_drop(monkeypatch):
    """畸形响应不能被读成"这些行不存在"。旧零向量 point-read 把 PK 不符判 MISSING→DROP；
    fetch 口径下它是"这批响应不可信"⇒ 整批 UNKNOWN + 零补推 + 不计重试预算。"""
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11}, wrong_id={11})
    conn = FakeConn()
    ctx = _setup(monkeypatch, client, chunks, conn=conn, RAG_STAGE3_PARITY_MAX_RETRIES=2)
    with pytest.raises(RuntimeError, match="parity"):
        pn.node_verify_and_repush(ctx)
    assert client.push_calls == []
    for c in chunks:
        assert c.index_error_code == "PARITY_UNKNOWN"
    upd = [e for e in conn.cur.executed if "UPDATE chunk_meta" in e[0]]
    assert all("index_retry_count" not in sql for sql, _ in upd), \
        "畸形响应属「无法确认」，绝不消耗重试预算"


# ── 9. eventual-consistency: lag on first read, present on re-confirm ─────────
def test_eventual_consistency_heals_without_persistent(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    # 11 真在 HA3，但首次 point-read 返回空（滞后）→ 误判 missing → 补推 → 复确认 present
    client = FakeHA3(present={10, 11}, lag_first={11})
    ctx = _setup(monkeypatch, client, chunks, RAG_STAGE3_PARITY_MAX_RETRIES=2)
    pn.node_verify_and_repush(ctx)              # 不抛异常（最终一致，非真丢失）
    assert chunks[1].index_status == "INDEXED"
    assert client.push_calls == [[11]]          # 只补推 1 次即愈合


# ── 10. 全部已知 PK 直接 fetch：不再有 id-range enum-hint 分支 ────────────────
#    （原「小批跳过 enum」「enum 失败降级 point-read」两用例随该分支删除而移除——
#      被测代码已不存在，留着只会空转假绿。enum 本身的健康/失败语义由
#      tests/test_ha3_reconcile.py 与 tests/test_ha3_verify.py 覆盖。）
def test_no_enumerate_hint_all_pks_go_through_fetch(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11})
    called = {"enum": 0}
    import opensearch_pipeline.ha3_reconcile as rc
    monkeypatch.setattr(rc, "_enumerate_ha3_pks",
                        lambda *a, **k: called.__setitem__("enum", called["enum"] + 1) or {})
    ctx = _setup(monkeypatch, client, chunks)
    pn.node_verify_and_repush(ctx)
    assert called["enum"] == 0, "stage-3 不再走 enum-hint"
    assert sorted(client.fetched_pks) == [10, 11], "全部 expected 都过 fetch"


# ── 11. 批次切分：>100 PK 分多批，且排序后分批（归因确定）──────────────────────
def test_fetch_batches_at_100_sorted(monkeypatch):
    pks = list(range(10, 215))                   # 205 个 → 3 批（100/100/5）
    chunks = [_mk_chunk(pk) for pk in pks]
    client = FakeHA3(present=set(pks))
    ctx = _setup(monkeypatch, client, chunks)
    pn.node_verify_and_repush(ctx)
    assert [len(b) for b in client.fetch_calls] == [100, 100, 5]
    assert client.fetched_pks == pks             # 排序分批，顺序确定
    assert client.push_calls == []


# ── 12. DROP + UNKNOWN coexist → two separate UPDATEs, distinct codes ─────────
def test_drop_and_unknown_coexist_two_updates(monkeypatch):
    # 批语义下二者共存需跨批：批0 = 10..109（含 never_heal=11 → DROP），
    # 批1 = 110..111（fetch_raise 命中 → 整批 UNKNOWN）。
    pks = list(range(10, 112))                   # 102 个 → 2 批
    chunks = [_mk_chunk(pk) for pk in pks]
    present = set(pks) - {11}
    client = FakeHA3(present=present, never_heal={11}, fetch_raise={110})
    conn = FakeConn()
    ctx = _setup(monkeypatch, client, chunks, conn=conn, RAG_STAGE3_PARITY_MAX_RETRIES=1)
    with pytest.raises(RuntimeError, match="parity"):
        pn.node_verify_and_repush(ctx)
    upd = [e for e in conn.cur.executed if "UPDATE chunk_meta" in e[0]]
    assert len(upd) == 2                        # 两组分开写，保留故障分类
    # 两组的 params 形状【有意不同】(A4 2026-07-25)：
    #   DROP  = 确认性失败 → 消耗重试预算，params=[max_retries, code, msg, *ids] (G9)
    #   UNKNOWN = 读不到、无法确认 → 不计数，params=[code, msg, *ids]
    drop_sql, drop_params = upd[0]
    unk_sql, unk_params = upd[1]
    assert "index_retry_count = index_retry_count + 1" in drop_sql
    # params[0] 是 chunk 级重试上限 RAG_STAGE3_CHUNK_MAX_RETRIES（≠ 本用例设的 parity 补推次数）
    assert drop_params[0] == pn._stage3_chunk_max_retries()
    assert drop_params[1] == "PARITY_DROP"
    assert "index_retry_count" not in unk_sql, "读故障不得把健康 chunk 推向 DEAD"
    assert unk_params[0] == "PARITY_UNKNOWN"
    assert conn.committed
    by_pk = {c.rds_id: c for c in chunks}
    assert by_pk[11].index_error_code == "PARITY_DROP"          # 批0 内的真丢失
    for pk in (110, 111):                                       # 批1 整批 UNKNOWN
        assert by_pk[pk].index_error_code == "PARITY_UNKNOWN"
    # 同批健康行不受牵连（健康路径根本不设该属性）
    assert getattr(by_pk[10], "index_error_code", None) is None


# ── 13. rowcount mismatch → rollback + raise, no commit ──────────────────────
def test_rowcount_mismatch_rolls_back(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10}, never_heal={11})
    conn = FakeConn(rowcount_override=0)         # UPDATE 改 0 行 ≠ 1 → 闭环失败
    ctx = _setup(monkeypatch, client, chunks, conn=conn, RAG_STAGE3_PARITY_MAX_RETRIES=0)
    with pytest.raises(RuntimeError, match="state-persistence"):
        pn.node_verify_and_repush(ctx)
    assert conn.rolled_back and not conn.committed


# ── 14. empty / all-non-INDEXED batch → return, no HA3 calls ─────────────────
def test_empty_batch_returns(monkeypatch):
    client = FakeHA3()
    ctx = _setup(monkeypatch, client, [])       # 空批
    pn.node_verify_and_repush(ctx)
    assert client.fetch_calls == [] and client.push_calls == []


def test_all_non_indexed_returns(monkeypatch):
    chunks = [_mk_chunk(10, status="FAILED"), _mk_chunk(11, status="NOT_INDEXED")]
    client = FakeHA3()
    ctx = _setup(monkeypatch, client, chunks)   # 无 INDEXED → expected 空
    pn.node_verify_and_repush(ctx)
    assert client.fetch_calls == [] and client.push_calls == []


# ── 15. DAG wiring guard ─────────────────────────────────────────────────────
def test_dag3_wiring_04b_before_05():
    from opensearch_pipeline.dag_definitions import build_dag3_chunk_to_opensearch
    dag = build_dag3_chunk_to_opensearch()
    assert "04b" in dag.nodes
    assert dag.nodes["04b"].depends_on == ["04"]
    assert dag.nodes["05"].depends_on == ["04b"]


# ── 16-20. content-drift sub-check (RAG_STAGE3_PARITY_DRIFT) ──────────────────
def test_drift_detected_then_healed(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11}, stale={11}, stale_heals=True)  # 11 present but stale content
    ctx = _setup(monkeypatch, client, chunks,
                 RAG_STAGE3_PARITY_DRIFT="true", RAG_STAGE3_PARITY_MAX_RETRIES=2)
    pn.node_verify_and_repush(ctx)              # drift re-pushed → content matches → no raise
    assert client.push_calls == [[11]]
    assert chunks[1].index_status == "INDEXED"


def test_drift_persistent_raises_parity_drift(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11}, stale={11}, stale_heals=False)  # never reconciles
    conn = FakeConn()
    ctx = _setup(monkeypatch, client, chunks, conn=conn,
                 RAG_STAGE3_PARITY_DRIFT="true", RAG_STAGE3_PARITY_MAX_RETRIES=1)
    with pytest.raises(RuntimeError, match="parity"):
        pn.node_verify_and_repush(ctx)
    assert chunks[1].index_error_code == "PARITY_DRIFT"
    upd = [e for e in conn.cur.executed if "UPDATE chunk_meta" in e[0]]
    assert any(e[1][1] == "PARITY_DRIFT" for e in upd)   # params=[max_retries, code, msg, *ids] (G9)
    assert conn.committed
    assert ("doc1", 2) in ctx["failed_doc_versions"]


def test_drift_flag_off_no_check(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11}, stale={11}, stale_heals=False)  # content differs...
    ctx = _setup(monkeypatch, client, chunks)  # ...but RAG_STAGE3_PARITY_DRIFT unset
    pn.node_verify_and_repush(ctx)             # presence-only → no drift check → no raise
    assert client.push_calls == []             # nothing re-pushed


def test_drift_matching_content_no_drift(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11})          # returns t{pk} == in-memory chunk_text
    ctx = _setup(monkeypatch, client, chunks, RAG_STAGE3_PARITY_DRIFT="true")
    pn.node_verify_and_repush(ctx)             # no drift, no raise
    assert client.push_calls == []


def test_drift_failopen_on_unreadable_text(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    # 11 present but returns no chunk_text_store (None) → drift skipped (fail-open), no raise
    client = FakeHA3(present={10, 11}, texts={11: None})
    ctx = _setup(monkeypatch, client, chunks, RAG_STAGE3_PARITY_DRIFT="true")
    pn.node_verify_and_repush(ctx)
    assert client.push_calls == []


# ── C2 回归（2026-07-22）：零向量伪影不再能把在场行判缺 + 固定等待 ───────────────

def test_fetch_present_but_zero_vector_invisible_is_present(monkeypatch):
    """**本次改造的核心回归**：行在 HA3(fetch 可取回)但零向量 query 看不见(枚举盲区)。

    旧实现走零向量 point-read ⇒ 判 missing ⇒ 无谓补推 ⇒ 未愈合则 04b raise ⇒ 节点 05
    永不执行 ⇒ 旧版本永不停用、双版本长存。新实现走官方 fetch ⇒ PRESENT ⇒ 零补推、不抛。
    FakeHA3 刻意不实现 `query`：一旦回退零向量会立刻 AttributeError。"""
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11})
    assert not hasattr(client, "query"), "桩不得提供 query——存在性路径回退即失败"
    ctx = _setup(monkeypatch, client, chunks)
    pn.node_verify_and_repush(ctx)
    assert sorted(client.fetched_pks) == [10, 11]
    assert client.push_calls == []
    for c in chunks:
        assert c.index_status == "INDEXED"


def test_settle_early_exits_when_fetch_sees_probe(monkeypatch):
    """F#56 早退已解禁（2026-07-30 生产实测：push 返回时 fetch/query/倒排三平面均已可见，
    平面间差值 1-2ms ≪ 分辨率下界 0.58s ⇒ 无可测跨平面窗口）。探针与权威判据同用 fetch。
    断言=探针可见即刻返回，一次 sleep 都不发生。"""
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11})          # 探针 pk=max=11 立即可见
    ctx = _setup(monkeypatch, client, chunks, RAG_STAGE3_PARITY_SETTLE_SEC=7)
    slept = []                                  # 必须在 _setup 之后：它自带 sleep no-op 桩
    monkeypatch.setattr(pn.time, "sleep", lambda s: slept.append(s))
    pn.node_verify_and_repush(ctx)
    assert slept == [], "探针已可见就不该睡"
    assert 11 in client.fetched_pks, "settle 探针必须走 fetch（与权威同平面）"


def test_settle_falls_back_to_full_wait_when_never_visible(monkeypatch):
    """探针始终不可见 → 轮询至 settle 上限才放行（上限是最终兜底，不能被早退绕过）。

    用**假时钟**（sleep 推进 time.time）：否则 sleep 被桩掉而真实时钟照走，循环会空转
    整整 settle 秒；假时钟既快又能精确断言轮询节奏。"""
    chunks = [_mk_chunk(10)]
    client = FakeHA3(present=set(), never_heal={10})   # 永不可见
    conn = FakeConn()
    ctx = _setup(monkeypatch, client, chunks, conn=conn,
                 RAG_STAGE3_PARITY_SETTLE_SEC=7, RAG_STAGE3_PARITY_SETTLE_POLL_SEC=3,
                 RAG_STAGE3_PARITY_MAX_RETRIES=1)
    clock = {"t": 1000.0}
    slept = []

    def _sleep(s):
        slept.append(s)
        clock["t"] += s

    monkeypatch.setattr(pn.time, "time", lambda: clock["t"])
    monkeypatch.setattr(pn.time, "sleep", _sleep)
    with pytest.raises(RuntimeError, match="parity"):
        pn.node_verify_and_repush(ctx)
    # 每次 settle：poll=3 → 3+3+1 恰好用满 7s 上限，末次被 remaining 截断
    assert slept[:3] == [3, 3, 1]
    assert sum(slept[:3]) == 7, "轮询总时长不得超过 settle 上限"


def test_settle_zero_skips_wait(monkeypatch):
    chunks = [_mk_chunk(10)]
    client = FakeHA3(present={10})
    ctx = _setup(monkeypatch, client, chunks, RAG_STAGE3_PARITY_SETTLE_SEC=0)
    slept = []                                  # 同上：须在 _setup 之后打桩
    monkeypatch.setattr(pn.time, "sleep", lambda s: slept.append(s))
    pn.node_verify_and_repush(ctx)
    assert slept == []
