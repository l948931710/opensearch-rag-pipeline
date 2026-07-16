# -*- coding: utf-8 -*-
"""
test_unknown_unknowns_batch5.py — 批次5（P0-06/P0-07d/P1-03/P1-08/P1-10）

生产姿态断言（REQUIRE_AUTH/ACL_FAIL_CLOSED fail-closed + 过渡 ack）· 拓扑防呆
（RAG_EXPECTED_REPLICAS>1 × memory 后端）· 审批 TTL 单源派生 + reaper cross-heal
（真库）· kill switch 冷实例 HIGH_WRITE fail-closed · readiness 新探针
（列契约/kill-switch/038 回填）。flag 拆分（P0-07a/b/c）的注册/授予语义在
test_ontology_resolve / test_ontology_identity_resolve_tool 就地扩展。
"""
import uuid
from types import SimpleNamespace

import pytest

from tests.test_config_loading import _fresh_load

PROD_RDS = "rm-bp1zc3iu8r5x0wsm45.mysql.rds.aliyuncs.com"


# ── P0-07d：生产安全姿态断言 ─────────────────────────────────────────────────────
class TestProdPostureAssertion:
    def test_missing_posture_raises(self):
        with pytest.raises(ValueError, match="RAG_REQUIRE_AUTH"):
            _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                        RAG_DASHSCOPE_API_KEY="x", RAG_ALLOW_LEGACY_OPEN_PROD="")

    def test_partial_posture_names_the_missing_one(self):
        with pytest.raises(ValueError, match="RAG_ACL_FAIL_CLOSED"):
            _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                        RAG_DASHSCOPE_API_KEY="x", RAG_ALLOW_LEGACY_OPEN_PROD="",
                        RAG_REQUIRE_AUTH="true")

    def test_posture_flags_on_passes(self):
        cfg = _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                          RAG_DASHSCOPE_API_KEY="x", RAG_ALLOW_LEGACY_OPEN_PROD="",
                          RAG_REQUIRE_AUTH="true", RAG_ACL_FAIL_CLOSED="true")
        assert cfg.environment == "production"

    def test_legacy_ack_is_transitional_escape(self):
        cfg = _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                          RAG_DASHSCOPE_API_KEY="x",
                          RAG_ALLOW_LEGACY_OPEN_PROD="ack")
        assert cfg.environment == "production"

    def test_development_unaffected(self):
        cfg = _fresh_load(RAG_SIMULATE="true", RAG_ALLOW_LEGACY_OPEN_PROD="")
        assert cfg.environment not in ("production", "staging")


# ── P1-10：拓扑防呆 ─────────────────────────────────────────────────────────────
class TestTopologyGuard:
    _BASE = dict(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                 RAG_DASHSCOPE_API_KEY="x", RAG_REQUIRE_AUTH="true",
                 RAG_ACL_FAIL_CLOSED="true", RAG_ALLOW_LEGACY_OPEN_PROD="")

    def test_replicas_gt1_memory_backend_raises(self):
        with pytest.raises(ValueError, match="RAG_EXPECTED_REPLICAS"):
            _fresh_load(RAG_EXPECTED_REPLICAS="2", **self._BASE)

    def test_replicas_gt1_all_redis_passes(self):
        cfg = _fresh_load(RAG_EXPECTED_REPLICAS="2",
                          RAG_SESSION_BACKEND="redis", RAG_RATE_LIMIT_BACKEND="redis",
                          RAG_MSG_DEDUP_BACKEND="redis", RAG_TOKEN_CACHE_BACKEND="redis",
                          **self._BASE)
        assert cfg.environment == "production"

    def test_single_replica_memory_passes(self):
        cfg = _fresh_load(RAG_EXPECTED_REPLICAS="1", **self._BASE)
        assert cfg.environment == "production"


# ── P1-08：审批 TTL 单源派生 ─────────────────────────────────────────────────────
class TestApprovalTtlSingleSource:
    def test_follows_suspended_when_unset(self, monkeypatch):
        from opensearch_pipeline.agent_runtime import approval_store as ap
        monkeypatch.delenv("RAG_AGENT_APPROVAL_TTL_S", raising=False)
        monkeypatch.setenv("RAG_AGENT_SUSPENDED_TTL_S", "3600")
        assert ap.approval_ttl_s() == 3600

    def test_both_unset_uses_default(self, monkeypatch):
        from opensearch_pipeline.agent_runtime import approval_store as ap
        monkeypatch.delenv("RAG_AGENT_APPROVAL_TTL_S", raising=False)
        monkeypatch.delenv("RAG_AGENT_SUSPENDED_TTL_S", raising=False)
        assert ap.approval_ttl_s() == ap.DEFAULT_APPROVAL_TTL_S

    def test_explicit_wins_and_mismatch_warns_once(self, monkeypatch, caplog):
        from opensearch_pipeline.agent_runtime import approval_store as ap
        monkeypatch.setattr(ap, "_TTL_MISMATCH_WARNED", False)
        monkeypatch.setenv("RAG_AGENT_APPROVAL_TTL_S", "100")
        monkeypatch.setenv("RAG_AGENT_SUSPENDED_TTL_S", "200")
        with caplog.at_level("WARNING"):
            assert ap.approval_ttl_s() == 100
            assert ap.approval_ttl_s() == 100
        assert sum("漂移" in r.message for r in caplog.records) == 1   # 只警一次


# ── P1-03：kill switch 冷实例 HIGH_WRITE fail-closed ──────────────────────────────
class _FakeToolReg:
    def to_registry_rows(self, registered_by="platform"):
        def _row(name, risk):
            return {"tool_name": name, "version": "1", "spec_json": "{}",
                    "risk_level": risk, "permission_scope": "s", "owner_team": "t",
                    "status": "active", "registered_by": registered_by}
        return [_row("safe_read", "read_only"), _row("danger_write", "high_write")]


class TestKillSwitchFailClosed:
    def _store_with_dead_db(self, monkeypatch):
        from opensearch_pipeline.agent_runtime.registry_store import RDSToolRegistryStore
        store = RDSToolRegistryStore(ttl_s=0)
        monkeypatch.setattr(store, "_conn",
                            lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        return store

    def test_cold_failure_disables_high_write_only(self, monkeypatch):
        store = self._store_with_dead_db(monkeypatch)
        with pytest.raises(RuntimeError):
            store.sync_specs(_FakeToolReg())      # DB 写失败上抛（调用方 fail-open）
        # 名单在 DB 写之前已捕获 → 冷实例 + DB 故障：写工具 fail-closed、读工具不误杀
        assert store.disabled_names() == {"danger_write"}

    def test_warm_snapshot_still_wins_over_failure(self, monkeypatch):
        store = self._store_with_dead_db(monkeypatch)
        store._high_write_names = {"danger_write"}
        store._cached_disabled = {"ops_disabled_tool"}    # 上次成功快照
        assert store.disabled_names() == {"ops_disabled_tool"}

    def test_cold_failure_without_sync_returns_empty(self, monkeypatch):
        # 从未 sync（无名单可依）→ 维持历史 fail-open 空集（无从得知写工具名单）
        store = self._store_with_dead_db(monkeypatch)
        assert store.disabled_names() == set()


# ── P0-06：readiness 新探针 ──────────────────────────────────────────────────────
class _Cur:
    def __init__(self, rows_by_sql):
        self._rows = rows_by_sql
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        for key, val in self._rows.items():
            if key in sql:
                if isinstance(val, Exception):
                    raise val
                self._sql = key
                return
        self._sql = ""

    def fetchone(self):
        rows = self._rows.get(self._sql) or [(0,)]
        return rows[0]

    def fetchall(self):
        return self._rows.get(self._sql) or []


class _Conn:
    def __init__(self, rows_by_sql):
        self._rows = rows_by_sql

    def cursor(self):
        return _Cur(self._rows)

    def close(self):
        pass


def _wire(monkeypatch, rows_by_sql):
    from opensearch_pipeline import readiness
    readiness._reset_cache()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda *a, **k: _Conn(rows_by_sql))
    monkeypatch.setattr("opensearch_pipeline.config.get_config",
                        lambda: SimpleNamespace(rds=SimpleNamespace(
                            database="k", operation_database="o", ontology_database="t")))
    return readiness


class TestReadinessBatch5:
    def test_schema_contract_skipped_when_flags_off(self, monkeypatch):
        from opensearch_pipeline import readiness
        readiness._reset_cache()
        monkeypatch.delenv("RAG_AGENT_ENABLE", raising=False)
        monkeypatch.delenv("RAG_ONTOLOGY_ENABLE", raising=False)
        assert readiness.schema_contract_status() == "skipped"

    def test_schema_contract_missing_column(self, monkeypatch):
        r = _wire(monkeypatch, {"information_schema.columns": [(0,)]})
        monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
        monkeypatch.delenv("RAG_ONTOLOGY_ENABLE", raising=False)
        st = r.schema_contract_status()
        assert st.startswith("missing:") and "agent_run.message_id" in st

    def test_schema_contract_ok(self, monkeypatch):
        r = _wire(monkeypatch, {"information_schema.columns": [(1,)]})
        monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
        monkeypatch.setenv("RAG_ONTOLOGY_ENABLE", "true")
        assert r.schema_contract_status() == "ok"

    def test_kill_switch_status_paths(self, monkeypatch):
        from opensearch_pipeline import readiness
        from opensearch_pipeline.agent_runtime.registry_store import RDSToolRegistryStore
        readiness._reset_cache()
        monkeypatch.delenv("RAG_AGENT_ENABLE", raising=False)
        assert readiness.kill_switch_status() == "skipped"
        monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
        monkeypatch.setattr(RDSToolRegistryStore, "probe_health", lambda self: "ok")
        assert readiness.kill_switch_status() == "ok"
        readiness._reset_cache()
        monkeypatch.setattr(RDSToolRegistryStore, "probe_health", lambda self: "error")
        assert readiness.kill_switch_status() == "error"

    def test_kill_switch_critical_follows_write_flag(self, monkeypatch):
        from opensearch_pipeline import readiness
        monkeypatch.delenv("RAG_ONTOLOGY_TOOLS_ENABLE", raising=False)
        monkeypatch.delenv("RAG_ONTOLOGY_WRITE_TOOLS_ENABLE", raising=False)
        assert readiness.kill_switch_critical() is False
        monkeypatch.setenv("RAG_ONTOLOGY_TOOLS_ENABLE", "true")
        monkeypatch.setenv("RAG_ONTOLOGY_WRITE_TOOLS_ENABLE", "true")
        assert readiness.kill_switch_critical() is True

    def test_ontology_backfill_pending_and_ok(self, monkeypatch):
        r = _wire(monkeypatch, {"ontology_object": [(7,)]})
        monkeypatch.setenv("RAG_ONTOLOGY_ENABLE", "true")
        assert r.ontology_backfill_status() == "pending(7)"
        r2 = _wire(monkeypatch, {"ontology_object": [(0,)]})
        assert r2.ontology_backfill_status() == "ok"
        from opensearch_pipeline import readiness
        readiness._reset_cache()
        monkeypatch.delenv("RAG_ONTOLOGY_ENABLE", raising=False)
        assert readiness.ontology_backfill_status() == "skipped"


# ── P1-08 cross-heal（真库）──────────────────────────────────────────────────────
def _local_operation_conn():
    import pymysql

    from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
    cfg = get_config()
    if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
        raise RuntimeError(f"[PROD-GUARD] 仅本地 MySQL；解析到 host {cfg.rds.host!r}，拒绝连接。")
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.operation_database,
                           autocommit=True, connect_timeout=5)


def _cross_heal_db_ready():
    try:
        conn = _local_operation_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema=DATABASE() AND table_name IN "
                        "('agent_run','approval_request')")
            ok = cur.fetchone()[0] == 2
        conn.close()
        return ok
    except Exception:   # noqa: BLE001
        return False


skipif_no_db = pytest.mark.skipif(not _cross_heal_db_ready(), reason="本地 MySQL 不可用")


@skipif_no_db
def test_ttl_cross_heal_both_directions():
    """审批过期 × run 挂起 → run 提前 expired（释放 active_thread）；
    run 终态 × 审批 pending → 审批提前 expired（清队列幽灵）。
    已决（approved）挂起 run 绝不误杀（归 B6 重驱）。"""
    from opensearch_pipeline.agent_runtime.approval_store import RDSApprovalStore
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore
    mark = "zzuk5_" + uuid.uuid4().hex[:8]
    r_susp, r_term, r_decided = f"{mark}a", f"{mark}b", f"{mark}c"
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            for rid, status in ((r_susp, "suspended"), (r_term, "succeeded"),
                                (r_decided, "suspended")):
                cur.execute(
                    "INSERT INTO agent_run (run_id, thread_id, user_id, channel, "
                    "agent_profile, status, acl_groups_snapshot, heartbeat_at, started_at) "
                    "VALUES (%s,%s,%s,'console','default',%s,'[]',NOW(3),NOW(3))",
                    (rid, f"t-{rid}", "zzuk5", status))
            def _areq(rid, status):
                cur.execute(
                    "INSERT INTO approval_request (request_id, run_id, call_id, tool_name, "
                    "proposed_args_json, args_digest, render_summary, requested_by, "
                    "approver_scope, status, expires_at, created_at) "
                    "VALUES (%s,%s,'c1','t','{}','d','s','u','scope',%s,"
                    "DATE_ADD(NOW(3), INTERVAL 3600 SECOND),NOW(3))",
                    (uuid.uuid4().hex, rid, status))
            _areq(r_susp, "expired")       # 审批先过期 → run 应提前收口
            _areq(r_term, "pending")       # run 已终态 → 审批应提前收口
            _areq(r_decided, "approved")   # 已决待重驱 → 双方向都不许动

        n_orphan = RDSApprovalStore().expire_for_terminal_runs()
        n_susp = RDSRunStore().expire_suspended_with_expired_approval()
        assert n_orphan >= 1 and n_susp >= 1

        with conn.cursor() as cur:
            cur.execute("SELECT status FROM agent_run WHERE run_id=%s", (r_susp,))
            assert cur.fetchone()[0] == "expired"
            cur.execute("SELECT status FROM approval_request WHERE run_id=%s", (r_term,))
            assert cur.fetchone()[0] == "expired"
            cur.execute("SELECT status FROM agent_run WHERE run_id=%s", (r_decided,))
            assert cur.fetchone()[0] == "suspended"       # 已决挂起不误杀
            cur.execute("SELECT status FROM approval_request WHERE run_id=%s", (r_decided,))
            assert cur.fetchone()[0] == "approved"
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM approval_request WHERE run_id LIKE %s", (mark + "%",))
            cur.execute("DELETE FROM agent_run WHERE run_id LIKE %s", (mark + "%",))
        conn.close()
