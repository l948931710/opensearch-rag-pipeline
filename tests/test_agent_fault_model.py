# -*- coding: utf-8 -*-
"""
test_agent_fault_model.py — 外审核查修复批次1（2026-07-16）故障注入回归

把外部 production 评审的 5 个故障注入（核查台账
docs/audits/enterprise_agent_review_verification_2026-07-16.md）固化成 blocking tests：
- P0-01 loop 生成器未发终态事件即耗尽 → durable 终态 == 客户端终态（不再留 zombie running）；
- P0-02 drain 与 submit/resume 交棒竞态 → 交棒失败诚实落 failed，drain 等 _active 归零；
- P0-03 commit ACK 丢失 → store 层 read-after-write 消歧（已提交按成功返回）；
- P0-04 拒绝终止 CAS 失败 → 不编造终态帧（durable 已 cancelled 才宣告）；
- P1-01 事件队列 maxsize=1 → clamp ≥2，终态帧不被收尾哨兵挤掉。

追加（staging 灰度 2026-07-17）：完成事务 1213/1205 锁竞争单次全新事务重放——
恰重放一次、整段重走（非单句）、每次尝试各自 _begin 钉连接、重试输家 CAS False、
commit 阶段异常不进重放面（仍归 P0-03 消歧）。
"""
import threading
import time

from pymysql.err import OperationalError

import pytest

from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import (
    ModelDelta, RunCompleted, RunFailed, ToolCallProposed)
from opensearch_pipeline.agent_runtime.executor import (
    RunHandle, RunRejected, ThreadedRunExecutor)
from opensearch_pipeline.agent_runtime.loop import DefaultAgentLoop, ModelTurn
from opensearch_pipeline.agent_runtime.tool import ToolResult


class _FaultStore:
    """协议完整但可注入故障的假 store（形状对齐 test_agent_runtime_executor 的 _FakeStore）。"""

    def __init__(self):
        self.transitions = []
        self._n = 0

    def create_run(self, ctx, profile):
        self._n += 1
        return f"run{self._n}"

    def append_step(self, run_id, step):
        return 1

    def transition(self, run_id, frm, to):
        self.transitions.append((run_id, frm, to))
        return True

    def consume_budget(self, run_id, *, turns=0, tool_calls=0, tokens=0):
        return {}

    def load_latest_checkpoint(self, run_id):
        return None


def _ctx():
    return ExecutionContext.create(
        request_id="r", user_id="u", acl_groups=["g"], roles=["employee"],
        channel="console", thread_id="t", budget=RunBudget(max_turns=8))


def _scripted(turns):
    box = {"i": 0}

    def _fn(msgs, tools):
        t = turns[box["i"]]
        box["i"] += 1
        return t

    return _fn


def _spy_frames(monkeypatch):
    """旁路捕获 RunHandle 的对外帧与收尾（评审 test-quality 缺口：resume 抛异常的路径上
    handle 不可达，「不编造终态帧 / 交棒失败仍发终态」这半个不变量必须经 spy 观测）。"""
    frames, finishes = [], []
    orig_emit, orig_finish = RunHandle._emit, RunHandle._finish

    def spy_emit(self, ev):
        frames.append((self.run_id, ev))
        return orig_emit(self, ev)

    def spy_finish(self, *, end_relay=True):
        finishes.append((self.run_id, end_relay))
        return orig_finish(self, end_relay=end_relay)

    monkeypatch.setattr(RunHandle, "_emit", spy_emit)
    monkeypatch.setattr(RunHandle, "_finish", spy_finish)
    return frames, finishes


class _BareLoop:
    """协议违约 loop：可选先发若干非终态帧，然后**不发终态事件**直接 return（P0-01 注入面）。"""

    def __init__(self, pre_events=()):
        self._pre = list(pre_events)

    def run(self, ctx, messages, tools):
        for ev in self._pre:
            yield ev
        return


class _BareAfterToolLoop:
    """工具结果回注后裸 return（P0-01 第三个注入点：send() 之后耗尽）。"""

    def run(self, ctx, messages, tools):
        result = yield ToolCallProposed(call_id="c1", tool_name="knowledge_search",
                                        arguments={"query": "x"}, turn_index=0)
        assert result is not None
        return


# ── P0-01：生成器未发终态即耗尽 ──────────────────────────────────────────────


@pytest.mark.parametrize("loop", [
    _BareLoop(),                                        # 首帧前裸 return
    _BareLoop(pre_events=[ModelDelta(text="部分输出")]),   # 非终态帧后裸 return
    _BareAfterToolLoop(),                               # 工具结果回注后裸 return
], ids=["before-first-frame", "after-delta", "after-tool-result"])
def test_p0_01_bare_return_lands_failed_terminal(loop):
    """裸 return 的每个注入点：客户端必见终态帧、durable 必落 failed、容量/句柄归零。"""
    store = _FaultStore()
    fails = []
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    h = ex.submit(_ctx(), loop, [{"role": "user", "content": "q"}], [],
                  on_failure=lambda e: fails.append(e))
    events = list(h.events())
    ex.shutdown()
    terminal = [e for e in events if isinstance(e, RunFailed)]
    assert terminal and terminal[-1].retryable is True          # 客户端看到诚实终态
    assert ("run1", "running", "failed") in store.transitions   # durable 终态==客户端终态
    assert not any(isinstance(e, RunCompleted) for e in events)
    assert ex.active_count() == 0
    assert ex.get_live_handle("run1") is None
    assert fails                                                # CAS 成立 → 失败侧回调照跑


def test_p0_01_bare_return_ownership_lost_skips_failure_callback():
    """D3 fencing 对齐：裸 return 收尾时 CAS 失败（收尸/取消抢先）→ 终态帧照发，
    失败侧回调**不跑**（绝不在他方已持有终态后再写失败侧落库）。"""

    class _CasLostStore(_FaultStore):
        def transition(self, run_id, frm, to):
            super().transition(run_id, frm, to)
            return False

    store = _CasLostStore()
    fails = []
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    h = ex.submit(_ctx(), _BareLoop(), [], [], on_failure=lambda e: fails.append(e))
    events = list(h.events())
    ex.shutdown()
    assert any(isinstance(e, RunFailed) for e in events)
    assert fails == []


# ── P0-02：drain 与 submit/resume 交棒竞态 ──────────────────────────────────


def test_p0_02_submit_dispatch_failure_marks_failed():
    """池已关（drain 竞态的交棒侧）→ pool.submit 抛：run 行必须诚实 running→failed，
    不再留「durable running、无人持有」的孤儿等 reaper。"""
    store = _FaultStore()
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    ex._pool.shutdown(wait=True)                     # 注入：drain 已 shutdown 线程池
    with pytest.raises(RuntimeError):
        ex.submit(_ctx(), DefaultAgentLoop(_scripted([ModelTurn(text="x")])), [], [])
    assert ("run1", "running", "failed") in store.transitions
    assert ex.active_count() == 0
    assert ex.get_live_handle("run1") is None


def test_p0_02_resume_dispatch_failure_marks_failed(monkeypatch):
    """resuming→running 已接手后交棒失败：不回边 suspended（非合法边），诚实落 failed，
    且 handle 必须发终态帧+收流（中继消费者不空等——评审 mutation 缺口，经 spy 观测）。"""
    from opensearch_pipeline.agent_runtime.approval import Approved

    frames, finishes = _spy_frames(monkeypatch)
    store = _FaultStore()
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    ex._pool.shutdown(wait=True)
    with pytest.raises(RuntimeError):
        ex.resume("run9", _ctx(), Approved(),
                  DefaultAgentLoop(_scripted([ModelTurn(text="x")])), [])
    assert ("run9", "suspended", "resuming") in store.transitions
    assert ("run9", "resuming", "running") in store.transitions
    assert ("run9", "running", "failed") in store.transitions
    assert ("run9", "resuming", "suspended") not in store.transitions   # 已交棒，非回边
    assert any(r == "run9" and isinstance(ev, RunFailed) and "交棒失败" in ev.error
               for r, ev in frames)                                     # 终态帧照发
    assert any(r == "run9" for r, _ in finishes)                        # 流已收尾
    assert ex.active_count() == 0


def test_p0_02_submit_raise_after_enqueue_does_not_false_fail():
    """复查修正（评审 concurrency P2）：pool.submit 在**入队后**才抛（线程创建失败类）时
    条目仍会被 warm worker 驱动——绝不反标 failed（误标会让 fenced 驱动器作废真答案）。"""
    store = _FaultStore()
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    orig_submit = ex._pool.submit

    def enqueue_then_raise(fn, *a, **kw):
        orig_submit(fn, *a, **kw)               # 条目真实入队（将被驱动）
        raise RuntimeError("can't start new thread")

    ex._pool.submit = enqueue_then_raise        # _shutdown/_broken 均 False → 可能已入队
    with pytest.raises(RuntimeError, match="can't start new thread"):
        ex.submit(_ctx(), DefaultAgentLoop(_scripted([ModelTurn(text="x")])), [], [])
    deadline = time.monotonic() + 5
    while (time.monotonic() < deadline
           and ("run1", "running", "succeeded") not in store.transitions):
        time.sleep(0.02)
    assert ("run1", "running", "succeeded") in store.transitions   # warm worker 驱动到完成
    assert ("run1", "running", "failed") not in store.transitions  # 绝不反标在跑 run
    ex.shutdown()


def test_p0_02_drain_zero_timeout_mid_submit_no_orphan():
    """drain(timeout=0) 恰夹在 create_run 与交棒之间（评审原始注入）：交棒撞已关的池 →
    submit 抛且 run 落 failed——决不允许 durable 停 running 而 active=0、live={}。"""
    entered, release = threading.Event(), threading.Event()

    class _BlockingStore(_FaultStore):
        def create_run(self, ctx, profile):
            entered.set()
            assert release.wait(5)
            return super().create_run(ctx, profile)

    store = _BlockingStore()
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    errs = []

    def _submit():
        try:
            ex.submit(_ctx(), DefaultAgentLoop(_scripted([ModelTurn(text="x")])), [], [])
        except Exception as e:   # noqa: BLE001
            errs.append(e)

    t = threading.Thread(target=_submit)
    t.start()
    assert entered.wait(5)                            # submit 已 admitted、卡在 create_run
    ex.drain(timeout=0)                               # 空 _live 即刻关池（极端窗口）
    release.set()
    t.join(5)
    assert errs                                       # 交棒失败如实上抛（路由侧可见）
    assert ("run1", "running", "failed") in store.transitions   # 孤儿被诚实收口
    assert ex.active_count() == 0


def test_p0_02_drain_waits_for_admitted_unregistered_run():
    """带预算的 drain 必须等 admitted-but-unregistered 的 run（_active 计数）而不只 _live
    快照：窗口内的 run 正常交棒、跑完、succeeded，而不是撞上已关的池。"""
    entered, release = threading.Event(), threading.Event()

    class _BlockingStore(_FaultStore):
        def create_run(self, ctx, profile):
            entered.set()
            assert release.wait(5)
            return super().create_run(ctx, profile)

    store = _BlockingStore()
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    errs = []

    def _submit():
        try:
            ex.submit(_ctx(), DefaultAgentLoop(_scripted([ModelTurn(text="x")])), [], [])
        except Exception as e:   # noqa: BLE001
            errs.append(e)

    t = threading.Thread(target=_submit)
    t.start()
    assert entered.wait(5)
    box = {}
    dt = threading.Thread(target=lambda: box.setdefault("report", ex.drain(timeout=10)))
    dt.start()
    time.sleep(0.15)                                  # drain 进入 _active 等待段
    release.set()
    dt.join(10)
    t.join(10)
    assert not errs
    assert ("run1", "running", "succeeded") in store.transitions
    assert ex.active_count() == 0


# ── P0-03：commit ACK 丢失（两将军）→ read-after-write 消歧 ─────────────────


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._row = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self._conn.select_exc is not None:
            raise self._conn.select_exc
        if sql.strip().lower().startswith("select status"):
            self._row = (self._conn.status_answer,)
        else:
            self.rowcount = 1

    def fetchone(self):
        return self._row


class _FakeConn:
    """pymysql 形状替身：commit_exc 注入「服务端已提交、ACK 丢失」（rollback 也已死）。"""

    def __init__(self, *, commit_exc=None, status_answer="running", select_exc=None):
        self.commit_exc = commit_exc
        self.status_answer = status_answer
        self.select_exc = select_exc

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        if self.commit_exc is not None:
            raise self.commit_exc

    def rollback(self):
        if self.commit_exc is not None:
            raise RuntimeError("rollback on dead connection")

    def close(self):
        pass


def _patch_conns(monkeypatch, conns):
    """假连接序列**只服务测试线程**：同进程遗留的后台线程（路由测试起的 reaper 等）
    也走 db._get_db_conn，不设防会偷走序列里的连接、打乱主线程的 conn 顺序（batch 内
    偶发红、单跑绿的典型形态）。后台线程拿到的异常由其自身 fail-open 路径消化。"""
    seq = list(conns)
    owner = threading.get_ident()

    def _factory():
        if threading.get_ident() != owner:
            raise RuntimeError("fake conn: 非测试线程的后台 DB 访问（有意拒绝）")
        return seq.pop(0)

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _factory)


def test_p0_03_commit_ack_loss_read_back_confirms_success(monkeypatch):
    """commit 抛但事务实际已提交：新连接读到 succeeded → 返回 True（客户端照常收 done），
    不再对外宣告 answer_persist_failed 制造客户端/durable 真假分裂。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    _patch_conns(monkeypatch, [
        _FakeConn(commit_exc=OSError("(2013, 'Lost connection during commit ACK')"),
                  status_answer="running"),            # 主连接：CAS 读 running、commit 断
        _FakeConn(status_answer="succeeded"),          # 消歧连接：证实已提交
    ])
    assert RDSRunStore().complete_run_atomic("r1") is True


def test_p0_03_commit_ack_loss_read_back_not_applied_reraises(monkeypatch):
    """commit 抛且新连接读到仍 running（事务确实没生效）→ 原异常上抛，维持保守失败语义
    （消歧读对 running 有界重试 3 次——慢收尾窗口——之后定论）。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    boom = OSError("(2013, 'Lost connection during commit ACK')")
    _patch_conns(monkeypatch, [
        _FakeConn(commit_exc=boom, status_answer="running"),
        _FakeConn(status_answer="running"),
        _FakeConn(status_answer="running"),
        _FakeConn(status_answer="running"),
    ])
    with pytest.raises(OSError) as exc_info:
        RDSRunStore().complete_run_atomic("r1")
    assert exc_info.value is boom                      # 消歧读绝不掩盖原始 commit 异常


def test_p0_03_commit_ack_loss_slow_commit_confirmed_on_retry(monkeypatch):
    """复查修正（评审 transaction P2）：commit 慢收尾——首次消歧读仍 running、
    重试读到 succeeded → 按成功返回，不把已成功的答案误报 failed。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    _patch_conns(monkeypatch, [
        _FakeConn(commit_exc=OSError("read timeout on slow commit"), status_answer="running"),
        _FakeConn(status_answer="running"),            # 第 1 读：commit 仍在服务端收尾
        _FakeConn(status_answer="succeeded"),          # 第 2 读：已落地
    ])
    assert RDSRunStore().complete_run_atomic("r1") is True


def test_p0_03_commit_ack_loss_verify_read_failure_reraises(monkeypatch):
    """commit 抛且消歧读也失败（结果真未知）→ 保守：原异常上抛，绝不谎报成功。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    boom = OSError("(2013, 'Lost connection during commit ACK')")
    _patch_conns(monkeypatch, [
        _FakeConn(commit_exc=boom, status_answer="running"),
        _FakeConn(select_exc=RuntimeError("verify read down")),
        _FakeConn(select_exc=RuntimeError("verify read down")),
        _FakeConn(select_exc=RuntimeError("verify read down")),
    ])
    with pytest.raises(OSError):
        RDSRunStore().complete_run_atomic("r1")


def test_p0_03_transactional_methods_pin_transaction(monkeypatch):
    """复查修正（评审 transaction P1）：池化 SteadyDB 连接不 begin() 会在断连时透明重连+
    单句重试，把多语句事务劈成两半（status=succeeded 落地而答案行丢失）——事务性方法
    必须显式 begin() 圈定事务边界。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    class _BeginConn(_FakeConn):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.begun = 0

        def begin(self):
            self.begun += 1

    c = _BeginConn(status_answer="running")
    _patch_conns(monkeypatch, [c])
    assert RDSRunStore().complete_run_atomic("r1") is True
    assert c.begun == 1


def test_p0_03_precommit_failure_unchanged(monkeypatch):
    """commit 前异常（extra_writer 抛）语义不变：回滚后原样上抛，无消歧读。
    （兼作锁重放的负向门：非锁异常零重试——序列仅 1 条连接，误重放即 IndexError 现形。）"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    _patch_conns(monkeypatch, [_FakeConn(status_answer="running")])
    with pytest.raises(RuntimeError, match="qa 行写失败"):
        RDSRunStore().complete_run_atomic(
            "r1", extra_writer=lambda cur: (_ for _ in ()).throw(RuntimeError("qa 行写失败")))


# ── 完成事务锁竞争单次重放（staging 灰度 2026-07-17：单次 1213 打成 AGENT_ERROR） ──


class _RetryProbeConn(_FakeConn):
    """重放探针：计数 begin/commit/rollback——断言「全新事务重放」而非单句重试
    （每次尝试各自 begin 钉连接；失败尝试只回滚零提交）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.begun = 0
        self.committed = 0
        self.rolled_back = 0

    def begin(self):
        self.begun += 1

    def commit(self):
        super().commit()
        self.committed += 1

    def rollback(self):
        try:
            super().rollback()
        finally:
            self.rolled_back += 1


def _deadlock_exc():
    return OperationalError(
        1213, "Deadlock found when trying to get lock; try restarting transaction")


def test_complete_stmt_deadlock_retries_once_fresh_txn(monkeypatch):
    """语句阶段撞 1213 → 全新事务重放一次成功：首连接零提交纯回滚，第二连接**各自
    begin 钉连接**（SteadyDB 纪律不因重放松动）后整段重走并 commit——用户拿 done，
    不再被单次死锁打成 AGENT_ERROR。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    c1 = _RetryProbeConn(select_exc=_deadlock_exc(), status_answer="running")
    c2 = _RetryProbeConn(status_answer="running")
    _patch_conns(monkeypatch, [c1, c2])
    assert RDSRunStore().complete_run_atomic("r1") is True
    assert (c1.begun, c1.committed) == (1, 0) and c1.rolled_back >= 1
    assert (c2.begun, c2.committed) == (1, 1)


def test_complete_deadlock_in_answer_write_replays_whole_txn(monkeypatch):
    """答案写（extra_writer）内撞 1213——灰度现场形态：重放跑**整段事务**
    （FOR UPDATE→答案写→CAS），writer 被完整重调一次；首次尝试已整体回滚，
    答案行恰落一次。绝非对死语句的单句重试。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    calls = {"n": 0}

    def writer(cur):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _deadlock_exc()
        cur.execute("INSERT INTO qa_session_log (message_id) VALUES ('m1')")

    c1 = _RetryProbeConn(status_answer="running")
    c2 = _RetryProbeConn(status_answer="running")
    _patch_conns(monkeypatch, [c1, c2])
    assert RDSRunStore().complete_run_atomic("r1", extra_writer=writer) is True
    assert calls["n"] == 2
    assert (c1.committed, c2.committed) == (0, 1)


def test_complete_lock_wait_timeout_1205_retries(monkeypatch):
    """1205 锁等待超时与 1213 同窗：语句已回滚+显式 rollback 收干净 → 同样重放一次。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    c1 = _RetryProbeConn(
        select_exc=OperationalError(
            1205, "Lock wait timeout exceeded; try restarting transaction"),
        status_answer="running")
    c2 = _RetryProbeConn(status_answer="running")
    _patch_conns(monkeypatch, [c1, c2])
    assert RDSRunStore().complete_run_atomic("r1") is True
    assert c2.committed == 1


def test_complete_double_deadlock_restores_original_error(monkeypatch):
    """连续两撞：重放**恰一次**（序列仅 2 条连接，第三次取连接会 IndexError 现形），
    还原原始 1213 上抛——executor 沿 P0-01 诚实落 failed，错误面貌与现状一致。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    boom2 = _deadlock_exc()
    _patch_conns(monkeypatch, [
        _RetryProbeConn(select_exc=_deadlock_exc(), status_answer="running"),
        _RetryProbeConn(select_exc=boom2, status_answer="running"),
    ])
    with pytest.raises(OperationalError) as exc_info:
        RDSRunStore().complete_run_atomic("r1")
    assert exc_info.value is boom2
    assert exc_info.value.args[0] == 1213


def test_complete_retry_loser_cas_false_no_write(monkeypatch):
    """重放间隙他方收口（取消/收尸）：重放 FOR UPDATE 读到非 running → False 且零提交
    ——重试输家自然 CAS 失败，沿用完成侧 fencing 语义，绝不覆盖他方终态。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    c1 = _RetryProbeConn(select_exc=_deadlock_exc(), status_answer="running")
    c2 = _RetryProbeConn(status_answer="cancelled")
    _patch_conns(monkeypatch, [c1, c2])
    assert RDSRunStore().complete_run_atomic("r1") is False
    assert c2.committed == 0


def test_complete_commit_phase_deadlock_not_retried(monkeypatch):
    """commit 阶段异常即便带锁码也**不进重放面**——结果未知归 P0-03 消歧（重放会把
    「未知」误判成失去所有权→False→结果作废，比诚实 failed 更糟）。消歧读不出
    succeeded → 原异常上抛；若误重放，序列第 5 条连接会被取用（IndexError 现形）。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    boom = OperationalError(1213, "Deadlock found when trying to get lock (commit)")
    _patch_conns(monkeypatch, [
        _FakeConn(commit_exc=boom, status_answer="running"),
        _FakeConn(status_answer="running"),
        _FakeConn(status_answer="running"),
        _FakeConn(status_answer="running"),
    ])
    with pytest.raises(OperationalError) as exc_info:
        RDSRunStore().complete_run_atomic("r1")
    assert exc_info.value is boom


def test_complete_connection_lost_stmt_phase_not_retried(monkeypatch):
    """语句阶段断连（2013 等 connection-lost 族）不重放：票面仅 1213/1205 两码——
    断连族另有 _begin 钉连接纪律与 P0-03 消歧收口，混入重放面反而引回单句重放风险。
    序列仅 1 条连接，误重放即 IndexError 现形。"""
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    boom = OperationalError(2013, "Lost connection to MySQL server during query")
    _patch_conns(monkeypatch, [_FakeConn(select_exc=boom, status_answer="running")])
    with pytest.raises(OperationalError) as exc_info:
        RDSRunStore().complete_run_atomic("r1")
    assert exc_info.value is boom


# ── P0-04：拒绝终止 CAS 失败不编造终态 ──────────────────────────────────────


class _TerminateStore(_FaultStore):
    """resuming→cancelled CAS 失败注入：reaper 抢先回边（durable=suspended）。"""

    def __init__(self, *, durable_after_cas="suspended"):
        super().__init__()
        self.durable = durable_after_cas

    def transition(self, run_id, frm, to):
        self.transitions.append((run_id, frm, to))
        if (frm, to) == ("resuming", "cancelled"):
            return False                              # 注入：CAS 被并发抢先
        return True

    def get_run(self, run_id):
        return {"run_id": run_id, "status": self.durable}


def test_p0_04_rejected_terminate_cas_false_no_fabricated_terminal(monkeypatch):
    """CAS 失败且 durable 未收敛到 cancelled → 抛 RunRejected（路由 409），
    绝不无条件发「审批拒绝并终止」帧 + 回 202 cancelled（对外编造终态）。
    帧流经 spy 观测（评审 mutation 缺口：handle 在抛异常路径上不可达）。"""
    from opensearch_pipeline.agent_runtime.approval import RejectedTerminate

    frames, finishes = _spy_frames(monkeypatch)
    store = _TerminateStore(durable_after_cas="suspended")
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    with pytest.raises(RunRejected):
        ex.resume("run9", _ctx(), RejectedTerminate(),
                  DefaultAgentLoop(_scripted([ModelTurn(text="x")])), [])
    assert ("run9", "resuming", "cancelled") in store.transitions   # CAS 试过
    assert not any(isinstance(ev, RunFailed) for _, ev in frames)   # 不编造终态帧
    assert ("run9", False) in finishes                              # 收流但不写中继 __end__
    assert ex.active_count() == 0                                    # 槽位不泄漏


def test_p0_04_rejected_terminate_db_error_rolls_back_claim():
    """复查修正（评审 P2）：CAS 抛 DB 异常 ≠ CAS False——必须立即回边 resuming→suspended
    保住可重试性（撤回类场景可能无 approval_decision 行、B6 对账无从收敛，
    绝不能钉死在 resuming 等 ~15min reaper）。"""
    from opensearch_pipeline.agent_runtime.approval import RejectedTerminate

    class _DbErrStore(_FaultStore):
        def transition(self, run_id, frm, to):
            self.transitions.append((run_id, frm, to))
            if (frm, to) == ("resuming", "cancelled"):
                raise RuntimeError("db blip")
            return True

    store = _DbErrStore()
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    with pytest.raises(RuntimeError, match="db blip"):
        ex.resume("run9", _ctx(), RejectedTerminate(),
                  DefaultAgentLoop(_scripted([ModelTurn(text="x")])), [])
    assert ("run9", "resuming", "suspended") in store.transitions   # 立即回边，非钉死
    assert ex.active_count() == 0


def test_p0_04_rejected_terminate_converges_on_durable_cancelled():
    """CAS 失败但 durable 已是 cancelled（他方等价收敛）→ 照常宣告终态（与真相一致）。"""
    from opensearch_pipeline.agent_runtime.approval import RejectedTerminate

    store = _TerminateStore(durable_after_cas="cancelled")
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    handle = ex.resume("run9", _ctx(), RejectedTerminate(),
                       DefaultAgentLoop(_scripted([ModelTurn(text="x")])), [])
    events = list(handle.events())
    assert any(isinstance(e, RunFailed) and "拒绝" in e.error for e in events)
    assert ex.active_count() == 0


def test_p0_04_rejected_terminate_cas_ok_announces_cancelled():
    """CAS 成功的原有语义回归：终态帧照发、resuming→cancelled 落库。"""
    from opensearch_pipeline.agent_runtime.approval import RejectedTerminate

    store = _FaultStore()
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    handle = ex.resume("run9", _ctx(), RejectedTerminate(),
                       DefaultAgentLoop(_scripted([ModelTurn(text="x")])), [])
    events = list(handle.events())
    assert any(isinstance(e, RunFailed) and "拒绝" in e.error for e in events)
    assert ("run9", "resuming", "cancelled") in store.transitions
    assert ex.active_count() == 0


# ── P1-01：事件队列边界配置 ────────────────────────────────────────────────


def test_p1_01_queue_max_one_clamped_terminal_survives(monkeypatch):
    """RAG_AGENT_EVENT_QUEUE_MAX=1（退化配置）：clamp 到 ≥2，收尾哨兵不再挤掉
    队列里唯一的终态帧——消费者必能读到终态。"""
    monkeypatch.setenv("RAG_AGENT_EVENT_QUEUE_MAX", "1")
    h = RunHandle("r1")
    h._emit(RunFailed(error="boom", retryable=False))
    h._finish()
    events = list(h.events())
    assert any(isinstance(e, RunFailed) for e in events)


def test_p1_01_queue_zero_stays_unbounded(monkeypatch):
    """<=0 的历史语义不变：无界队列。"""
    monkeypatch.setenv("RAG_AGENT_EVENT_QUEUE_MAX", "0")
    assert RunHandle("r2")._q.maxsize == 0
