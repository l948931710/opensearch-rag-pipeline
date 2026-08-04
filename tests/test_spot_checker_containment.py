# -*- coding: utf-8 -*-
"""附录B — spot-check 隔离路径的陈旧快照 / 并发重分块封堵探测（2026-08-03）。

修复前：主连接 autocommit=False，run 起始的文档清单 SELECT 就开了事务，此后 phase ①②
（重构文本 + 分钟级并发 LLM 复审）都不 commit/rollback ⇒ REPEATABLE READ 下隔离 Phase-1
在**run 起始的读视图**上枚举 chunk PK，且全程不取锁。并发同版本 re-chunk（DELETE+INSERT，
新自增 id）后，HA3 里新旧两代 PK 可能并存（stage-2 只删 RDS 行；stage-3 旧版本清理谓词是
`version_no < N`；本文件的 orphan 对账是 dry_run=True），而隔离只删得到其中一代，
Phase-2 却无条件 commit 隔离终态并打印 ✅ ⇒ **RDS 报已隔离、HA3 仍以旧权限可检索**。

本轮是 **detector, not fence**（同 PK 复活 / 提交后写方 / 请求删除≠确认删除仍未关闭，
见 spot_checker 内的残余洞清单）。这里锁定三件事：
  1. 刷新读视图，但**删两代的并集** —— 只删刷新后的新 PK 反而会忘掉真正躺在 HA3 里的旧代；
  2. 权限按新鲜值重判（不是"一变就跳过"，那会新增假阴）；
  3. 检出未封堵的新 PK 时**不收回 RDS 封堵**（回滚会让新行退回 is_active=1、重新可被
     stage-3 推送 = 相对现状新增泄漏面），只收回"隔离成功"这个断言。
"""
import types

import pytest

from opensearch_pipeline import ha3_reconcile, spot_checker


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.conn.executed.append((s, params))
        if s.startswith("SELECT dv.doc_id"):
            self._result = list(self.conn.docs_rows)
        elif "SELECT chunk_text FROM chunk_meta" in s:
            self._result = [("正文",)]
        elif s.startswith("SELECT id FROM chunk_meta"):
            nth = len(self.conn.pk_reads_done)
            pks = self.conn.pk_reads[min(nth, len(self.conn.pk_reads) - 1)]
            self.conn.pk_reads_done.append(sorted(pks))
            self.conn.ops.append("pk_read_for_update" if "FOR UPDATE" in s else "pk_read")
            self._result = [(p,) for p in sorted(pks)]
        elif s.startswith("SELECT permission_level FROM document_meta"):
            self._result = [(self.conn.fresh_perm,)]
        else:
            self._result = []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeConn:
    def __init__(self, docs_rows, pk_reads, fresh_perm):
        self.docs_rows = docs_rows
        self.pk_reads = pk_reads          # 依次供 S_old / S_fresh / Phase-2 的 S2
        self.pk_reads_done = []
        self.fresh_perm = fresh_perm
        self.executed = []
        self.ops = []
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return _FakeCursor(self)

    def begin(self):
        self.ops.append("begin")

    def commit(self):
        self.committed += 1
        self.ops.append("commit")

    def rollback(self):
        self.rolled_back += 1
        self.ops.append("rollback")

    def close(self):
        pass


class _FakeHa3:
    """有 push_documents ⇒ 走 HA3 分支；记录每次删除请求里的 PK。"""
    def __init__(self):
        self.deleted = []

    def push_documents(self, table, pk_field, request):
        for d in request.body:
            self.deleted.append(d["fields"][pk_field])
        return types.SimpleNamespace(status_code=200)


def _fake_config():
    return types.SimpleNamespace(
        simulate=False,
        llm=types.SimpleNamespace(api_key="k", model="m", api_base_url="http://x"),
        alibaba_vector=types.SimpleNamespace(
            endpoint="ha3-test", instance_id="i", table_name="t", pk_field="id"),
        opensearch=types.SimpleNamespace(host="localhost", index_name="idx"),
    )


def _setup(monkeypatch, *, pk_reads, fresh_perm="public", current_perm="public",
           suggested="restricted"):
    from opensearch_pipeline import env_guard

    conn = _FakeConn([("docQ", 3, current_perm, "标题", "hr")], pk_reads, fresh_perm)
    client = _FakeHa3()
    monkeypatch.setattr(spot_checker, "get_config", _fake_config)
    monkeypatch.setattr(spot_checker, "_get_db_conn", lambda **k: conn)
    monkeypatch.setattr(spot_checker, "_get_opensearch_client", lambda: client)
    monkeypatch.setattr(env_guard, "assert_destructive_write_allowed",
                        lambda *a, **k: None)
    for name in ("reconcile_pending_deletes", "reconcile_stranded_versions"):
        monkeypatch.setattr(spot_checker, name,
                            lambda: {"total": 0, "success": 0, "failed": 0, "errors": []})
    monkeypatch.setattr(ha3_reconcile, "reconcile_ha3_orphan_pks",
                        lambda **kw: {"checked": 0, "stale": 0, "deleted": 0, "errors": []})
    monkeypatch.setattr(spot_checker, "_safety_review_llm", lambda title, txt, **k: {
        "safety_status": "unsafe", "suggested_permission_level": suggested,
        "reason": "PII"})
    return conn, client


def _run():
    return spot_checker.run_spot_check_pipeline(limit_or_percent=1.0, simulate=False)


# ── ① 陈旧快照：刷新后必须删【两代并集】，不能只删新鲜那一代 ────────────────────

def test_deletes_union_of_stale_and_fresh_pk_generations(monkeypatch):
    """S_old={1,2}（run 起始快照，正是此刻躺在 HA3 里的一代）、S_fresh={7,8}（重切后新一代）。

    只删 S_fresh ⇒ {1,2} 被永久遗忘在索引里（stage-3 的 `version_no < N` 清理够不着，
    orphan 对账在本文件 dry_run）；只删 S_old ⇒ 漏掉 {7,8}。必须两代都删。
    """
    conn, client = _setup(monkeypatch, pk_reads=[{1, 2}, {7, 8}, {7, 8}])
    _run()
    assert sorted(client.deleted) == [1, 2, 7, 8], (
        f"必须删新旧两代并集，实际删了 {sorted(client.deleted)}")


def test_read_view_refreshed_before_fresh_enumeration(monkeypatch):
    """rollback 必须发生在【第二次】PK 枚举之前——否则 S_fresh 仍是 run 起始快照的副本。"""
    conn, _ = _setup(monkeypatch, pk_reads=[{1, 2}, {7, 8}, {7, 8}])
    _run()
    first_pk = conn.ops.index("pk_read")
    rb = conn.ops.index("rollback", first_pk)
    second_pk = conn.ops.index("pk_read", rb)
    assert first_pk < rb < second_pk, f"ops 序列不对：{conn.ops}"


# ── ② 封堵探测：检出未删的新 PK ⇒ 不谎报已隔离，但也不撤销 RDS 封堵 ──────────────

def test_unsealed_pks_refuse_to_claim_quarantine(monkeypatch):
    """Phase-2 重读发现 {9} 从未被删 ⇒ 不进 quarantined_documents、报 error、计数 +1。"""
    conn, client = _setup(monkeypatch, pk_reads=[{1, 2}, {1, 2}, {1, 2, 9}])
    rep = _run()
    assert rep["quarantined_documents"] == [], "绝不谎报「已隔离」"
    assert rep["ha3_containment_unconfirmed"] == 1
    assert any("containment UNCONFIRMED" in e for e in rep["errors"])
    assert 9 not in client.deleted


def test_unsealed_detection_still_commits_rds_containment(monkeypatch):
    """codex BLOCKER：检出未封堵**不得回滚**。

    回滚会让并发新行退回 is_active=1、重新可被 stage-3 认领推送 —— 相对现状**新增**
    泄漏面。今天这笔事务的 chunk 停用 + QUARANTINED 是已生效的 RDS 侧封堵，必须保留；
    我们只收回「隔离成功」这个断言。
    """
    conn, _ = _setup(monkeypatch, pk_reads=[{1, 2}, {1, 2}, {1, 2, 9}])
    _run()
    assert conn.committed >= 1, "RDS 封堵必须照常提交"
    quarantine_writes = [s for s, _ in conn.executed if "QUARANTINED" in s]
    assert quarantine_writes, "chunk 停用 / 版本 QUARANTINED 必须仍然执行"
    reasons = [p for s, p in conn.executed if s.startswith("INSERT INTO review_task")]
    assert reasons and "CONTAINMENT UNCONFIRMED" in reasons[0][4], (
        "review_task 的 reason 必须写明封堵未确认")


def test_happy_path_unchanged(monkeypatch):
    """PK 集合未变 ⇒ 行为与修复前逐字一致：照常隔离、零 error。"""
    conn, client = _setup(monkeypatch, pk_reads=[{1, 2}, {1, 2}, {1, 2}])
    rep = _run()
    assert len(rep["quarantined_documents"]) == 1
    assert rep["quarantined_documents"][0]["doc_id"] == "docQ"
    assert rep.get("ha3_containment_unconfirmed", 0) == 0
    assert rep["errors"] == []
    assert sorted(client.deleted) == [1, 2]


# ── ③ 权限重判：不是"一变就跳过"（那会新增假阴）────────────────────────────────

def test_concurrently_tightened_permission_is_skipped(monkeypatch):
    """他人已把权限收紧到建议值 ⇒ 无需再隔离，且**绝不删索引**。"""
    conn, client = _setup(monkeypatch, pk_reads=[{1, 2}, {1, 2}, {1, 2}],
                          current_perm="public", fresh_perm="restricted",
                          suggested="restricted")
    rep = _run()
    assert rep["quarantined_documents"] == []
    assert rep["mismatch_detected"] == 0, "跳过时计数必须回退，否则报告虚高"
    assert client.deleted == [], "跳过分支不得触碰索引"


def test_partially_tightened_permission_still_quarantines(monkeypatch):
    """public→dept_internal 后 LLM 仍建议 restricted ⇒ 真缺口，**不得**因"权限变了"就跳过。

    这一条是 S-1b 的假阴防线：若实现写成"fresh != current 就 skip"，此例会静默放行。
    """
    conn, client = _setup(monkeypatch, pk_reads=[{1, 2}, {1, 2}, {1, 2}],
                          current_perm="public", fresh_perm="dept_internal",
                          suggested="restricted")
    rep = _run()
    assert len(rep["quarantined_documents"]) == 1
    assert rep["quarantined_documents"][0]["previous_permission"] == "dept_internal", (
        "报告里应记录**新鲜**的前权限，而不是 run 起始的陈旧值")
    assert sorted(client.deleted) == [1, 2]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
