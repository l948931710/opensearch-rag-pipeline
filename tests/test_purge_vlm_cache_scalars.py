# -*- coding: utf-8 -*-
"""D1 清理脚本的安全性钉子（scratch/purge_vlm_cache_scalars_20260725.py）。

这个脚本会删真实缓存行，且删除结果会在下一次开镜像的真实运行中被**整库推到 OSS**，
所以每条安全属性都要有测试守着：dry-run 零删除、备份不可覆盖、备份必须过
integrity_check、只删非 object、全程零 OSS 调用。
"""
import importlib.util
import json
import os
import sqlite3

import pytest

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scratch", "purge_vlm_cache_scalars_20260725.py")


@pytest.fixture(scope="module")
def purge_mod():
    spec = importlib.util.spec_from_file_location("purge_vlm_cache_scalars", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk_db(tmp_path, name="vlm_cache.sqlite3"):
    """3 条合法 dict + 3 条脏标量（覆盖现网见到的三种 key 形态）。"""
    p = tmp_path / name
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE vlm_cache (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    good = {"status": "ROUTE_TO_VECTOR", "visual_summary": "s", "ocr_text": "",
            "width": 1, "height": 1}
    rows = [
        ("a" * 64 + ":pub:2", json.dumps(good)),
        ("b" * 64 + ":pub:2", json.dumps(good)),
        ("c" * 32 + ":pub", json.dumps(good)),
        ("d" * 64 + ":pub:2", "5421"),          # 现网活路径上的脏标量
        ("e" * 32 + ":pub", "3020"),            # MD5 时代、当前不可达
        ("f" * 32, "2208"),                     # 裸 key
    ]
    con.executemany("INSERT INTO vlm_cache (k, v) VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return str(p)


def _count(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM vlm_cache").fetchone()[0]
    finally:
        con.close()


def test_survey_classifies_without_writing(purge_mod, tmp_path):
    db = _mk_db(tmp_path)
    before = os.path.getmtime(db)
    total, bad, shapes = purge_mod.survey(db)
    assert total == 6 and len(bad) == 3
    assert {s[0] for s in shapes} == {"pub:2", "pub", "<bare>"}
    assert {s[1] for s in shapes} == {64, 32}          # SHA-256 与 MD5 分得开
    assert os.path.getmtime(db) == before, "盘点必须只读"


def test_dry_run_deletes_nothing(purge_mod, tmp_path, capsys):
    db = _mk_db(tmp_path)
    rc = purge_mod.main(["--db", db])
    assert rc == 0
    assert _count(db) == 6, "dry-run 不许删任何行"
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "未删除任何行" in out
    assert "OSS" in out, "OSS 传播提醒必须出现在执行前摘要"


def test_apply_requires_explicit_db(purge_mod, tmp_path):
    """--apply 不给 --db 必须报错退出，杜绝误清会被真实运行复用的默认库。"""
    with pytest.raises(SystemExit) as ei:
        purge_mod.main(["--apply"])
    assert ei.value.code != 0


def test_apply_deletes_only_non_object_rows(purge_mod, tmp_path, capsys):
    db = _mk_db(tmp_path)
    rc = purge_mod.main(["--db", db, "--apply"])
    assert rc == 0
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = dict(con.execute("SELECT k, v FROM vlm_cache"))
    finally:
        con.close()
    assert len(rows) == 3, "只应剩下 3 条合法 dict"
    assert all(isinstance(json.loads(v), dict) for v in rows.values())
    out = capsys.readouterr().out
    assert "OSS" in out, "结束处也必须再提示 OSS 传播"


def test_backup_is_verified_and_never_overwritten(purge_mod, tmp_path):
    db = _mk_db(tmp_path)
    b1 = purge_mod.backup_and_verify(db)
    b2 = purge_mod.backup_and_verify(db)
    assert b1 != b2, "重复执行必须落到新序号，绝不覆盖既有备份"
    assert os.path.exists(b1) and os.path.exists(b2)
    for b in (b1, b2):
        con = sqlite3.connect(f"file:{b}?mode=ro", uri=True)
        try:
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert con.execute("SELECT COUNT(*) FROM vlm_cache").fetchone()[0] == 6
        finally:
            con.close()


def test_backup_survives_uncheckpointed_wal(purge_mod, tmp_path):
    """裸 cp 会漏掉尚未 checkpoint 的 WAL 内容 —— online backup API 不会。

    这条是选型理由本身的钉子：WAL 模式下写入后不 checkpoint，备份仍须包含新行。
    """
    db = _mk_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO vlm_cache (k, v) VALUES (?, ?)", ("z" * 64 + ":pub:2", "9999"))
    con.commit()          # 落 WAL，但不 checkpoint
    try:
        b = purge_mod.backup_and_verify(db)
        chk = sqlite3.connect(f"file:{b}?mode=ro", uri=True)
        try:
            assert chk.execute(
                "SELECT COUNT(*) FROM vlm_cache WHERE k LIKE 'zzz%'").fetchone()[0] == 1
        finally:
            chk.close()
    finally:
        con.close()


def test_script_makes_no_oss_calls(purge_mod, tmp_path, monkeypatch):
    """脚本自身零 OSS 调用（传播风险只来自后续真实运行的整库镜像，不来自这里）。"""
    import opensearch_pipeline.clients as clients
    called = []
    monkeypatch.setattr(clients, "_get_oss_bucket",
                        lambda *a, **k: called.append(1) or (None, True))
    db = _mk_db(tmp_path)
    purge_mod.main(["--db", db, "--apply"])
    assert called == []
    src = open(_SCRIPT, encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    body = code.split('"""', 2)[-1]        # 去掉模块 docstring（里面会提到 OSS）
    for token in ("oss2", "_get_oss_bucket", "put_object", "OSS_MIRROR_KEY"):
        assert token not in body, f"脚本正文不得出现 {token}"


def test_module_import_does_not_touch_default_db(purge_mod):
    """import 本身零副作用：DEFAULT_DB 只是常量，不在导入期打开。"""
    assert purge_mod.DEFAULT_DB.endswith(os.path.join("scratch", "vlm_cache.sqlite3"))
    assert purge_mod.TABLE == "vlm_cache"
