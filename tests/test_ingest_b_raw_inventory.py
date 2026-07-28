# -*- coding: utf-8 -*-
"""B10 + B11（2026-07-25）：`raw/` 只读盘点探针（合并成一个作业，共享一次 LIST）。

B10：自助上传在 register 失败时留下 OSS 对象但无 document_version 行；
     register_new_files 对这类是 `continue` + 注释「孤儿存储由 GC 清理」，而全仓**没有**
     那个 GC；reconcile.run_raw_parity_check 是单向的（RDS→OSS），管不了反方向。
B11：scan_oss_sync_keys 的"未注册"计数既不过 ingest_policy 准入策略，db_raw_keys 又只从
     `status='active'` 构建（退役/被取代版本会被算成"新文件"），且那份报告无任何消费者。

合并的理由：两条的对象集合完全重叠，分开跑等于把全量 LIST 付两遍、对同一对象重复报警。
**只读只报数、绝不删**；且**尚未接调度**（接调度与告警阈值属 C1：先配 webhook 再加探针）。
"""
import datetime as dt
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

import opensearch_pipeline.raw_inventory as RI


class _Obj:
    def __init__(self, key, etag="", age_days=10.0):
        self.key = key
        self.etag = etag
        self.last_modified = (dt.datetime.now(dt.timezone.utc)
                              - dt.timedelta(days=age_days)).timestamp()


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cur(self.rows)

    def close(self):
        pass


def _run(monkeypatch, objects, rows):
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: object())
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn",
                        lambda **kw: _Conn(rows))
    import oss2
    monkeypatch.setattr(oss2, "ObjectIterator", lambda *a, **k: iter(objects))
    return RI.run_raw_inventory()


def test_single_list_shared_by_both_probes(monkeypatch):
    """B10 与 B11 的对象集合完全重叠 —— 必须只 LIST 一次。"""
    calls = {"n": 0}
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: object())
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn", lambda **kw: _Conn([]))
    import oss2

    def _iter(*a, **k):
        calls["n"] += 1
        return iter([])

    monkeypatch.setattr(oss2, "ObjectIterator", _iter)
    RI.run_raw_inventory()
    assert calls["n"] == 1


def test_registered_objects_are_not_reported(monkeypatch):
    key = "raw/production/DOC_A/UP1/x.pdf"
    out = _run(monkeypatch, [_Obj(key)], [(key, "DOC_A", "etag1")])
    assert out["counts"]["unregistered"] == 0
    assert out["counts"]["self_serve_orphan"] == 0


def test_registered_set_includes_inactive_versions(monkeypatch):
    """必须减 raw_key **全集** —— 只减 active 会把退役/被取代版本误报成"新文件"。"""
    src = inspect.getsource(RI.run_raw_inventory)
    assert "SELECT raw_key, doc_id, etag FROM document_version" in src
    assert "status = 'active'" not in src and "status='active'" not in src


def test_self_serve_orphan_bucket(monkeypatch):
    """自助上传路径形状 + 超过 upload token TTL 仍未登记。"""
    out = _run(monkeypatch, [_Obj("raw/production/DOC_X/UP9/x.pdf", age_days=5)], [])
    assert out["counts"]["self_serve_orphan"] == 1
    assert out["oldest_orphan_age_days"] and out["oldest_orphan_age_days"] >= 4.9


def test_expected_reject_bucket_needs_recomputable_evidence(monkeypatch):
    """B14 有意拒绝的对象（同 ETag 升版）不得报成"register 丢了"。
    判据必须可复算：key 里的 doc_id + 对象 ETag == 该文档已登记过的 ETag。"""
    key = "raw/production/DOC_A/UP2/x.pdf"
    out = _run(monkeypatch, [_Obj(key, etag='"same-etag"')],
               [("raw/production/DOC_A/UP1/x.pdf", "DOC_A", "same-etag")])
    assert out["counts"]["expected_reject"] == 1
    assert out["counts"]["self_serve_orphan"] == 0


def test_policy_rejected_bucket(monkeypatch):
    """归档/临时文件/旧格式等被准入策略挡掉的，不算"该注册未注册"。"""
    out = _run(monkeypatch, [_Obj("raw/_archive/old.doc"), _Obj("raw/production/~$tmp.docx")], [])
    assert out["counts"]["policy_rejected"] == 2
    assert out["counts"]["unregistered"] == 0


def test_unregistered_bucket_is_policy_aware(monkeypatch):
    out = _run(monkeypatch, [_Obj("raw/production/正经文档.pdf")], [])
    assert out["counts"]["unregistered"] == 1


def test_probe_is_read_only_and_never_deletes():
    src = inspect.getsource(RI)
    for forbidden in ("delete_object", "batch_delete", "DELETE FROM", "UPDATE "):
        assert forbidden not in src, f"盘点探针绝不能出现 {forbidden}"


def test_fail_open_on_any_error(monkeypatch):
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("oss down")))
    out = RI.run_raw_inventory()      # 不得抛
    assert out["ok"] is False and out["errors"]


def test_registered_in_ops_monitor_job_list():
    from opensearch_pipeline import ops_monitor
    assert "raw_inventory" in ops_monitor._JOBS
    src = inspect.getsource(ops_monitor.run_all)
    assert "run_raw_inventory" in src


def test_not_claimed_as_wired_monitoring():
    """现网节点只跑 --only reconcile_ha3 reconcile_oss —— 本作业未接调度，
    文档/注释不得宣称监控闭环（接调度与阈值属 C1：先配 webhook 再加探针）。"""
    assert "尚未接任何调度" in RI.__doc__
    node = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "dataworks_nodes", "ops_health_monitor_node.py"),
                encoding="utf-8").read()
    assert "raw_inventory" not in node, "接调度是 C1 的动作，不在本批"
