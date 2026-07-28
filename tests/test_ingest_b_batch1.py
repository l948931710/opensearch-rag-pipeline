# -*- coding: utf-8 -*-
"""B1a（2026-07-25）：stage-2 状态机的三处收口 + retry_count 的 NULL 洞。

背景（B 档评审 codex 四轮共识）：B1 的**即时回滚**被整条推迟到 B1b —— 本仓有三条通道能把
LOADING/PROCESSING 行转手（2h 陈旧接管、reset_for_rechunk、scratch/reset_stuck），而
document_version 上没有 per-claim identity 列，任何"按 id + 状态"的回滚都存在 ABA：
旧 worker 可能把接管者的行打回去。所以本批只做**不需要所有权证明**的三件事：

  ① 两个成功出口清零 retry_count（语义 = 连续失败次数，不是累计）；
  ② 批次成功前**只读**残留断言 —— 0-chunk/explosion 收尾落库是 fail-open，DAG 可以"成功"
     而行仍留在处理中态，既不被认领谓词收也不在 pending 计数里 = 一边报绿一边楔死；
  ③ 四处 retry_count+1 统一走 COALESCE —— 该列 `INT DEFAULT 0` 无 NOT NULL，NULL+1 仍是
     NULL，转 FAILED 后既不满足 `retry_count<3` 也不进 `>=3` 死信探针（新的静默楔住）。
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

import pytest

import opensearch_pipeline.dataworks_orchestrator as orch
import opensearch_pipeline.pipeline_nodes as pn
from opensearch_pipeline.reindex_states import RETRY_COUNT_INC_SQL

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════ ③ COALESCE 覆盖四处 + 源码扫描钉死 ═══════════════════

def test_retry_inc_sql_uses_coalesce():
    assert RETRY_COUNT_INC_SQL == "retry_count = COALESCE(retry_count, 0) + 1"


def test_no_bare_retry_count_increment_anywhere():
    """全仓不得再出现裸 `retry_count + 1` —— 第五处日后长出来就会重新打开 NULL 洞。"""
    offenders = []
    for root in ("opensearch_pipeline", "dataworks_nodes", "scripts"):
        base = os.path.join(REPO, root)
        for dirpath, _dirs, files in os.walk(base):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                text = open(p, encoding="utf-8", errors="replace").read()
                # 允许 COALESCE 形式；只抓裸自增
                for m in re.finditer(r"retry_count\s*=\s*retry_count\s*\+\s*1", text):
                    ctx = text[max(0, m.start() - 60):m.start()]
                    if "COALESCE" not in ctx:
                        offenders.append(f"{os.path.relpath(p, REPO)}")
    assert not offenders, (
        f"裸 retry_count+1（NULL+1 仍是 NULL → 静默楔住）：{sorted(set(offenders))}；"
        f"请改用 reindex_states.RETRY_COUNT_INC_SQL")


@pytest.mark.parametrize("mod,fn_name", [
    (orch, "run_stage"),                       # canonical 取件失败
    (orch, "_reset_stale_stage2_locks"),       # 陈旧接管
    (pn, "node_classify_and_risk_assess"),     # 分类失败
    (pn, "node_write_chunk_meta"),             # 0-chunk 疑似失败
])
def test_all_four_increment_sites_use_shared_fragment(mod, fn_name):
    src = inspect.getsource(getattr(mod, fn_name))
    assert "RETRY_COUNT_INC_SQL" in src, f"{fn_name} 未走共享片段"


# ═══════════════════ ① 两个成功出口清零 ═══════════════════

def test_both_success_exits_clear_retry_count():
    src = inspect.getsource(pn.node_write_chunk_meta)
    # 普通 DONE
    m = re.search(r"SET content_process_status = 'DONE',.*?WHERE doc_id = %s AND version_no = %s",
                  src, re.DOTALL)
    assert m and "retry_count = 0" in m.group(0), "普通 DONE 出口未清零"
    # EMPTY/DONE（走动态 _retry_clause）
    assert '_chunk_status, _cps = "EMPTY", "DONE"' in src
    m2 = re.search(r'_chunk_status, _cps = "EMPTY", "DONE".*?_retry_clause = ([^\n]+)', src, re.DOTALL)
    assert m2 and "retry_count = 0" in m2.group(1), "EMPTY/DONE 出口未清零"


def test_failure_exit_still_increments_not_clears():
    """失败出口必须仍自增（清零只属于成功出口，否则毒文档永不进死信）。"""
    src = inspect.getsource(pn.node_write_chunk_meta)
    m = re.search(r'_chunk_status, _cps = "NEEDS_REVIEW", "FAILED".*?_retry_clause = ([^\n]+)',
                  src, re.DOTALL)
    assert m and "RETRY_COUNT_INC_SQL" in m.group(1)


# ═══════════════════ ② 残留断言 ═══════════════════

class _ResCursor:
    def __init__(self, rows=None, boom=False):
        self.rows, self.boom, self.sqls = rows or [], boom, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        if self.boom:
            raise RuntimeError("rds unreachable")

    def fetchall(self):
        return self.rows


class _ResConn:
    def __init__(self, cur):
        self.cur, self.closed = cur, False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _patch_conn(monkeypatch, cur):
    conn = _ResConn(cur)
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn", lambda **kw: conn)
    return conn


def test_residue_assertion_passes_when_clean(monkeypatch):
    cur = _ResCursor(rows=[])
    conn = _patch_conn(monkeypatch, cur)
    orch._assert_no_claimed_residue([1, 2, 3])       # 不得抛
    assert conn.closed
    assert "LOADING" in cur.sqls[0] and "PROCESSING" in cur.sqls[0]


def test_residue_assertion_is_read_only(monkeypatch):
    """刻意不写任何状态：即时回滚需要所有权证明（三条转手通道 + 无 claim identity），
    留作 B1b。这里只读就绝不会踩到接管者。"""
    cur = _ResCursor(rows=[])
    _patch_conn(monkeypatch, cur)
    orch._assert_no_claimed_residue([1])
    assert all(s.strip().upper().startswith("SELECT") for s in cur.sqls)


def test_residue_assertion_raises_on_wedged_rows(monkeypatch):
    cur = _ResCursor(rows=[(7, "DOC_X", 2, "PROCESSING")])
    _patch_conn(monkeypatch, cur)
    with pytest.raises(RuntimeError, match=r"still LOADING/PROCESSING"):
        orch._assert_no_claimed_residue([7])


def test_residue_assertion_is_fail_closed_on_query_error(monkeypatch):
    """查不出来 ≠ 没问题：这是安全断言，不是可观测探针。"""
    _patch_conn(monkeypatch, _ResCursor(boom=True))
    with pytest.raises(RuntimeError, match=r"could not run"):
        orch._assert_no_claimed_residue([1])


def test_residue_assertion_does_not_filter_by_active_status(monkeypatch):
    """并发退役但未收口的认领行同样要被看见 —— 不得加 status='active' 过滤。"""
    cur = _ResCursor(rows=[])
    _patch_conn(monkeypatch, cur)
    orch._assert_no_claimed_residue([1])
    assert "status = 'active'" not in cur.sqls[0] and "status='active'" not in cur.sqls[0]


def test_residue_assertion_wired_before_success_print():
    """必须在所有 closure 之后、打印成功之前。"""
    src = inspect.getsource(orch.run_stage)
    i_assert = src.index("_assert_no_claimed_residue(")
    i_print = src.index("Stage 2 successfully completed")
    assert i_assert < i_print
