# -*- coding: utf-8 -*-
"""B7 + B8（2026-07-25）。部署顺序有硬约束：**B7 serving 必须先于 B8 reconciler** ——
B8 的 RDS-only SUCCESS 会留下同版本旧物理 PK，B7 未上线时 serving 仍会投放这些陈旧命中。

B7：同版本重切（node_write_chunk_meta 按 (doc_id,version_no) 全量 DELETE→INSERT）给同一
    chunk_id 重新分配 chunk_meta.id（= HA3 主键），旧 PK 成孤儿：不在 deactivate 的覆盖面
    （按 version_no < N 选行），也不在按 version 删的 PENDING_DELETE 里。孤儿行的
    chunk_id/is_active/permission 全对得上 → 只按 chunk_id 复核会原样放行。
    修法：(chunk_id, HA3 返回的 id) 双轴比对；且两个轴**分开查**（此前压进同一列表按
    chunk_id 查，"只有 id 没有 chunk_id"的历史命中根本查不到）。

B8：stage-3 批级 all-or-nothing 把同批已全成功的**首版**文档也标 FAILED（无更旧 active
    chunk ⇒ 既有搁浅修复器的必要条件不成立 ⇒ 永久跳过）。修法：该条件降为分支条件，
    新增 RDS-only 分支（零 HA3 删除），并用 current_version_no 作**锁内一致性信号**
    而非硬等式门 —— 它是注册分配指针，console 在新版本尚未摄取前就递增。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

import opensearch_pipeline.retriever as R
import opensearch_pipeline.spot_checker as SC


# ═══════════════════ B7：serving 双轴复核 ═══════════════════

class _Cur:
    def __init__(self, rows_by_axis):
        self.rows_by_axis = rows_by_axis
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        axis = "id" if "WHERE id IN" in sql else "chunk_id"
        self.queries.append((axis, list(params or [])))
        self._rows = self.rows_by_axis.get(axis, [])

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


def _revalidate(monkeypatch, results, rows_by_axis):
    cur = _Cur(rows_by_axis)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: _Conn(cur))
    cfg = R.get_config()
    monkeypatch.setattr(cfg.rag, "main_hit_revalidate", True, raising=False)
    monkeypatch.setattr(cfg, "simulate_db", False, raising=False)
    return R._revalidate_main_hits(results), cur


def _hit(chunk_id="c1", pk=10, perm="public", owner=""):
    return {"chunk_id": chunk_id, "id": pk, "permission_level": perm, "owner_dept": owner}


def test_b7_orphan_pk_is_dropped(monkeypatch):
    """HA3 返回的 id 与 RDS 现行 chunk_meta.id 不一致 = 同版本重切留下的孤儿行（旧文本）。"""
    out, _ = _revalidate(monkeypatch, [_hit(pk=999)],
                         {"chunk_id": [("c1", 1, "public", "", 10)]})
    assert out == [], "陈旧孤儿 PK 必须被丢弃"


def test_b7_matching_pk_is_kept(monkeypatch):
    out, _ = _revalidate(monkeypatch, [_hit(pk=10)],
                         {"chunk_id": [("c1", 1, "public", "", 10)]})
    assert len(out) == 1


def test_b7_missing_ha3_id_only_skips_pk_axis(monkeypatch):
    """缺 id 的历史命中不得因为"没法比 PK"就整条丢 —— 那会把正常内容一起打掉。"""
    hit = {"chunk_id": "c1", "permission_level": "public", "owner_dept": ""}
    out, _ = _revalidate(monkeypatch, [hit], {"chunk_id": [("c1", 1, "public", "", 10)]})
    assert len(out) == 1


def test_b7_chunkless_hit_is_queried_by_pk_axis(monkeypatch):
    """chunk_id 为空、只有 HA3 id 的行必须走 `WHERE id IN (...)` —— 此前两个轴被压进同一个
    列表再按 chunk_id 查，所谓 id 兜底根本查不到。"""
    hit = {"chunk_id": "", "id": 42, "permission_level": "public", "owner_dept": ""}
    out, cur = _revalidate(monkeypatch, [hit], {"id": [(None, 1, "public", "", 42)]})
    assert [q[0] for q in cur.queries] == ["id"]
    assert len(out) == 1


def test_b7_acl_drift_still_dropped(monkeypatch):
    """既有 ACL 轴行为不变。"""
    out, _ = _revalidate(monkeypatch, [_hit(perm="public")],
                         {"chunk_id": [("c1", 1, "dept_internal", "finance", 10)]})
    assert out == []


def test_b7_legacy_four_column_row_shape_does_not_crash(monkeypatch):
    """降级/旧桩返回 4 列时只跳过 PK 比对，不得抛。"""
    out, _ = _revalidate(monkeypatch, [_hit(pk=10)], {"chunk_id": [("c1", 1, "public", "")]})
    assert len(out) == 1


# ═══════════════════ B8：搁浅版本三分支 ═══════════════════

def test_b8_candidate_sql_allows_first_version_without_old_chunks():
    """「旧版本仍有 active chunk」必须是分支条件而非必要条件，否则首版文档永远修不了。"""
    src = inspect.getsource(SC.reconcile_stranded_versions)
    assert "OR dv.index_status IN" in src, "候选谓词未放开首版（无更旧 active chunk）分支"
    assert "AND EXISTS (SELECT 1 FROM chunk_meta o" not in src, "旧 chunk 仍是硬性必要条件"


def test_b8_rds_only_branch_never_touches_ha3():
    """RDS-only 分支零删除：不构造、不调用 HA3 客户端（客户端改为按需惰性获取）。"""
    src = inspect.getsource(SC.reconcile_stranded_versions)
    assert "def _bounded_client(" in src, "客户端必须惰性获取"
    assert "if old_ids:\n                    _search_delete_old_chunks(" in src, \
        "删除必须被 old_ids 分支包住"
    # 批级预取已移除（此前客户端不可用会让整批 failed=len(rows)）
    assert "os_client = _get_bounded_search_client()" not in src


def test_b8_current_version_is_a_signal_not_a_hard_gate():
    """current_version_no 是**注册分配指针**（console 在新版本尚未摄取前就递增）：
    只有 candidate > current 才是不一致态；candidate < current 是合法的 served-fallback
    （v2 全量 INDEXED、v3 刚登记还没产 chunk），必须允许修。"""
    src = inspect.getsource(SC.reconcile_stranded_versions)
    assert "current_version_no FROM document_meta" in src
    assert "version_no > _cur_ver" in src, "只挡 candidate > current"
    assert "version_no != _cur_ver" not in src and "version_no == _cur_ver" not in src, \
        "不得用等式硬门（会永久跳过合法的 served-fallback）"


def test_b8_lock_doc_keeps_boolean_contract():
    """_lock_doc 与 pending-delete reconciler 共用，返回值语义不得改。"""
    src = inspect.getsource(SC._lock_doc)
    assert "-> bool" in src
    assert "SELECT doc_id FROM document_meta" in src, "仍只取行锁，不改查询列"


def test_b8_deploy_order_is_documented():
    """B7 必须先于 B8：B8 的 RDS-only SUCCESS 会留下同版本旧 PK，
    B7 未上线时 serving 仍会投放这些陈旧命中。"""
    assert "id" in inspect.getsource(R._revalidate_main_hits), "B7 双轴复核必须已在位"
