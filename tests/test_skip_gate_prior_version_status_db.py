# -*- coding: utf-8 -*-
"""skip-gate 的「prior version」选取是否看旧版本 status（真库，2026-08-06）。

## 被测的那一句

`pipeline_nodes.py:1459` 里 L2 skip-gate 选 prior version 用的是：

    SELECT version_no, canonical_sha256 FROM document_version
    WHERE doc_id=%s AND version_no<%s AND canonical_sha256 IS NOT NULL
    ORDER BY version_no DESC LIMIT 1

**没有 `status` 谓词**。命中即 `SKIPPED_DUPLICATE` + 把 `current_version_no`
**回退**到那个 prior 版本。于是问题是：当最近的那个 prior 是 `retired` / `rejected`
（即一个**不在服务**的版本）时，新版本会不会被它挡掉、指针会不会被拨到一个死版本上。

## 本模块只回答"如果出现会怎样"

"这个局面在生产里出不出得来"是另一个问题（要查现网历史，只读 prod-ro，未做）。
已知 console 路径上 F-37 会在 upload-url 阶段 409 拦住退役文档升版
（kb_console.py:2769），所以真实可达性存疑 —— 但可达性存疑不等于行为无害，
行为本身先钉死在这里。

🔴 **对照组**是本模块的核心：同一份 seed 只改 prior 的 `status`（`rejected` vs
`active`），两组结果必须**相同**才证明"确实不看 status"。只跑一组的话，
`SKIPPED_DUPLICATE` 可能只是因为 hash 相同，跟 status 毫无关系。

真库 + `RAG_SIMULATE_OSS=true`（skip 命中时根本不写 OSS，走不到上传）。
"""
import hashlib

import pytest

from tests.local_stack import requires_local_db

_DOC = "DOC_ITEST_SKIPGATE"
_TEXT = "本文正文完全没有变化。" * 8          # 需 >32 字符，避开跨文档去重的空文本短路
_SHA = hashlib.sha256(_TEXT.encode("utf-8")).hexdigest()


def _cleanup(cur):
    cur.execute("DELETE FROM chunk_meta WHERE doc_id=%s", (_DOC,))
    cur.execute("DELETE FROM document_version WHERE doc_id=%s", (_DOC,))
    cur.execute("DELETE FROM document_meta WHERE doc_id=%s", (_DOC,))


def _seed(cur, prior_status: str):
    """v1 = 带 hash 的 prior（status 由参数给）；v2 = 待处理的新版本，正文与 v1 逐字节相同。"""
    cur.execute(
        "INSERT INTO document_meta (doc_id,title,original_filename,owner_dept,status,"
        "current_version_no,permission_level) "
        "VALUES (%s,'skip-gate 用例','sg.txt','production','active',2,'dept_internal')", (_DOC,))
    cur.execute(
        "INSERT INTO document_version (doc_id,version_no,status,content_process_status,"
        "index_status,canonical_sha256) VALUES (%s,1,%s,'DONE','INDEXED',%s)",
        (_DOC, prior_status, _SHA))
    cur.execute(
        "INSERT INTO document_version (doc_id,version_no,status,content_process_status,"
        "index_status) VALUES (%s,2,'active','NOT_STARTED','NOT_INDEXED')", (_DOC,))


def _run_build_canonical(monkeypatch):
    """把 v2 送进 node_build_canonical（skip-gate 就在里面），返回 ctx。"""
    from opensearch_pipeline import pipeline_nodes
    from opensearch_pipeline.extraction.schema import ExtractionResult

    res = ExtractionResult(
        doc_id=_DOC, version_no=2, source_key=f"raw/production/{_DOC}/v2/sg.txt",
        file_ext="txt", extract_method="markdown", title="skip-gate 用例",
        text=_TEXT, text_length=len(_TEXT))
    ctx = {"extractions": [res], "bizdate": "20260806"}
    pipeline_nodes.node_build_canonical(ctx)
    return ctx


def _v2_state(cur):
    cur.execute("SELECT content_process_status, chunk_status FROM document_version "
                "WHERE doc_id=%s AND version_no=2", (_DOC,))
    row = cur.fetchone()
    cur.execute("SELECT current_version_no FROM document_meta WHERE doc_id=%s", (_DOC,))
    return {"v2_cps": row[0], "v2_chunk_status": row[1], "current_version_no": cur.fetchone()[0]}


@pytest.fixture
def _env(monkeypatch):
    # 🔴 skip-gate 整段挂在 `and not simulate_db` 上（pipeline_nodes.py:1451）——simulate 下
    # 它压根不执行，v2 会停在 NOT_STARTED。若不显式关掉 DB 模拟，`make test`（默认
    # RAG_SIMULATE=true）里本模块会：对照用例假红、反证锚**假绿**（两个都没命中 skip，
    # `!= SKIPPED_DUPLICATE` 自然成立）。库可达性由 requires_local_db 先行保证，
    # 所以这里关模拟是安全的，不是把测试硬拗成绿。
    monkeypatch.setenv("RAG_SIMULATE_DB", "false")
    monkeypatch.setenv("RAG_SIMULATE_API", "true")
    monkeypatch.setenv("RAG_SIMULATE_OSS", "true")
    monkeypatch.setenv("RAG_SKIP_UNCHANGED_REINGEST", "true")   # 生产默认开（pipeline_nodes:1467）
    monkeypatch.setenv("RAG_DEDUP_CROSS_DOC", "false")          # 隔离：只测 intra-doc 这道门
    from opensearch_pipeline import config as _cfg
    _cfg._config = None
    yield
    _cfg._config = None


def _observe(monkeypatch, prior_status: str) -> dict:
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            _cleanup(cur)
            _seed(cur, prior_status)
            conn.commit()
        _run_build_canonical(monkeypatch)
        with conn.cursor() as cur:
            state = _v2_state(cur)
            _cleanup(cur)
            conn.commit()
        return state
    finally:
        conn.close()


@requires_local_db
def test_skip_gate_ignores_prior_version_status(_env, monkeypatch):
    """对照实验：prior 是 `active` 还是 `rejected`，skip-gate 的判定必须**一模一样**。

    相同 ⇒ 证实那句 SELECT 确实不看 status（代码事实的行为确认）。
    不同 ⇒ 我读错了代码，本模块的前提作废。
    """
    active_prior = _observe(monkeypatch, "active")
    rejected_prior = _observe(monkeypatch, "rejected")

    assert active_prior == rejected_prior, (
        f"prior 的 status 改变了 skip-gate 结果：active={active_prior} rejected={rejected_prior}")
    # 非空证明：两组必须都真的命中了 skip，否则"相同"是两个都没命中的空相等
    assert active_prior["v2_cps"] == "SKIPPED_DUPLICATE", (
        f"同正文重传未命中 skip-gate，本对照无意义（flag 没生效？）：{active_prior}")
    assert active_prior["current_version_no"] == 1, (
        "命中 skip 必须把 current_version_no 回退到 prior —— 若 prior 恰好是个不在服务的"
        "版本（retired/rejected），这一步就是把指针拨到死版本上")


@requires_local_db
def test_skip_gate_still_requires_hash_match(_env, monkeypatch):
    """🔴 反证锚：正文不同则**不得** skip —— 证明上面的 SKIPPED_DUPLICATE 来自 hash 比对本身。"""
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            _cleanup(cur)
            # prior 的 hash 故意与 v2 正文不匹配
            cur.execute(
                "INSERT INTO document_meta (doc_id,title,original_filename,owner_dept,status,"
                "current_version_no,permission_level) "
                "VALUES (%s,'x','sg.txt','production','active',2,'dept_internal')", (_DOC,))
            cur.execute(
                "INSERT INTO document_version (doc_id,version_no,status,content_process_status,"
                "index_status,canonical_sha256) VALUES (%s,1,'rejected','DONE','INDEXED','deadbeef')",
                (_DOC,))
            cur.execute(
                "INSERT INTO document_version (doc_id,version_no,status,content_process_status,"
                "index_status) VALUES (%s,2,'active','NOT_STARTED','NOT_INDEXED')", (_DOC,))
            conn.commit()
        _run_build_canonical(monkeypatch)
        with conn.cursor() as cur:
            state = _v2_state(cur)
            _cleanup(cur)
            conn.commit()
        assert state["v2_cps"] != "SKIPPED_DUPLICATE", (
            f"hash 不同却被 skip ⇒ 上一条用例的 SKIPPED 不是 hash 比对的结果：{state}")
        assert state["current_version_no"] == 2, "未命中 skip 时不得回退版本指针"
    finally:
        conn.close()
