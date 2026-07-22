# -*- coding: utf-8 -*-
"""Majors γ6(M7-lite) audit_digest——canonical/Merkle/HMAC 链/水位追赶契约(2026-07-21 codex 共识)。

- 纯函数：canonical 确定性、Merkle(空/奇/偶)、验签往返+改一字节即破、key_id 轮换；
- gate：flag off(默认)→skipped/exit 0；simulate→skipped；key 缺失→error(exit 3,
  fail-closed 绝不产未签名产物)；
- 追赶(fake bucket+fake conn)：首跑从最早行北京日封到最近可结算日、prev_digest 成链、
  重跑水位幂等零重封；篡改历史文件→verify 侧断链可检；
- 午夜宽限窗：00:00-00:30 不结算昨日。
M7 状态=OPEN：本模块是检测层缓解(当日未封窗口不在检测面)。
"""
import hashlib
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from opensearch_pipeline import audit_digest, ops_monitor
from opensearch_pipeline.audit_digest import (
    TABLE,
    build_day_manifest,
    canonical_bytes,
    key_id_of,
    latest_settleable_day,
    merkle_root,
    row_hash,
    run_audit_digest,
    sign_manifest,
    verify_manifest_signature,
)


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv("RAG_AUDIT_DIGEST_ENABLE", raising=False)
    monkeypatch.delenv("RAG_AUDIT_HMAC_KEY", raising=False)


# ── 纯函数 ───────────────────────────────────────────────────────────────────


def test_canonical_and_row_hash_deterministic():
    a = {"b": 1, "a": "中"}
    b = {"a": "中", "b": 1}
    assert canonical_bytes(a) == canonical_bytes(b)          # 键序无关
    assert row_hash(a) == row_hash(b)
    assert row_hash({"a": "中", "b": 2}) != row_hash(a)


def test_merkle_shapes():
    e1 = merkle_root([], table=TABLE, day="2026-07-01")
    e2 = merkle_root([], table=TABLE, day="2026-07-02")
    assert e1 != e2                                          # 空日按日区分
    h = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(3)]
    odd = merkle_root(h, table=TABLE, day="d")
    # 奇数复制末叶 ⇒ 与 [h0,h1,h2,h2] 等价
    assert odd == merkle_root(h + [h[2]], table=TABLE, day="d")
    assert merkle_root(h[:1], table=TABLE, day="d") == h[0]  # 单叶=自身
    assert merkle_root(list(reversed(h)), table=TABLE, day="d") != odd   # 行序敏感


def test_sign_verify_and_tamper():
    m = sign_manifest({"schema": 1, "table": TABLE, "date": "2026-07-01",
                       "count": 2, "merkle_root": "r", "prev_digest": None,
                       "key_id": key_id_of("k1"), "generated_at": "t"}, "k1")
    assert verify_manifest_signature(m, "k1")
    assert not verify_manifest_signature(m, "k2")            # 错 key
    bad = dict(m)
    bad["count"] = 3
    assert not verify_manifest_signature(bad, "k1")          # 改内容即破


def test_key_rotation_ids_differ():
    assert key_id_of("old") != key_id_of("new")
    assert len(key_id_of("x")) == 16


def test_grace_window():
    assert latest_settleable_day(datetime(2026, 7, 22, 0, 10)) == "2026-07-20"   # 宽限窗内退一天
    assert latest_settleable_day(datetime(2026, 7, 22, 0, 45)) == "2026-07-21"
    assert latest_settleable_day(datetime(2026, 7, 22, 12, 0)) == "2026-07-21"


# ── gate ─────────────────────────────────────────────────────────────────────


def test_flag_off_skipped_exit_0():
    rep = run_audit_digest(alert=False)
    assert rep.get("skipped") is True
    assert ops_monitor._job_exit("audit_digest", rep) == 0
    assert "audit_digest" in ops_monitor._JOBS


def test_simulate_skipped(monkeypatch):
    monkeypatch.setenv("RAG_AUDIT_DIGEST_ENABLE", "true")
    rep = run_audit_digest(alert=False)      # 测试环境 RAG_SIMULATE=true
    assert rep.get("skipped") is True and rep.get("reason") == "simulate"


def test_missing_key_fail_closed(monkeypatch):
    monkeypatch.setenv("RAG_AUDIT_DIGEST_ENABLE", "true")
    fake_cfg = SimpleNamespace(simulate=False, simulate_db=False, simulate_oss=False)
    monkeypatch.setattr("opensearch_pipeline.config.get_config", lambda: fake_cfg)
    rep = run_audit_digest(alert=False)
    assert "RAG_AUDIT_HMAC_KEY" in (rep.get("error") or "")
    assert ops_monitor._job_exit("audit_digest", rep) == 3


# ── 追赶（fake bucket + fake conn）────────────────────────────────────────────


class _FakeBucket:
    def __init__(self):
        self.store = {}

    def put_object(self, key, body):
        self.store[key] = bytes(body)

    def get_object(self, key):
        if key not in self.store:
            raise KeyError(key)
        return SimpleNamespace(read=lambda: self.store[key])

    def list_objects(self, prefix="", marker="", max_keys=1000):
        keys = sorted(k for k in self.store if k.startswith(prefix) and k > marker)
        page = keys[:max_keys]
        return SimpleNamespace(
            object_list=[SimpleNamespace(key=k) for k in page],
            is_truncated=len(keys) > max_keys,
            next_marker=(page[-1] if page else ""))


class _FakeCur:
    """按 SQL 形状分流：MIN(created_at) / 当日行 SELECT / SET time_zone。"""

    def __init__(self, days):
        self._days = days                     # {day: [row tuple,...]}
        self._rows = None
        self._min = None

    def execute(self, sql, params=None):
        if sql.startswith("SET SESSION"):
            return
        if "MIN(created_at)" in sql:
            self._min = min((d for d in self._days), default=None)
            return
        # 当日行查询：params 是太平洋区间——用起点反推北京日（bounds 起点恒前一日 08/09 时）
        from opensearch_pipeline.qa_rollup import _beijing_day_pacific_bounds
        for day in self._days:
            if _beijing_day_pacific_bounds(day)[0] == params[0]:
                self._rows = list(self._days[day])
                return
        self._rows = []

    def fetchone(self):
        if self._min is not None:
            return (datetime.strptime(_pac_start(self._min), "%Y-%m-%d %H:%M:%S"),)
        return (None,)

    def fetchall(self):
        return self._rows or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _pac_start(day):
    from opensearch_pipeline.qa_rollup import _beijing_day_pacific_bounds
    return _beijing_day_pacific_bounds(day)[0]


class _FakeConn:
    def __init__(self, days):
        self._days = days

    def cursor(self):
        return _FakeCur(self._days)

    def close(self):
        pass


def _audit_row(aid):
    # _AUDIT_COLS 15 列 + created_at 格式化串
    return (aid, "r1", 1, "req", "tool_execute", "kb_search", "READ_ONLY",
            "allow", None, "u1", None, None, "console", "d" * 8, None,
            f"2026-07-20T08:00:00.{aid[-6:]}")


def test_catch_up_seals_chain_and_is_idempotent(monkeypatch):
    days = {"2026-07-19": [_audit_row("a00001")],
            "2026-07-20": [_audit_row("b00001"), _audit_row("b00002")]}
    bucket = _FakeBucket()
    monkeypatch.setenv("RAG_AUDIT_HMAC_KEY", "test-key")
    monkeypatch.setattr(audit_digest, "latest_settleable_day", lambda now_bj=None: "2026-07-20")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _FakeConn(days))
    monkeypatch.setattr(audit_digest, "_op_db", lambda: "db", raising=False)
    fake_cfg = SimpleNamespace(rds=SimpleNamespace(operation_database="db"))
    monkeypatch.setattr("opensearch_pipeline.config.get_config", lambda: fake_cfg)

    rep = audit_digest._catch_up(bucket, "test-key")
    assert rep["ok"] is True and rep["sealed"] == ["2026-07-19", "2026-07-20"]
    k19, k20 = audit_digest._manifest_key("2026-07-19"), audit_digest._manifest_key("2026-07-20")
    m19 = json.loads(bucket.store[k19])
    m20 = json.loads(bucket.store[k20])
    assert m19["count"] == 1 and m20["count"] == 2
    assert m19["prev_digest"] is None
    assert m20["prev_digest"] == hashlib.sha256(bucket.store[k19]).hexdigest()   # 成链
    assert verify_manifest_signature(m19, "test-key")
    assert m19["key_id"] == key_id_of("test-key")

    rep2 = audit_digest._catch_up(bucket, "test-key")      # 水位幂等：无新完整日零重封
    assert rep2["ok"] is True and rep2["sealed"] == []

    # verify 侧断链可检：篡改 19 号文件 → 重算 sha ≠ 20 号 prev_digest
    bucket.store[k19] = bucket.store[k19] + b" "
    assert m20["prev_digest"] != hashlib.sha256(bucket.store[k19]).hexdigest()


def test_catch_up_recomputes_manifest_matches_db(monkeypatch):
    """封存产物与 DB 重算一致（verify 脚本③的核心等式）。"""
    rows = [_audit_row("c00001"), _audit_row("c00002")]
    days = {"2026-07-20": rows}
    bucket = _FakeBucket()
    monkeypatch.setenv("RAG_AUDIT_HMAC_KEY", "k")
    monkeypatch.setattr(audit_digest, "latest_settleable_day", lambda now_bj=None: "2026-07-20")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _FakeConn(days))
    fake_cfg = SimpleNamespace(rds=SimpleNamespace(operation_database="db"))
    monkeypatch.setattr("opensearch_pipeline.config.get_config", lambda: fake_cfg)
    rep = audit_digest._catch_up(bucket, "k")
    assert rep["sealed"] == ["2026-07-20"]
    m = json.loads(bucket.store[audit_digest._manifest_key("2026-07-20")])
    conn = _FakeConn(days)
    db_rows = audit_digest._fetch_day_rows(conn, "db", "2026-07-20")
    root = merkle_root([row_hash(r) for r in db_rows], table=TABLE, day="2026-07-20")
    assert m["merkle_root"] == root and m["count"] == len(db_rows)
    # build_day_manifest 与落盘同源（同 rows 同链锚 ⇒ 同根）
    m2 = build_day_manifest(db_rows, day="2026-07-20", prev_digest=None, key="k")
    assert m2["merkle_root"] == m["merkle_root"]
