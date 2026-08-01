# -*- coding: utf-8 -*-
"""阶段 B WP2：归属轴写路径——raw-key 命名空间 / 摄取覆写守卫 / meta 投影 outbox CAS。

codex 点名的证据（MISSING EVIDENCE 对应项）：
  · node raw-key → scanner 不生成 dept:"unknown"、不把 node-<id> 补进 dept；
  · _dept_from_raw_key 语义不变（图片路径消费方拿第 2 段原文）；
  · meta outbox：DAG 失败/中途复活（generation 变）时 processed 不得推进。
"""
import pytest

from opensearch_pipeline.kb_upload import (
    build_raw_key,
    node_storage_segment,
    parse_raw_owner,
)


# ── parse_raw_owner / node_storage_segment ───────────────────────────────────
def test_node_segment_roundtrip():
    key = build_raw_key(node_storage_segment(599318766), "D1", "U1", "a.pdf",
                        permission_level="dept_internal")
    assert key.startswith("raw/node-599318766/internal/D1/U1/")
    ro = parse_raw_owner(key)
    assert ro == {"mode": "node", "storage_segment": "node-599318766",
                  "legacy_owner": None, "owner_dept_id": 599318766}


def test_legacy_key_parses_legacy():
    ro = parse_raw_owner("raw/production/internal/D1/U1/a.pdf")
    assert ro["mode"] == "legacy" and ro["legacy_owner"] == "production"
    assert ro["owner_dept_id"] is None


@pytest.mark.parametrize("seg", ["node-0", "node-01", "node--3", "node-", "node-x",
                                 "node-1x", "Node-5", "nodee-5"])
def test_non_canonical_node_segments_stay_legacy(seg):
    """严格正整数规范形：前导零/符号/非数字一律不当 node（路径段是安全输入面）。"""
    ro = parse_raw_owner(f"raw/{seg}/D1/U1/a.pdf")
    assert ro["mode"] == "legacy" and ro["legacy_owner"] == seg


def test_non_raw_prefix_all_empty():
    ro = parse_raw_owner("processing/assets/hr/D1/v1/img.png")
    assert ro == {"mode": "legacy", "storage_segment": "",
                  "legacy_owner": None, "owner_dept_id": None}


def test_node_storage_segment_rejects_root_and_bad():
    with pytest.raises(ValueError):
        node_storage_segment(1)
    with pytest.raises(ValueError):
        node_storage_segment(0)


def test_dept_from_raw_key_semantics_unchanged_for_node_segment():
    """图片对象路径等 8 处消费方拿第 2 段原文当 storage_segment——node 段原样返回。"""
    from opensearch_pipeline.pipeline_nodes import _dept_from_raw_key
    assert _dept_from_raw_key("raw/node-42/D1/U1/a.pdf") == "node-42"
    assert _dept_from_raw_key("raw/hr/D1/U1/a.pdf") == "hr"
    assert _dept_from_raw_key("s3://x/y", "unknown") == "unknown"


# ── scanner 路径补齐：node 任务绝不落 "unknown"/"node-<id>" 进 dept ─────────────
def test_scanner_fillin_node_key_sets_mode_not_dept(monkeypatch):
    from opensearch_pipeline.pipeline_nodes import node_scan_raw_files
    ctx = {"raw_tasks": [{
        "doc_id": "D1", "version_no": 1, "bucket_name": "b",
        "raw_key": "raw/node-599318766/internal/D1/U1/手册.pdf",
    }], "simulate": True}
    node_scan_raw_files(ctx)
    task = ctx["tasks"][0] if "tasks" in ctx else ctx["raw_tasks"][0]
    assert task["acl_mode"] == "node" and task["owner_dept_id"] == 599318766
    assert task["dept"] == "" and task["filename"] == "手册.pdf"


def test_scanner_fillin_legacy_key_unchanged(monkeypatch):
    from opensearch_pipeline.pipeline_nodes import node_scan_raw_files
    ctx = {"raw_tasks": [{
        "doc_id": "D2", "version_no": 1, "bucket_name": "b",
        "raw_key": "raw/hr/D2/U1/规章.pdf",
    }], "simulate": True}
    node_scan_raw_files(ctx)
    task = ctx["tasks"][0] if "tasks" in ctx else ctx["raw_tasks"][0]
    assert task.get("acl_mode") != "node" and task["dept"] == "hr"


# ── meta 投影 outbox：成功才 CAS 完成，复活行不被旧轮擦掉 ─────────────────────
class _ObCur:
    def __init__(self, gen_now=5):
        self.gen_now = gen_now       # 行的当前 generation（complete 时 CAS 比对）
        self.writes = []
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if s.startswith("UPDATE") and "processed_at = NOW()" in s:
            rid, gen = args
            self.writes.append(("complete", rid, gen))
            self.rowcount = 1 if gen == self.gen_now else 0   # CAS
        elif s.startswith("UPDATE"):
            self.writes.append(("other", s))
            self.rowcount = 1
        self._rows = []

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ObConn:
    def __init__(self, cur):
        self._c = cur
        self.committed = False

    def cursor(self):
        return self._c

    def commit(self):
        self.committed = True

    def close(self):
        pass


def test_complete_meta_projection_cas(monkeypatch):
    """generation 相符 → completed；窗口中途再编辑（generation 已 +1）→ superseded，
    行保持待处理（下轮按新代次重投影，丢更新关死）。"""
    from opensearch_pipeline import db
    from opensearch_pipeline.access_grants import complete_meta_projection
    cur = _ObCur(gen_now=5)
    monkeypatch.setattr(db, "_get_db_conn", lambda: _ObConn(cur))
    out = complete_meta_projection([(1, "D1", 5), (2, "D2", 4)])
    assert out["completed"] == 1 and out["superseded"] == 1 and not out["errors"]


def test_complete_meta_projection_empty_noop(monkeypatch):
    from opensearch_pipeline import db
    from opensearch_pipeline.access_grants import complete_meta_projection

    def _boom():
        raise AssertionError("空 claims 不应连库")

    monkeypatch.setattr(db, "_get_db_conn", _boom)
    assert complete_meta_projection([]) == {"completed": 0, "superseded": 0, "errors": []}
