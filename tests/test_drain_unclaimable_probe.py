# -*- coding: utf-8 -*-
"""A3（2026-07-25）：drain 收尾必须报出"不可认领"的行，堵死 stage-2 假绿。

缺陷链条：`_count_pending_rows` 的谓词与认领谓词同源（这是必须的——不一致就会踩
ingest_policy 的「计得到、领不走 → 无进展守卫永久判死」陷阱），但这同时意味着**被楔住
的行对 drain 完全隐形**：stage-2 的批次级失败分支不回滚已认领行（对照 stage-3 有逐条
回滚），它们停在 LOADING/PROCESSING，于是下一次运行打印「drained: 0 pending rows」并
exit 0 —— 一边报绿、一边有整批文档楔死；三次批次级崩溃后 retry_count>=3，该行同时从
认领谓词与计数谓词消失，变成永久绿灯 + 永不入库。

本修复只做可见性：
  · 不改认领谓词（避免上述陷阱）；
  · 不改退出码（现网存量死信基线未知，贸然翻红会让每次运行都变红）；
  · count>0 时不得打印无保留的"全清"字样。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

from unittest.mock import patch

import opensearch_pipeline.dataworks_orchestrator as orch


class _ProbeCursor:
    """按 execute 顺序依次返回预设 COUNT(*)；也可指定某次抛错。"""

    def __init__(self, counts, boom_at=None):
        self.counts = list(counts)
        self.boom_at = boom_at
        self.sqls = []
        self._next = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sqls.append(" ".join(sql.split()))
        if self.boom_at is not None and len(self.sqls) == self.boom_at:
            raise RuntimeError("probe blew up")
        self._next = self.counts.pop(0) if self.counts else 0

    def fetchone(self):
        return (self._next,)


class _ProbeConn:
    def __init__(self, cursor):
        self._cur = cursor
        self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


def _patch_conn(cursor):
    return patch("opensearch_pipeline.pipeline_nodes._get_db_conn",
                 lambda **kw: _ProbeConn(cursor))


# ── 探针本身 ────────────────────────────────────────────────────────────────

def test_stage2_probe_reports_wedged_and_dead_rows():
    cur = _ProbeCursor([7, 2])
    with _patch_conn(cur):
        out = orch._probe_unclaimable_rows(2)
    assert [(lbl.split(" ")[0], cnt) for lbl, cnt, _hint in out] == [("stage-2", 7), ("stage-2", 2)]
    joined = " ".join(cur.sqls)
    # 楔住态：认领谓词与计数谓词都不含它们
    assert "content_process_status IN ('LOADING','PROCESSING')" in joined
    # 死信：retry_count 耗尽后同时从两个谓词消失，不查就永远没人发现
    assert "retry_count >= 3" in joined


def test_probe_omits_zero_counts():
    cur = _ProbeCursor([0, 0])
    with _patch_conn(cur):
        assert orch._probe_unclaimable_rows(2) == []


def test_stage1_has_no_probe_and_opens_no_connection():
    """stage-1 认领不写任何状态（"装好依赖重跑即全量自愈"的前提），无楔住态可查。"""
    def _boom(**kw):
        raise AssertionError("stage-1 不该开连接")

    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", _boom):
        assert orch._probe_unclaimable_rows(1) == []


def test_stage3_probe_covers_dead_chunks_and_held_locks():
    cur = _ProbeCursor([5, 1])
    with _patch_conn(cur):
        out = orch._probe_unclaimable_rows(3)
    assert [cnt for _l, cnt, _h in out] == [5, 1]
    joined = " ".join(cur.sqls)
    assert "index_status = 'DEAD'" in joined          # chunk_meta 词表
    assert "index_status = 'PROCESSING'" in joined    # document_version 词表（两套不相交）


def test_single_probe_failure_does_not_kill_the_rest(capsys):
    cur = _ProbeCursor([4], boom_at=1)        # 第一条探针炸（不消费计数），第二条仍要跑
    with _patch_conn(cur):
        out = orch._probe_unclaimable_rows(2)
    assert [cnt for _l, cnt, _h in out] == [4]


def test_probe_is_fail_open_when_db_unreachable():
    """纯可观测性：拿不到连接绝不影响入库结论。"""
    def _boom(**kw):
        raise RuntimeError("RDS unreachable")

    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", _boom):
        assert orch._probe_unclaimable_rows(2) == []


# ── drain 收尾的措辞 ────────────────────────────────────────────────────────

def _run_drain_stage2(probe_result):
    # A12 互斥锁显式关（test_cost_breaker 同款）：simulate=False 会真连 config.rds 取
    # GET_LOCK——CI 无本地 MySQL 必 sys.exit(1)（锁语义另有专测覆盖）。
    with patch.dict(os.environ, {"RAG_INGEST_SINGLETON_LOCK": "false"}), \
         patch.object(orch, "_count_pending_rows", lambda stage: 0), \
         patch.object(orch, "_reset_stale_stage2_locks", lambda: None), \
         patch.object(orch, "_probe_unclaimable_rows", lambda stage: probe_result), \
         patch.object(orch, "run_stage", lambda *a, **kw: None):
        orch.run_stage_drained(stage=2, bizdate="20260725", simulate=False)


def test_drain_does_not_claim_all_clear_when_rows_are_wedged(capsys):
    _run_drain_stage2([("stage-2 认领中未收口 (LOADING/PROCESSING)", 12, "上一次批次级失败残留")])
    out = capsys.readouterr().out
    assert "未全清" in out
    assert "12" in out
    assert "drained: 0 pending rows" not in out, "有楔住行时不得打印无保留的全清字样"


def test_drain_prints_all_clear_when_nothing_is_wedged(capsys):
    _run_drain_stage2([])
    out = capsys.readouterr().out
    assert "drained: 0 pending rows" in out
    assert "未全清" not in out


def test_drain_exit_semantics_unchanged_by_probe():
    """只做可见性：探针命中不得把正常收尾变成 raise（退出码语义不变）。"""
    _run_drain_stage2([("stage-2 死信 (FAILED 且 retry_count>=3)", 3, "耗尽预算")])
