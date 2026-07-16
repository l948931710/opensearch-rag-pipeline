# -*- coding: utf-8 -*-
"""
test_agent_approval_integrity_db.py — P0-C 审批完整性真库对抗测试（重评报告 §5C 验收 5）

对本地真 MySQL（fuling_operation，schema/025+031）打 approval_store 的硬化语义：
- **过期在决策时刻原子裁决**：pending 但 expires_at 已过 → decide 返回 DECIDE_EXPIRED 且
  请求行原子转 expired（不依赖 reaper 窗口——「reaper down」场景即本测试本身：无 reaper 在跑）。
- **决定绑定最终参数摘要**：approved→final_args_digest=原 args_digest；edited→改后参数
  原文 sha256；get_decision 读回一致。
- **并发双批**：两线程同时 decide → 恰一个 ACCEPTED（FOR UPDATE + pending CAS first-valid-wins）。
- **幂等键**：同键同向重放 → DUPLICATE；同键不同向 → ALREADY_DECIDED。

无本地 MySQL / 无表 → 整体 skip；host-pin 本地，绝不写 staging/prod（同 e2e_local_db 纪律）。
"""
import os
import threading
import uuid

import pytest

from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target


def _local_operation_conn():
    import pymysql
    cfg = get_config()
    if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
        raise RuntimeError(f"[PROD-GUARD] 仅本地 MySQL；解析到 host {cfg.rds.host!r}，拒绝连接。")
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.operation_database,
                           autocommit=True, connect_timeout=5)


def _db_ready():
    try:
        conn = _local_operation_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_schema=DATABASE() AND table_name='approval_decision' "
                        "AND column_name='final_args_digest'")
            ok = cur.fetchone()[0] == 1
        conn.close()
        return ok
    except Exception:
        return False


_AGENT_DB_OK = _db_ready()

# 批次6（unknown-unknowns P1-06）：RAG_AGENT_TESTS_REQUIRE_RDS=1 → 探测断线收集期硬红
# （与 ontology 家族 REQUIRE_RDS 同纪律，CI db-integration 的零跳过机器强制）。
if not _AGENT_DB_OK and os.environ.get("RAG_AGENT_TESTS_REQUIRE_RDS", "").strip() == "1":
    raise RuntimeError(
        "RAG_AGENT_TESTS_REQUIRE_RDS=1 但本地 MySQL/approval 硬化列不可用——"
        "真库契约族不许静默 skip（P1-06）；检查 ci_load_schema 与连接配置")

skipif_no_db = pytest.mark.skipif(
    not _AGENT_DB_OK, reason="本地 MySQL 无 approval 硬化列（先 apply schema/025+031）")


def _seed_request(*, ttl_s: int, args=None, run_id=None) -> str:
    """直插一行 pending approval_request（绕过 executor，测 decide 语义本身）。"""
    from opensearch_pipeline.agent_runtime.tool_executor import digest
    request_id = uuid.uuid4().hex
    args = args if args is not None else {"qty": 1}
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO approval_request "
                "(request_id, run_id, call_id, tool_name, tool_version, proposed_args_json, "
                " args_digest, render_summary, requested_by, approver_scope, status, "
                " expires_at, created_at) "
                "VALUES (%s,%s,'call_t','t.write','1.0','{}',%s,'t','tester','pmc','pending',"
                " DATE_ADD(NOW(3), INTERVAL %s SECOND), NOW(3))",
                (request_id, run_id or uuid.uuid4().hex, digest(args), int(ttl_s)))
    finally:
        conn.close()
    return request_id


def _request_status(request_id: str) -> str:
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM approval_request WHERE request_id=%s", (request_id,))
            return cur.fetchone()[0]
    finally:
        conn.close()


@skipif_no_db
def test_decide_expired_pending_atomically_rejected():
    """过期 pending：decide 即拒（DECIDE_EXPIRED）+ 行原子转 expired——不等 reaper。"""
    from opensearch_pipeline.agent_runtime.approval_store import (
        DECIDE_EXPIRED, RDSApprovalStore)
    rid = _seed_request(ttl_s=-5)          # 已过期 5 秒
    store = RDSApprovalStore()
    res = store.decide(rid, decision="approved", decided_by="admin1")
    assert res == DECIDE_EXPIRED
    assert _request_status(rid) == "expired"
    # 决定行绝不产生（过期不是决定）
    assert store.get_decision(rid) is None


@skipif_no_db
def test_decide_binds_final_args_digest_approved_and_edited():
    """approved→final=原 digest；edited→final=改后参数 sha256；get_decision 读回。"""
    from opensearch_pipeline.agent_runtime.approval_store import (
        DECIDE_ACCEPTED, RDSApprovalStore)
    from opensearch_pipeline.agent_runtime.tool_executor import digest
    store = RDSApprovalStore()

    r1 = _seed_request(ttl_s=600, args={"qty": 1})
    assert store.decide(r1, decision="approved", decided_by="admin1") == DECIDE_ACCEPTED
    d1 = store.get_decision(r1)
    assert d1["decision"] == "approved"
    assert d1["final_args_digest"] == digest({"qty": 1})
    assert d1["decided_by"] == "admin1"

    r2 = _seed_request(ttl_s=600, args={"qty": 1})
    edited = {"qty": 3}
    assert store.decide(r2, decision="edited", decided_by="admin2",
                        edited_args=edited) == DECIDE_ACCEPTED
    d2 = store.get_decision(r2)
    assert d2["final_args_digest"] == digest(edited)
    assert d2["final_args_digest"] != digest({"qty": 1})
    # 改参重放（digest 不一致）在路由层被 409——digest 事实在库，这里断言事实本身
    assert digest({"qty": 999999}) != d2["final_args_digest"]


@skipif_no_db
def test_concurrent_double_decide_single_winner():
    """并发双批：两线程不同处置同时 decide → 恰一个 ACCEPTED（first-valid-wins）。"""
    from opensearch_pipeline.agent_runtime.approval_store import (
        DECIDE_ACCEPTED, RDSApprovalStore)
    rid = _seed_request(ttl_s=600)
    store = RDSApprovalStore()
    results = {}
    barrier = threading.Barrier(2)

    def _worker(name, decision):
        barrier.wait()
        results[name] = store.decide(rid, decision=decision, decided_by=name)

    t1 = threading.Thread(target=_worker, args=("a1", "approved"))
    t2 = threading.Thread(target=_worker, args=("a2", "rejected_terminate"))
    t1.start(); t2.start(); t1.join(); t2.join()
    accepted = [k for k, v in results.items() if v == DECIDE_ACCEPTED]
    assert len(accepted) == 1, results
    # 库内状态与胜者一致，且只有一行决定
    dec = store.get_decision(rid)
    assert dec["decided_by"] == accepted[0]
    assert _request_status(rid) in ("approved", "rejected_terminate")


@skipif_no_db
def test_idempotency_key_replay_semantics():
    """同键同向重放 → DUPLICATE（不重复改状态）；同键不同向 → ALREADY_DECIDED。"""
    from opensearch_pipeline.agent_runtime.approval_store import (
        DECIDE_ACCEPTED, DECIDE_ALREADY_DECIDED, DECIDE_DUPLICATE, RDSApprovalStore)
    rid = _seed_request(ttl_s=600)
    store = RDSApprovalStore()
    assert store.decide(rid, decision="approved", decided_by="a1",
                        idempotency_key="k1") == DECIDE_ACCEPTED
    assert store.decide(rid, decision="approved", decided_by="a1",
                        idempotency_key="k1") == DECIDE_DUPLICATE
    assert store.decide(rid, decision="rejected_terminate", decided_by="a1",
                        idempotency_key="k1") == DECIDE_ALREADY_DECIDED
    # 不同键迟到决策也拒
    assert store.decide(rid, decision="approved", decided_by="a2",
                        idempotency_key="k2") == DECIDE_ALREADY_DECIDED


@skipif_no_db
def test_boundary_expiry_race_cas_guard():
    """临界时间：读时未过期、CAS 时已过期 → UPDATE 条件带 expires_at>NOW(3) 兜底拒绝。
    用 1 秒 TTL + 决策前 sleep 逼近边界（松断言：EXPIRED 或 ACCEPTED 都合法，但**绝不能**
    出现「行已 expired 却有决定行」的矛盾态）。"""
    import time

    from opensearch_pipeline.agent_runtime.approval_store import RDSApprovalStore
    rid = _seed_request(ttl_s=1)
    time.sleep(1.05)
    store = RDSApprovalStore()
    store.decide(rid, decision="approved", decided_by="a1")
    status = _request_status(rid)
    dec = store.get_decision(rid)
    if status == "expired":
        assert dec is None            # 过期路径绝不留决定行
    else:
        assert status == "approved" and dec is not None


@skipif_no_db
def test_list_pending_matches_csv_scope_components():
    """P1-11：CSV approver_scope（"pmc,rd"）按分量进主/备 steward 的待办队列（FIND_IN_SET）。"""
    from opensearch_pipeline.agent_runtime.approval_store import RDSApprovalStore
    run_id = uuid.uuid4().hex
    request_id = uuid.uuid4().hex
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO approval_request "
                "(request_id, run_id, call_id, tool_name, tool_version, proposed_args_json, "
                " args_digest, render_summary, requested_by, approver_scope, status, "
                " expires_at, created_at) "
                "VALUES (%s,%s,'c','t.write','1.0','{}','d','t','tester','pmc,rd','pending',"
                " DATE_ADD(NOW(3), INTERVAL 600 SECOND), NOW(3))",
                (request_id, run_id))
    finally:
        conn.close()
    store = RDSApprovalStore()
    try:
        assert any(r["request_id"] == request_id for r in store.list_pending(["pmc"]))   # 主 steward
        assert any(r["request_id"] == request_id for r in store.list_pending(["rd"]))    # backup
        assert not any(r["request_id"] == request_id for r in store.list_pending(["hr"]))
        assert any(r["request_id"] == request_id for r in store.list_pending(None))      # kb_admin 全量
    finally:
        conn = _local_operation_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM approval_request WHERE request_id=%s", (request_id,))
        finally:
            conn.close()
