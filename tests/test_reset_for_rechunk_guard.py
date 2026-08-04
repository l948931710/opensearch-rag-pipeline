# -*- coding: utf-8 -*-
"""reset_for_rechunk 的隔离守卫（2026-08-04 独立核验 B2）。

隔离件 reset + 非 orchestrator 裸跑是铸出 gate-only 态（列表徽章口径分叉）的唯一残余链；
守卫在链条第一步显式拒绝：--commit 遇隔离行（publish=QUARANTINED 或 gate=quarantined）
必须 SystemExit，除非显式 --include-quarantined。
"""
import importlib.util
import json
import pathlib
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "reset_for_rechunk.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("reset_for_rechunk_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *_a, **_k):
        pass

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def close(self):
        pass


def _run(monkeypatch, tmp_path, rows, argv_extra, env_ack=True):
    docfile = tmp_path / "docs.json"
    docfile.write_text(json.dumps([r["doc_id"] for r in rows]), encoding="utf-8")
    mod = _load_script_module()
    import opensearch_pipeline.prod_access as pa
    monkeypatch.setattr(pa, "get_prod_readonly_conn", lambda *a, **k: _Conn(rows))
    writes = {"n": 0}

    class _RwCur(_Cur):
        def execute(self, *_a, **_k):
            writes["n"] += 1
        rowcount = 1

    class _RwConn(_Conn):
        def cursor(self):
            return _RwCur(self._rows)

        def commit(self):
            pass

    monkeypatch.setattr(pa, "get_prod_rw_conn", lambda *a, **k: _RwConn(rows))
    if env_ack:
        monkeypatch.setenv("PROD_RW_ACK", "PROD-RW:2026-08-04")
    monkeypatch.setattr(sys, "argv",
                        ["reset_for_rechunk.py", "--docs", str(docfile), *argv_extra])
    return mod, writes


def _row(doc_id, publish=None, gate=None):
    return {"doc_id": doc_id, "version_no": 1, "content_process_status": "DONE",
            "chunk_status": "DONE", "index_status": "SUCCESS",
            "publish_status": publish, "gate_status": gate}


def test_commit_refuses_quarantined_rows(monkeypatch, tmp_path):
    """gate-only 与 publish 两种隔离形态，--commit 均硬拒且零写入。"""
    for row in (_row("D1", gate="quarantined"), _row("D2", publish="QUARANTINED")):
        mod, writes = _run(monkeypatch, tmp_path, [row], ["--commit"])
        with pytest.raises(SystemExit) as ei:
            mod.main()
        assert "REFUSED" in str(ei.value)
        assert writes["n"] == 0, "拒绝路径不得有任何写入"


def test_commit_include_quarantined_overrides(monkeypatch, tmp_path):
    """显式 --include-quarantined 才放行（操作员知情），且确实执行写入。"""
    mod, writes = _run(monkeypatch, tmp_path, [_row("D1", gate="quarantined")],
                       ["--commit", "--include-quarantined"])
    mod.main()
    assert writes["n"] == 1


def test_commit_clean_rows_unaffected(monkeypatch, tmp_path):
    """无隔离行时守卫零打扰（不改变既有工作流）。"""
    mod, writes = _run(monkeypatch, tmp_path, [_row("D1")], ["--commit"])
    mod.main()
    assert writes["n"] == 1


def test_preview_flags_quarantined_without_blocking(monkeypatch, tmp_path, capsys):
    """预览模式：标出隔离行但不拦（只读无害，且操作员需要先看见）。"""
    mod, _ = _run(monkeypatch, tmp_path, [_row("D1", gate="quarantined")], [], env_ack=False)
    mod.main()
    out = capsys.readouterr().out
    assert "QUARANTINED doc(s) in target set" in out and "DRY RUN" in out
