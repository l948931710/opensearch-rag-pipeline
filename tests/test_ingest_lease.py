"""ingest_lease 协议模块单测（PR-4 W1）——纯逻辑/假 cursor，不碰真库。

真库故障注入全集在 tests/test_ingest_lease_db.py（conftest 串行组）。
核心断言面：off 臂逐字节等价（SQL 片段为空 ⇒ 调用点拼出的语句与现状一致）、
on 臂片段形态、fetch/renew/fence/verify 的 LeaseLost 语义与节流。
"""

import pytest

from opensearch_pipeline import ingest_lease
from opensearch_pipeline.ingest_lease import LeaseLost, LeaseSet


class FakeCursor:
    def __init__(self, rowcount=1, rows=None):
        self.rowcount = rowcount
        self._rows = list(rows or [])
        self.executed = []  # [(sql, params)]

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


@pytest.fixture
def lease_on(monkeypatch):
    monkeypatch.setenv("RAG_INGEST_LEASE_ENABLE", "true")


@pytest.fixture
def lease_off(monkeypatch):
    monkeypatch.delenv("RAG_INGEST_LEASE_ENABLE", raising=False)


# ── off 臂：所有片段为空、所有动作 no-op（逐字节现状）────────────────────────

def test_off_arm_fragments_empty(lease_off):
    assert ingest_lease.claim_set_sql() == ""
    assert ingest_lease.claim_set_params() == ()
    assert ingest_lease.clear_set_sql() == ""
    ls = LeaseSet()
    assert ls.fence_where_sql(("d", 1)) == ""
    assert ls.fence_where_params(("d", 1)) == ()


def test_off_arm_takeover_predicate_is_legacy_verbatim(lease_off):
    assert ingest_lease.takeover_where_sql() == "updated_at < NOW() - INTERVAL 2 HOUR"
    assert ingest_lease.takeover_where_sql("dv.") == "dv.updated_at < NOW() - INTERVAL 2 HOUR"


def test_off_arm_actions_noop(lease_off):
    ls = LeaseSet()
    cur = FakeCursor()
    assert ls.fetch_and_register(cur, [("d", 1)]) == 0
    ls.maybe_renew(cur, ("d", 1))
    ls.verify_for_update(cur, ("d", 1))
    ls.check_fenced_write(cur, ("d", 1))
    assert cur.executed == []  # off 臂零 SQL


def test_unregistered_key_noop_even_when_on(lease_on):
    # flag 开但该 doc 不是租约认领路径（e.g. 认领前失败写 FAILED）——栅栏/续租不介入
    ls = LeaseSet()
    cur = FakeCursor(rowcount=0)
    ls.maybe_renew(cur, ("d", 1))
    ls.check_fenced_write(cur, ("d", 1))
    assert ls.fence_where_sql(("d", 1)) == ""
    assert cur.executed == []


# ── on 臂：片段形态 ──────────────────────────────────────────────────────────

def test_on_arm_claim_fragment_and_params(lease_on):
    frag = ingest_lease.claim_set_sql()
    assert frag.startswith(", ")
    assert "lease_epoch = lease_epoch + 1" in frag       # 恒改行——changed-rows 陷阱免疫
    assert "NOW(3) + INTERVAL %s SECOND" in frag         # SQL 侧时钟
    holder, ttl = ingest_lease.claim_set_params()
    assert holder == ingest_lease.holder_id()
    assert ttl == ingest_lease.lease_ttl_s()


def test_on_arm_takeover_predicate_dual_branch(lease_on):
    sql = ingest_lease.takeover_where_sql("dv.")
    assert "dv.lease_expires_at < NOW(3)" in sql
    assert "dv.lease_holder IS NULL AND dv.updated_at < NOW() - INTERVAL 2 HOUR" in sql


def test_holder_id_stable_and_shaped():
    a, b = ingest_lease.holder_id(), ingest_lease.holder_id()
    assert a == b and a.count("-") >= 2 and len(a) <= 64


def test_ttl_renew_env_clamps(monkeypatch):
    monkeypatch.setenv("RAG_INGEST_LEASE_TTL_S", "5")
    monkeypatch.setenv("RAG_INGEST_LEASE_RENEW_S", "1")
    assert ingest_lease.lease_ttl_s() == 60
    assert ingest_lease.lease_renew_s() == 10


# ── LeaseSet：注册/续租/栅栏/验租 ────────────────────────────────────────────

def _registered(monkeypatch=None):
    ls = LeaseSet()
    cur = FakeCursor(rows=[("d", 1, 7)])
    assert ls.fetch_and_register(cur, [("d", 1)]) == 1
    assert ls.epoch(("d", 1)) == 7
    return ls


def test_fetch_and_register_filters_by_holder(lease_on):
    # SELECT 谓词带 lease_holder=me——他方认领的行不会混进来（由 SQL 保证，这里断言谓词形态）
    ls = LeaseSet()
    cur = FakeCursor(rows=[("d", 1, 3), ("e", 2, 9)])
    ls.fetch_and_register(cur, [("d", 1), ("e", 2)])
    sql, params = cur.executed[0]
    assert "lease_holder = %s" in sql
    assert params[-1] == ingest_lease.holder_id()
    assert ls.epoch(("e", 2)) == 9


def test_maybe_renew_throttles_and_renews(lease_on, monkeypatch):
    ls = _registered()
    cur = FakeCursor(rowcount=1)
    ls.maybe_renew(cur, ("d", 1))          # 注册时刻即 last_renew——节流窗口内 no-op
    assert cur.executed == []
    monkeypatch.setattr(ingest_lease.time, "monotonic", lambda: 1e9)  # 越过节流窗
    ls.maybe_renew(cur, ("d", 1))
    assert len(cur.executed) == 1
    sql, params = cur.executed[0]
    assert "lease_expires_at = NOW(3)" in sql and "lease_epoch = %s" in sql
    assert params[-1] == 7


def test_maybe_renew_lost_lease_raises_and_discards(lease_on, monkeypatch):
    ls = _registered()
    monkeypatch.setattr(ingest_lease.time, "monotonic", lambda: 1e9)
    cur = FakeCursor(rowcount=0)
    with pytest.raises(LeaseLost):
        ls.maybe_renew(cur, ("d", 1))
    assert ls.epoch(("d", 1)) is None      # 丢锁即出集
    # main 移植修订（墓碑）：非自愿失锁后栅栏不再退化 no-op，而是恒拒绝——僵尸写到不了库
    with pytest.raises(LeaseLost):
        ls.fence_where_sql(("d", 1))


def test_fence_fragments_after_register(lease_on):
    ls = _registered()
    assert ls.fence_where_sql(("d", 1)) == " AND lease_holder = %s AND lease_epoch = %s"
    assert ls.fence_where_params(("d", 1)) == (ingest_lease.holder_id(), 7)


def test_check_fenced_write_raises_on_shortfall(lease_on):
    ls = _registered()
    with pytest.raises(LeaseLost):
        ls.check_fenced_write(FakeCursor(rowcount=0), ("d", 1))
    ls2 = _registered()
    ls2.check_fenced_write(FakeCursor(rowcount=1), ("d", 1))  # 满额通过


def test_verify_for_update_pass_and_fail(lease_on):
    ls = _registered()
    ok = FakeCursor(rows=[(ingest_lease.holder_id(), 7)])
    ls.verify_for_update(ok, ("d", 1))
    assert "FOR UPDATE" in ok.executed[0][0]

    ls2 = _registered()
    stolen = FakeCursor(rows=[("other-holder-1-abc", 8)])
    with pytest.raises(LeaseLost):
        ls2.verify_for_update(stolen, ("d", 1))

    ls3 = _registered()
    gone = FakeCursor(rows=[])
    with pytest.raises(LeaseLost):
        ls3.verify_for_update(gone, ("d", 1))


def test_get_lease_set_ctx_singleton(lease_on):
    ctx = {}
    a = ingest_lease.get_lease_set(ctx)
    assert ingest_lease.get_lease_set(ctx) is a
