# -*- coding: utf-8 -*-
"""
test_ingest_lease_db.py — PR-4 摄取租约/栅栏的真实 MySQL 故障注入（schema/048）

依赖本地 MySQL（须已 apply schema/048；conftest 串行组成员——与 test_pipeline/
test_concurrency 共享 document_version 真实 DML）。全部「时间流逝」用 SQL 直改
lease_expires_at/updated_at 模拟，零 sleep（除 T5 的行锁阻塞窗口）。

核心断言面：
  · 存活运行（按时续租）不再被 2h 年龄清扫/接管抢占——本立项的标题不变量；
  · 到期租约 TTL 级快速接管（不必等 2h）；无租约行走 2h 年龄兜底（新旧混跑）；
  · 接管后僵尸的栅栏写原子性失败（LeaseLost）；
  · verify_for_update 行锁把「验租→写回→commit」变成零接管窗口；
  · off 臂：租约列纹丝不动，行为=现状。
"""

import threading
import time

import pytest

from opensearch_pipeline import ingest_lease
from opensearch_pipeline.ingest_lease import LeaseLost, LeaseSet


def _get_real_db_conn():
    """真实 MySQL 连接（自建、非池），host-pin 本地 dev 栈（同 test_concurrency 安全闸）。"""
    import pymysql
    from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
    config = get_config()
    if config.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", config.rds.host):
        raise RuntimeError(
            f"[PROD-GUARD] test_ingest_lease_db 仅允许本地 MySQL；解析到 host "
            f"{config.rds.host!r}，拒绝连接。")
    return pymysql.connect(
        host=config.rds.host, port=config.rds.port, user=config.rds.user,
        password=config.rds.password, database=config.rds.database, autocommit=False)


def _db_ready():
    """本地 MySQL 可用且 schema/048 已 apply（缺列=环境未就绪，整体 skip 不假红）。"""
    try:
        conn = _get_real_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.COLUMNS"
                    " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='document_version'"
                    " AND COLUMN_NAME='lease_epoch'")
                return cur.fetchone()[0] == 1
        finally:
            conn.close()
    except Exception:
        return False


skipif_no_db = pytest.mark.skipif(not _db_ready(), reason="Local MySQL/schema 048 not available")

_PFX = "LEASE_T_"


@pytest.fixture
def db():
    conn = _get_real_db_conn()
    yield conn
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM document_version WHERE doc_id LIKE '{_PFX}%%'")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def lease_on(monkeypatch):
    monkeypatch.setenv("RAG_INGEST_LEASE_ENABLE", "true")


def _seed(conn, doc_id, cps="NOT_STARTED", idx="NOT_INDEXED", ver=1):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO document_version (doc_id, version_no, content_process_status,"
            " index_status, status) VALUES (%s, %s, %s, %s, 'active')",
            (doc_id, ver, cps, idx))
    conn.commit()


def _row(conn, doc_id, ver=1):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content_process_status, index_status, lease_holder, lease_expires_at,"
            " lease_epoch FROM document_version WHERE doc_id=%s AND version_no=%s",
            (doc_id, ver))
        return cur.fetchone()


def _claim_stage2(conn, ls, doc_id, ver=1):
    """复刻 classify 认领 SQL 形态（片段接线与 pipeline_nodes 同构）。"""
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE document_version SET content_process_status = 'PROCESSING'"
            f"{ingest_lease.claim_set_sql()}"
            f" WHERE doc_id = %s AND version_no = %s"
            f" AND content_process_status IN ('NOT_STARTED', 'LOADING', 'FAILED')",
            ingest_lease.claim_set_params() + (doc_id, ver))
        claimed = cur.rowcount > 0
        if claimed:
            ls.fetch_and_register(cur, [(doc_id, ver)])
    conn.commit()
    return claimed


def _expire_lease(conn, doc_id, ver=1):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE document_version SET lease_expires_at = NOW(3) - INTERVAL 5 SECOND"
            " WHERE doc_id=%s AND version_no=%s", (doc_id, ver))
    conn.commit()


def _backdate_updated_at(conn, doc_id, hours=3, ver=1):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE document_version SET updated_at = NOW() - INTERVAL %s HOUR"
            " WHERE doc_id=%s AND version_no=%s", (hours, doc_id, ver))
    conn.commit()


def _foreign_takeover(conn, doc_id, ver=1, holder="zombie-hunter-9-deadbeef"):
    """模拟另一实例接管：他方 holder 落戳 + epoch+1（谓词=到期租约，同接管 SQL 语义）。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE document_version SET lease_holder=%s,"
            " lease_expires_at = NOW(3) + INTERVAL 900 SECOND,"
            " lease_epoch = lease_epoch + 1, updated_at = NOW()"
            " WHERE doc_id=%s AND version_no=%s AND lease_expires_at < NOW(3)",
            (holder, doc_id, ver))
        assert cur.rowcount == 1, "foreign takeover precondition: lease must be expired"
    conn.commit()


# ── T1 认领落戳/epoch 单调 ───────────────────────────────────────────────────

@skipif_no_db
def test_claim_stamps_lease_and_epoch_monotonic(db, lease_on):
    doc = _PFX + "T1"
    _seed(db, doc)
    ls = LeaseSet()
    assert _claim_stage2(db, ls, doc)
    cps, _, holder, expires, epoch = _row(db, doc)
    assert cps == "PROCESSING" and holder == ingest_lease.holder_id()
    assert expires is not None and epoch == 1 and ls.epoch((doc, 1)) == 1

    # 复位后重认领：epoch 单调 +1
    with db.cursor() as cur:
        cur.execute("UPDATE document_version SET content_process_status='FAILED',"
                    " retry_count=0 WHERE doc_id=%s", (doc,))
    db.commit()
    ls2 = LeaseSet()
    assert _claim_stage2(db, ls2, doc)
    assert _row(db, doc)[4] == 2 and ls2.epoch((doc, 1)) == 2


# ── T2 标题不变量：存活运行不再被清扫；到期才回收 ────────────────────────────

@skipif_no_db
def test_sweep_spares_live_lease_and_reaps_expired(db, lease_on):
    from opensearch_pipeline.dataworks_orchestrator import _reset_stale_stage2_locks
    doc = _PFX + "T2"
    _seed(db, doc, cps="LOADING")
    ls = LeaseSet()
    with db.cursor() as cur:
        cur.execute(
            f"UPDATE document_version SET content_process_status='LOADING'"
            f"{ingest_lease.claim_set_sql()} WHERE doc_id=%s",
            ingest_lease.claim_set_params() + (doc,))
        ls.fetch_and_register(cur, [(doc, 1)])
    db.commit()

    # >2h 无 updated_at 进展的**存活**持有者（租约在续）：旧实现必杀，现须放过
    _backdate_updated_at(db, doc, hours=3)
    _reset_stale_stage2_locks()
    assert _row(db, doc)[0] == "LOADING", "live lease holder must survive the 2h sweep"

    # 租约到期（持有者真死）：TTL 级回收，FAILED + 清租约
    _expire_lease(db, doc)
    n = _reset_stale_stage2_locks()
    cps, _, holder, expires, _ = _row(db, doc)
    assert n >= 1 and cps == "FAILED" and holder is None and expires is None


@skipif_no_db
def test_sweep_legacy_null_lease_rows_keep_2h_fallback(db, lease_on):
    from opensearch_pipeline.dataworks_orchestrator import _reset_stale_stage2_locks
    doc = _PFX + "T2L"
    _seed(db, doc, cps="LOADING")          # 旧包认领：无租约列（NULL）
    _backdate_updated_at(db, doc, hours=1)
    _reset_stale_stage2_locks()
    assert _row(db, doc)[0] == "LOADING", "fresh legacy row must not be reaped"
    _backdate_updated_at(db, doc, hours=3)
    _reset_stale_stage2_locks()
    assert _row(db, doc)[0] == "FAILED", "stale legacy row keeps the 2h fallback"


# ── T3 stage-3 接管谓词双臂 ──────────────────────────────────────────────────

@skipif_no_db
def test_stage3_takeover_respects_live_lease(db, lease_on):
    doc = _PFX + "T3"
    _seed(db, doc, idx="PROCESSING")
    ls = LeaseSet()
    with db.cursor() as cur:  # 直落带租约的 PROCESSING（模拟他批在跑）
        cur.execute(
            f"UPDATE document_version SET index_status='PROCESSING'"
            f"{ingest_lease.claim_set_sql()} WHERE doc_id=%s",
            ingest_lease.claim_set_params() + (doc,))
        ls.fetch_and_register(cur, [(doc, 1)])
    db.commit()
    _backdate_updated_at(db, doc, hours=3)   # 旧谓词会放行接管的形态

    def _try_takeover(conn):
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE document_version SET index_status='PROCESSING', updated_at=NOW()"
                f"{ingest_lease.claim_set_sql()}"
                f" WHERE doc_id=%s AND version_no=%s AND index_status='PROCESSING'"
                f" AND {ingest_lease.takeover_where_sql()}",
                ingest_lease.claim_set_params() + (doc, 1))
            rc = cur.rowcount
        conn.commit()
        return rc

    assert _try_takeover(db) == 0, "live lease must not be taken over (even >2h updated_at)"
    _expire_lease(db, doc)
    assert _try_takeover(db) == 1, "expired lease must be claimable immediately (no 2h wait)"
    assert _row(db, doc)[4] == 2, "takeover must bump the fencing epoch"


# ── T4 栅栏：接管后僵尸终态写原子性失败 ──────────────────────────────────────

@skipif_no_db
def test_fenced_terminal_write_rejected_after_takeover(db, lease_on):
    doc = _PFX + "T4"
    _seed(db, doc)
    ls = LeaseSet()
    assert _claim_stage2(db, ls, doc)
    _expire_lease(db, doc)
    _foreign_takeover(db, doc)

    key = (doc, 1)
    with db.cursor() as cur:
        cur.execute(
            f"UPDATE document_version SET content_process_status='DONE'"
            f"{ingest_lease.clear_set_sql()}"
            f" WHERE doc_id=%s AND version_no=%s{ls.fence_where_sql(key)}",
            (doc, 1) + ls.fence_where_params(key))
        with pytest.raises(LeaseLost):
            ls.check_fenced_write(cur, key)
    db.rollback()
    cps, _, holder, _, _ = _row(db, doc)
    assert cps == "PROCESSING" and holder == "zombie-hunter-9-deadbeef", \
        "zombie write must not land; new holder's state intact"
    assert ls.epoch(key) is None, "lost key must be discarded locally"


@skipif_no_db
def test_verify_still_held_snapshot(db, lease_on):
    doc = _PFX + "T4S"
    _seed(db, doc)
    ls = LeaseSet()
    assert _claim_stage2(db, ls, doc)
    with db.cursor() as cur:
        assert ls.verify_still_held(cur, (doc, 1)) is True
    _expire_lease(db, doc)
    _foreign_takeover(db, doc)
    with db.cursor() as cur:
        assert ls.verify_still_held(cur, (doc, 1)) is False
    db.rollback()
    assert ls.epoch((doc, 1)) is None


# ── T5 verify_for_update 行锁 = 零接管窗口 ───────────────────────────────────

@skipif_no_db
def test_verify_for_update_blocks_takeover_until_commit(db, lease_on):
    doc = _PFX + "T5"
    _seed(db, doc)
    ls = LeaseSet()
    assert _claim_stage2(db, ls, doc)
    _expire_lease(db, doc)   # 租约已到期：接管方随时有权抢——除非被行锁挡住

    hold_s = 0.6
    t_b_done = {}

    def _blocked_takeover():
        conn_b = _get_real_db_conn()
        try:
            t0 = time.monotonic()
            with conn_b.cursor() as cur:
                cur.execute(
                    "UPDATE document_version SET lease_holder='late-taker-1-cafebabe',"
                    " lease_expires_at = NOW(3) + INTERVAL 900 SECOND,"
                    " lease_epoch = lease_epoch + 1, updated_at = NOW()"
                    " WHERE doc_id=%s AND version_no=%s AND lease_expires_at < NOW(3)",
                    (doc, 1))
                t_b_done["rowcount"] = cur.rowcount
            conn_b.commit()
            t_b_done["waited"] = time.monotonic() - t0
        finally:
            conn_b.close()

    key = (doc, 1)
    with db.cursor() as cur:
        ls.verify_for_update(cur, key)      # FOR UPDATE：行锁到手（租约虽到期仍是我方 epoch）
        t = threading.Thread(target=_blocked_takeover)
        t.start()
        time.sleep(hold_s)                  # 模拟「验租后-commit 前」的写回窗口
        cur.execute(
            f"UPDATE document_version SET content_process_status='DONE'"
            f"{ingest_lease.clear_set_sql()}"
            f" WHERE doc_id=%s AND version_no=%s{ls.fence_where_sql(key)}",
            (doc, 1) + ls.fence_where_params(key))
        ls.check_fenced_write(cur, key)     # 窗口内不可能被接管 → 必然满额
    db.commit()
    t.join(timeout=10)

    assert t_b_done["waited"] >= hold_s * 0.8, "takeover must block on the row lock"
    assert t_b_done["rowcount"] == 0, \
        "post-commit takeover must find no expired lease (cleared by terminal write)"
    assert _row(db, doc)[0] == "DONE"


# ── T6 批量续租：延到期 + 部分丢失对账 ───────────────────────────────────────

@skipif_no_db
def test_renew_all_extends_and_reconciles_partial_loss(db, lease_on, monkeypatch):
    d1, d2 = _PFX + "T6A", _PFX + "T6B"
    _seed(db, d1)
    _seed(db, d2)
    ls = LeaseSet()
    assert _claim_stage2(db, ls, d1) and _claim_stage2(db, ls, d2)

    with db.cursor() as cur:  # 压低 d1 到期时间，续租后必须变晚
        cur.execute("UPDATE document_version SET lease_expires_at = NOW(3) + INTERVAL 1 SECOND"
                    " WHERE doc_id=%s", (d1,))
    db.commit()
    before = _row(db, d1)[3]

    _expire_lease(db, d2)
    _foreign_takeover(db, d2)   # d2 被接管 → renew_all 应对账丢弃且不打扰新持有者

    with db.cursor() as cur:
        lost = ls.renew_all(cur)
    db.commit()
    assert lost == 1 and ls.epoch((d2, 1)) is None and ls.epoch((d1, 1)) == 1
    after = _row(db, d1)[3]
    assert after > before, "renew_all must extend the surviving lease"
    assert _row(db, d2)[2] == "zombie-hunter-9-deadbeef", "new holder untouched"
    # main 移植修订（墓碑，codex 共识 2026-08-02）：renew_all 判负的 key 此后一切
    # 栅栏/验租恒拒绝——即使调用点忘了剔除，僵尸对 d2 的写也到不了库。
    with pytest.raises(LeaseLost):
        ls.fence_where_sql((d2, 1))
    with pytest.raises(LeaseLost):
        ls.fence_where_params((d2, 1))
    with db.cursor() as cur:
        assert ls.verify_still_held(cur, (d2, 1)) is False
        with pytest.raises(LeaseLost):
            ls.verify_for_update(cur, (d2, 1))
    db.rollback()


# ── T8 A3 live-lock 探针谓词真值表（R4b 共识，含 NULL updated_at）────────────

@skipif_no_db
def test_probe_predicate_truth_table_both_arms(db, lease_on, monkeypatch):
    """两臂对五类行的判定：legacy fresh/stale × lease live/expired × NULL updated_at。
    off 臂=旧 `updated_at >= cutoff`；on 臂=NOT(takeover)。断言（R4b）：
      · legacy fresh：两臂都算"未释放"；legacy stale：两臂都不算；
      · live lease：on 臂算（即使 updated_at 已 >2h——租约在续）；
      · expired lease：on 臂不算（TTL 级可抢）；
      · NULL updated_at 无租约行：两臂一致不计数（SQL 三值 known limitation）。"""
    rows = {
        "T8F": {},                                # legacy fresh
        "T8S": {"back_h": 3},                     # legacy stale
        "T8L": {"lease": "live", "back_h": 3},    # live lease（年龄已过 2h）
        "T8E": {"lease": "expired"},              # expired lease
        "T8N": {"null_updated": True},            # NULL updated_at, 无租约
    }
    null_ok = True
    for suffix, spec in rows.items():
        doc = _PFX + suffix
        _seed(db, doc, idx="PROCESSING")
        if spec.get("lease"):
            with db.cursor() as cur:
                cur.execute(
                    f"UPDATE document_version SET index_status='PROCESSING'"
                    f"{ingest_lease.claim_set_sql()} WHERE doc_id=%s",
                    ingest_lease.claim_set_params() + (doc,))
            db.commit()
            if spec["lease"] == "expired":
                _expire_lease(db, doc)
        if spec.get("back_h"):
            _backdate_updated_at(db, doc, hours=spec["back_h"])
        if spec.get("null_updated"):
            try:
                with db.cursor() as cur:
                    cur.execute("UPDATE document_version SET updated_at=NULL"
                                " WHERE doc_id=%s", (doc,))
                db.commit()
            except Exception:
                db.rollback()
                null_ok = False   # 列不可空的环境：跳过 NULL 行断言，其余照测

    def _live_set(pred_sql):
        with db.cursor() as cur:
            cur.execute(
                f"SELECT doc_id FROM document_version"
                f" WHERE doc_id LIKE '{_PFX}T8%%' AND index_status='PROCESSING'"
                f" AND ({pred_sql})")
            return {r[0].replace(_PFX, "") for r in cur.fetchall()}

    on_set = _live_set(f"NOT ({ingest_lease.takeover_where_sql()})")
    monkeypatch.delenv("RAG_INGEST_LEASE_ENABLE", raising=False)
    off_pred = "updated_at >= NOW() - INTERVAL 2 HOUR"
    off_set = _live_set(off_pred)

    assert "T8F" in on_set and "T8F" in off_set, "legacy fresh：两臂都算未释放"
    assert "T8S" not in on_set and "T8S" not in off_set, "legacy stale：两臂都可抢"
    assert "T8L" in on_set, "live lease 必须算未释放（即使年龄 >2h）"
    assert "T8E" not in on_set, "expired lease 必须可抢（不算未释放）"
    if null_ok:
        assert "T8N" not in on_set and "T8N" not in off_set,             "NULL updated_at 无租约行：两臂一致不计数（known limitation）"


# ── T7 off 臂：租约列纹丝不动 ────────────────────────────────────────────────

@skipif_no_db
def test_off_arm_leaves_lease_columns_null(db, monkeypatch):
    monkeypatch.delenv("RAG_INGEST_LEASE_ENABLE", raising=False)
    doc = _PFX + "T7"
    _seed(db, doc)
    ls = LeaseSet()
    assert _claim_stage2(db, ls, doc)
    cps, _, holder, expires, epoch = _row(db, doc)
    assert cps == "PROCESSING" and holder is None and expires is None and epoch == 0
    with db.cursor() as cur:   # 栅栏/验租/续租全 no-op
        ls.verify_for_update(cur, (doc, 1))
        ls.maybe_renew(cur, (doc, 1))
        assert ls.fence_where_sql((doc, 1)) == ""
