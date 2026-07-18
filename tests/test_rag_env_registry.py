# -*- coding: utf-8 -*-
"""B2（生产级外审 2026-07-17 P1-08/P2-17）：RAG_* 环境变量注册表守护。

① 清单↔源码双向一致（新增变量没登记 / 删了代码留悬空条目 → 红）；
② 未知名检测（拼错的 flag 可被发现）；③ 在册名不误报。
"""
from pathlib import Path

from opensearch_pipeline import rag_env_registry as reg

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_registry_matches_source_bidirectional():
    scanned = reg.scan_source_rag_vars(REPO_ROOT)
    missing = scanned - reg.KNOWN_RAG_ENV_VARS
    stale = reg.KNOWN_RAG_ENV_VARS - scanned - reg.MANUAL_EXTRAS
    assert not missing, (
        "新增 RAG_* 变量未登记（跑 python -m opensearch_pipeline.rag_env_registry "
        f"重生成清单）：{sorted(missing)}")
    assert not stale, f"注册表悬空条目（源码已删，重生成清单）：{sorted(stale)}"


def test_unknown_var_detected(monkeypatch):
    monkeypatch.setenv("RAG_TOTALLY_MISSPELLED_FLAG_X", "1")
    assert "RAG_TOTALLY_MISSPELLED_FLAG_X" in reg.unknown_rag_vars()


def test_known_vars_not_flagged(monkeypatch):
    monkeypatch.setenv("RAG_REQUIRE_AUTH", "true")
    monkeypatch.setenv("RAG_AGENT_DURABLE_DISPATCH", "true")
    u = reg.unknown_rag_vars()
    assert "RAG_REQUIRE_AUTH" not in u
    assert "RAG_AGENT_DURABLE_DISPATCH" not in u
