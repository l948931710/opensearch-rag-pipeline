# -*- coding: utf-8 -*-
"""#F-spot-unsafe — spot_checker.run_spot_check_pipeline 的 unsafe 兜底登记回归。

修复语义：安全复审返回 safety_status='unsafe' 但【未建议收紧权限】(suggested==current) 时，
原实现只看 _suggests_tightening() → unsafe 裁决被完全丢弃，文档继续留在索引可检索。
修复后即便权限未收紧，也在 review_task 登记一条 PENDING 人工审核任务
(review_type='spot_check_unsafe')，并把 report['unsafe_flagged'] +1。

本测试在不接触真实 DB / OpenSearch / 阿里云 SDK / LLM 的前提下，用 monkeypatch 把
_get_db_conn / _safety_review_llm / get_config / 三个对账任务全部替换，驱动到 unsafe elif 分支，
断言真正执行了 INSERT review_task。

为何对旧代码失败：旧实现没有 unsafe elif 分支，unsafe+未收紧的裁决被静默吞掉——
既不会执行任何 INSERT review_task，report 里也没有 'unsafe_flagged' 计数(=0 / KeyError)。
故下面对 INSERT 与计数的断言在修复前必然失败。
"""
import types

from opensearch_pipeline import spot_checker
from opensearch_pipeline import ha3_reconcile


# ── 伪 DB 驱动（cursor 作为上下文管理器；按 SQL 前缀路由 fetchall）─────────────

class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        # 归一化空白便于前缀匹配
        s = " ".join(sql.split())
        self.conn.executed.append((s, params))
        if s.startswith("SELECT dv.doc_id"):
            self._result = list(self.conn.docs_rows)
        elif "SELECT chunk_text FROM chunk_meta" in s:
            self._result = list(self.conn.chunk_rows)
        else:
            self._result = []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeConn:
    def __init__(self, docs_rows, chunk_rows):
        self.docs_rows = docs_rows      # [(doc_id, version_no, permission_level, title, owner_dept)]
        self.chunk_rows = chunk_rows    # [(chunk_text,)]
        self.executed = []              # [(normalized_sql, params)]
        self.began = 0
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def begin(self):
        self.began += 1

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


def _fake_config():
    llm = types.SimpleNamespace(
        api_key="test-key",
        model="gemini-3.1-flash-lite",
        api_base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    return types.SimpleNamespace(simulate=False, llm=llm)


def _patch_common(monkeypatch, conn, verdict):
    """把整条重路径外围依赖全部替换，只留裁决/登记核心逻辑真实执行。"""
    monkeypatch.setattr(spot_checker, "get_config", _fake_config)
    monkeypatch.setattr(spot_checker, "_get_db_conn", lambda **k: conn)
    # 三个前置对账任务：no-op（本项不测它们）
    monkeypatch.setattr(spot_checker, "reconcile_pending_deletes",
                        lambda: {"total": 0, "success": 0, "failed": 0, "errors": []})
    monkeypatch.setattr(spot_checker, "reconcile_stranded_versions",
                        lambda: {"total": 0, "success": 0, "failed": 0, "errors": []})
    monkeypatch.setattr(ha3_reconcile, "reconcile_ha3_orphan_pks",
                        lambda: {"checked": 0, "stale": 0, "deleted": 0, "errors": []})
    # 安全复审 LLM：直接返回既定裁决 dict（纯只读，替换后无任何 HTTP/SDK）
    monkeypatch.setattr(spot_checker, "_safety_review_llm",
                        lambda title, txt, **k: dict(verdict))


def _find_review_task_inserts(conn):
    return [(s, p) for (s, p) in conn.executed if s.startswith("INSERT INTO review_task")]


# ── 核心：unsafe + 权限未收紧 → 登记 PENDING review_task ─────────────────────

def test_unsafe_no_tightening_registers_pending_review_task(monkeypatch):
    # current == suggested == 'internal' → _suggests_tightening False → 命中 unsafe elif
    docs = [("docU", 7, "internal", "薪酬明细表", "hr")]
    chunks = [("这份文档包含全员薪酬明细，属于敏感信息。",)]
    conn = _FakeConn(docs, chunks)
    verdict = {
        "safety_status": "unsafe",
        "suggested_permission_level": "internal",   # 未收紧
        "reason": "contains full-company payroll PII",
    }
    _patch_common(monkeypatch, conn, verdict)

    report = spot_checker.run_spot_check_pipeline(limit_or_percent=0.05, simulate=False)

    # 1) 计数真正 +1（旧代码无此语义）
    assert report["unsafe_flagged"] == 1
    assert report["checked_documents"] == 1
    # 未收紧 → 不隔离
    assert report["mismatch_detected"] == 0
    assert report["quarantined_documents"] == []

    # 2) 真正执行了 INSERT review_task，且带 unsafe 类型 + PENDING 状态
    inserts = _find_review_task_inserts(conn)
    assert len(inserts) == 1, f"应恰好登记一条 review_task, 实际 executed={conn.executed}"
    sql, params = inserts[0]
    assert "'spot_check_unsafe'" in sql          # review_type 为字面量
    assert "'PENDING'" in sql                     # review_status
    # 3) 绑定参数：task_id / doc_id / version_no / owner_dept / suggested_permission
    assert params[0] == "spot_unsafe_docU_v7"     # task_id
    assert params[1] == "docU"                    # doc_id
    assert params[2] == 7                         # version_no
    assert "hr" in params                         # owner_dept 绑定进 params
    # 事务闭合：begin + commit，无 rollback
    assert conn.began >= 1 and conn.committed >= 1 and conn.rolled_back == 0


# ── 对照：safe + 未收紧 → 什么都不登记 ──────────────────────────────────────

def test_safe_no_tightening_registers_nothing(monkeypatch):
    docs = [("docS", 1, "internal", "普通说明", "eng")]
    chunks = [("一份可公开的普通说明文档。",)]
    conn = _FakeConn(docs, chunks)
    verdict = {
        "safety_status": "safe",
        "suggested_permission_level": "internal",
        "reason": "no sensitive content",
    }
    _patch_common(monkeypatch, conn, verdict)

    report = spot_checker.run_spot_check_pipeline(limit_or_percent=0.05, simulate=False)

    assert report["checked_documents"] == 1
    assert report["unsafe_flagged"] == 0
    assert report["mismatch_detected"] == 0
    assert _find_review_task_inserts(conn) == []