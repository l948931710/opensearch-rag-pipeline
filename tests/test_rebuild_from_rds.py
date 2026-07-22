# -*- coding: utf-8 -*-
"""scripts/rebuild_from_rds.py（P2-3 HA3 缺失 chunk 补救 runner）单测。

不变量：dry-run 默认绝不写；--commit 需 PROD_RW_ACK + env_guard 双门；写侧 UPDATE 重断言
资格谓词（is_active=1 AND index_status='INDEXED'）；资格闸绝不复活退役/隔离/删除在途的行；
parity 报告两种形态（顶层 {"ha3": ...} / 裸报告）都能解析。
"""
import importlib.util
import json
import os

import pytest

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "rebuild_from_rds.py")
_spec = importlib.util.spec_from_file_location("rebuild_from_rds", _SCRIPT)
rb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rb)


def _row(pk, doc="d1", *, active=1, status="INDEXED", doc_status="active",
         dv_status="SUCCESS", ver=1):
    return {"id": pk, "chunk_id": f"c{pk}", "doc_id": doc, "version_no": ver,
            "is_active": active, "index_status": status,
            "doc_status": doc_status, "dv_index_status": dv_status}


# ── parity 报告解析 ──

def test_targets_from_bare_and_wrapped_report():
    bare = {"rds_active_missing": [{"id": 7, "doc_id": "dA"}, {"id": 3, "doc_id": "dA"}],
            "vanished_docs": [{"doc_id": "dGone", "rds_active": 4, "ha3_kept": 0}]}
    assert rb._targets_from_parity_report(bare) == ([3, 7], ["dGone"])
    wrapped = {"ha3": bare, "oss": {"ok": True}}
    assert rb._targets_from_parity_report(wrapped) == ([3, 7], ["dGone"])


def test_targets_empty_on_error_or_skipped_report():
    assert rb._targets_from_parity_report({"error": "boom", "counts": {}}) == ([], [])
    assert rb._targets_from_parity_report({"skipped": "simulate"}) == ([], [])


def test_targets_fetch_mode_pk_precise_no_doc_expansion(capsys):
    """终局（2026-07-21）：fetch 定性成功 → 目标 = confirmed∪unclassified 的 PK 精确集；
    query_invisible（fetch 证实在场）坚决排除；doc 级扩张一律不用——1 confirmed 不得把
    同文档 99 个在场行拖下水整篇重推（codex 共识 blocker 2）。"""
    rep = {"rds_active_missing": [{"id": 1, "doc_id": "dA"}, {"id": 2, "doc_id": "dA"},
                                  {"id": 3, "doc_id": "dB"}],
           "vanished_docs": [{"doc_id": "dA", "rds_active": 3, "ha3_kept": 0}],
           "fetch_reclassify": {"ok": True, "missing_confirmed": [3], "unclassified": [2],
                                "query_invisible": [1], "fetch_errors": []}}
    pks, docs = rb._targets_from_parity_report(rep)
    assert pks == [2, 3] and docs == []
    out = capsys.readouterr().out
    assert "未定性" in out            # unclassified 目标必须先警告
    assert "已排除 1 个" in out       # invisible 排除可见


def test_targets_fetch_mode_pure_invisible_yields_nothing(capsys):
    """全部判缺 fetch 在场 → 无目标（重推是安慰剂）。"""
    rep = {"rds_active_missing": [{"id": 7, "doc_id": "dA"}],
           "vanished_docs": [],
           "fetch_reclassify": {"ok": True, "missing_confirmed": [], "unclassified": [],
                                "query_invisible": [7], "fetch_errors": []}}
    assert rb._targets_from_parity_report(rep) == ([], [])


def test_targets_query_fallback_warns_loudly(capsys):
    """无 fr（旧版报告）/ fr 失败 → 维持 query 单口径目标（灾备路径，写侧仍双门），
    但必须先醒目 WARNING。"""
    bare = {"rds_active_missing": [{"id": 5, "doc_id": "dV"}],
            "vanished_docs": [{"doc_id": "dV", "rds_active": 2, "ha3_kept": 0}]}
    assert rb._targets_from_parity_report(bare) == ([5], ["dV"])
    assert "query 单口径" in capsys.readouterr().out
    failed = dict(bare, fetch_reclassify={"ok": False, "error": "down"})
    assert rb._targets_from_parity_report(failed) == ([5], ["dV"])
    assert "query 单口径" in capsys.readouterr().out


# ── 资格闸分类 ──

def test_classify_eligibility_gates():
    rows = [
        _row(1),                                        # eligible
        _row(2, active=0),                              # 退役/隔离 chunk → 绝不复活
        _row(3, status="NOT_INDEXED"),                  # 本就可认领
        _row(4, status="FAILED"),                       # 本就可认领
        _row(5, status="DELETED"),                      # chunk 终态
        _row(6, doc_status="retired"),                  # 退役文档
        _row(7, dv_status="PENDING_DELETE"),            # HA3 删除在途
        _row(8, dv_status="PROCESSING"),                # stage-3 在跑
    ]
    v = rb.classify_candidates(rows)
    assert [r["id"] for r in v["eligible"]] == [1]
    assert v["skipped"] == {"inactive": 1, "already_claimable": 2, "chunk_status_other": 1,
                            "doc_not_active": 1, "version_deleting": 1, "stage3_running": 1}


# ── 假连接 ──

class _Cur:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        s = " ".join(sql.split())
        if s.startswith("SELECT"):
            self._rows = list(self._conn.rows)
        elif s.startswith("UPDATE"):
            self.rowcount = len(params or ())
            self._conn.updates += 1

    def fetchall(self):
        return getattr(self, "_rows", [])


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []
        self.updates = 0
        self.commits = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


# ── dry-run 默认绝不写 ──

def test_dry_run_previews_without_writing(monkeypatch, capsys):
    import opensearch_pipeline.prod_access as pa
    ro = _Conn([_row(1), _row(2, active=0)])
    monkeypatch.setattr(pa, "get_prod_readonly_conn", lambda *a, **k: ro)
    monkeypatch.setattr(pa, "get_prod_rw_conn",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run 绝不开 RW 连接")))
    assert rb.main(["--docs", "d1"]) == 0
    assert ro.updates == 0 and ro.commits == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "1 行" in out  # 只有 id=1 eligible


# ── commit：双门 + 写侧重断言谓词 ──

def test_commit_requires_prod_rw_ack(monkeypatch):
    import opensearch_pipeline.prod_access as pa
    monkeypatch.setattr(pa, "get_prod_readonly_conn", lambda *a, **k: _Conn([_row(1)]))
    monkeypatch.delenv("PROD_RW_ACK", raising=False)
    monkeypatch.delenv("RAG_PROD_RW_ACK", raising=False)
    with pytest.raises(SystemExit, match="PROD_RW_ACK"):
        rb.main(["--docs", "d1", "--commit"])


def test_commit_flips_indexed_to_not_indexed_with_reasserted_predicate(monkeypatch):
    import opensearch_pipeline.env_guard as eg
    import opensearch_pipeline.prod_access as pa
    ro = _Conn([_row(1), _row(2), _row(3, active=0)])
    rw = _Conn([])
    guard_calls = []
    monkeypatch.setattr(pa, "get_prod_readonly_conn", lambda *a, **k: ro)
    monkeypatch.setattr(pa, "get_prod_rw_conn", lambda *a, **k: rw)
    monkeypatch.setattr(eg, "assert_destructive_write_allowed",
                        lambda *a, **k: guard_calls.append((a, k)))
    monkeypatch.setenv("PROD_RW_ACK", "PROD-RW:2099-01-01")   # 令牌校验在被 mock 的 prod_access 侧
    assert rb.main(["--docs", "d1", "--commit"]) == 0
    assert guard_calls, "commit 前必须过 env_guard 破坏性写守卫"
    upd = [(s, p) for s, p in rw.executed if s.strip().startswith("UPDATE")]
    assert len(upd) == 1
    sql, params = upd[0]
    assert "index_status = 'NOT_INDEXED'" in sql
    # 写侧必须重断言资格谓词（TOCTOU 只朝少写偏）
    assert "is_active = 1" in sql and "index_status = 'INDEXED'" in sql
    assert params == (1, 2)          # inactive 的 id=3 绝不进写集
    assert rw.commits == 1


def test_from_reconcile_report_file(monkeypatch, tmp_path, capsys):
    import opensearch_pipeline.prod_access as pa
    report = {"ha3": {"rds_active_missing": [{"id": 1, "doc_id": "d1"}],
                      "vanished_docs": []}}
    p = tmp_path / "parity.json"
    p.write_text(json.dumps(report), encoding="utf-8")
    ro = _Conn([_row(1)])
    monkeypatch.setattr(pa, "get_prod_readonly_conn", lambda *a, **k: ro)
    assert rb.main(["--from-reconcile", str(p)]) == 0
    assert ro.updates == 0
    assert "1 个缺失 chunk PK" in capsys.readouterr().out


def test_from_reconcile_refuses_error_report(tmp_path):
    p = tmp_path / "parity.json"
    p.write_text(json.dumps({"ha3": {"error": "HA3 down", "counts": {}}}), encoding="utf-8")
    assert rb.main(["--from-reconcile", str(p)]) == 3


# ── P3-7：嵌入制度漂移选择器（embedding_version「只写不读」的读者半边）──

def test_stale_embedding_selector_targets_drifted_rows(monkeypatch, capsys):
    import opensearch_pipeline.prod_access as pa
    from opensearch_pipeline.config import get_config
    ro = _Conn([_row(1), _row(2)])
    monkeypatch.setattr(pa, "get_prod_readonly_conn", lambda *a, **k: ro)
    assert rb.main(["--stale-embedding"]) == 0
    # 选择谓词按行级真值列比对（embedding_model/dimension），有意不比 embedding_version
    # 指纹串——历史行是旧常量格式，按串比对会把全语料误判为陈旧
    sel = " ".join(ro.executed[0][0].split())
    assert "embedding_model" in sel and "embedding_dimension" in sel
    assert "embedding_version" not in sel
    emb = get_config().embedding
    assert ro.executed[0][1] == (emb.model, int(emb.dimension))
    assert ro.updates == 0                     # dry-run 默认绝不写
    assert "DRY RUN" in capsys.readouterr().out
