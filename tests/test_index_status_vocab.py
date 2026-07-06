"""index_status 双词表耦合测试（同名列、不同表、不相交值集）。

背景：`chunk_meta.index_status` 的成功值是 **INDEXED**，`document_version.index_status`
的成功值是 **SUCCESS**。散落的裸字符串曾两次酿祸：2026-06-15 canary 把 'NOT_STARTED'
写进 document_version（stage-3 锁 WHERE 匹配不到 → 静默跳过整批），以及
contribution.INDEX_OK_STATUSES 长期把 'INDEXED' 当 document_version 成功值查询（永远空集）。

三层防线（配套 opensearch_pipeline/reindex_states.py 的 ChunkIndexStatus /
DocVersionIndexStatus）：
  1. 冻结两套词表的线上取值（refactor 绝不许悄悄改任何状态字符串）；
  2. 关键跨模块常量（STAGE3_*、contribution.INDEX_*）必须落在正确的词表内；
  3. AST 扫描关键模块：代码（含 SQL 字符串）不得再出现裸 index_status 字面量——
     全部经 reindex_states 常量渲染。docstring/注释不算；
     dataworks_nodes/register_new_files.py 是 DataWorks 节点粘贴源（stdlib-only、
     不能 import 本仓库模块），豁免在外。
"""
import ast
import inspect
import re

import pytest

from opensearch_pipeline.reindex_states import (
    STAGE3_CHUNK_RESELECT_INDEX_STATUS,
    STAGE3_CLAIMABLE_INDEX_STATUS,
    ChunkIndexStatus,
    DocVersionIndexStatus,
    rechunk_reset_state,
    sql_in_list,
)


# ─────────────────────── 1. 词表取值冻结（线上契约） ───────────────────────

def test_chunk_meta_vocabulary_frozen():
    # G9（2026-07-06）新增 DEAD 死信终态：index_retry_count 达上限的毒 chunk，
    # 不在 stage-3 loader 重选集内（schema/019 + _fail_chunks_with_retry_budget）。
    assert ChunkIndexStatus.ALL == ("NOT_INDEXED", "INDEXED", "FAILED", "DELETED", "DEAD")


def test_document_version_vocabulary_frozen():
    assert DocVersionIndexStatus.ALL == (
        "NOT_INDEXED", "PROCESSING", "SUCCESS", "FAILED", "PENDING_DELETE", "DELETED",
    )


def test_success_values_do_not_cross_tables():
    # 'INDEXED' 从未被任何代码写入 document_version；'SUCCESS' 从未写入 chunk_meta。
    assert "INDEXED" not in DocVersionIndexStatus.ALL
    assert "SUCCESS" not in ChunkIndexStatus.ALL
    # PROCESSING / PENDING_DELETE 是 document_version 独有的生命周期态
    assert "PROCESSING" not in ChunkIndexStatus.ALL
    assert "PENDING_DELETE" not in ChunkIndexStatus.ALL
    # 'NOT_STARTED' 属 content/chunk 生命周期，两张表的 index_status 都绝不允许（canary 事故值）
    assert "NOT_STARTED" not in ChunkIndexStatus.ALL
    assert "NOT_STARTED" not in DocVersionIndexStatus.ALL


# ─────────────────────── 2. 跨模块常量落在正确词表内 ───────────────────────

def test_stage3_sets_are_vocab_subsets():
    assert STAGE3_CLAIMABLE_INDEX_STATUS == (
        DocVersionIndexStatus.NOT_INDEXED, DocVersionIndexStatus.FAILED)
    assert STAGE3_CHUNK_RESELECT_INDEX_STATUS == (
        ChunkIndexStatus.NOT_INDEXED, ChunkIndexStatus.FAILED)
    # SQL 渲染与既有语句逐字一致（", " 分隔——fake-cursor 测试按此匹配运行期 SQL）
    assert sql_in_list(STAGE3_CLAIMABLE_INDEX_STATUS) == "'NOT_INDEXED', 'FAILED'"
    assert rechunk_reset_state()["index_status"] in STAGE3_CLAIMABLE_INDEX_STATUS


def test_contribution_statuses_use_docversion_vocab():
    """贡献对账 JOIN 的是 document_version：OK/FAIL 集必须⊆该词表，且不得含幽灵值。

    旧值 ('SUCCESS','INDEXED') / ('INDEXING_ERROR','ERROR','FAILED') 里的 INDEXED /
    INDEXING_ERROR / ERROR 从未被任何写路径产出，查询它们永远空集（活体词表混淆）。"""
    from opensearch_pipeline import contribution as C

    assert set(C.INDEX_OK_STATUSES) <= set(DocVersionIndexStatus.ALL)
    assert set(C.INDEX_FAIL_STATUSES) <= set(DocVersionIndexStatus.ALL)
    assert C.INDEX_OK_STATUSES == (DocVersionIndexStatus.SUCCESS,)
    assert C.INDEX_FAIL_STATUSES == (DocVersionIndexStatus.FAILED,)


# ─────────────────────── 3. AST 扫描：关键模块零裸字面量 ───────────────────────

# 词表重构覆盖的模块：index_status 的一切写入/热点 WHERE 都必须经 reindex_states 常量。
_GUARDED_MODULES = (
    "opensearch_pipeline.pipeline_nodes",
    "opensearch_pipeline.dataworks_orchestrator",
    "opensearch_pipeline.spot_checker",
    "opensearch_pipeline.access_grants",
    "opensearch_pipeline.ingestion_resume",
    "opensearch_pipeline.reconcile",
    "opensearch_pipeline.chunker",
    "opensearch_pipeline.contribution",
    "opensearch_pipeline.routes.kb_console",
    "opensearch_pipeline.routes.kb_access",
    "opensearch_pipeline.routes.contribution",
)

# SQL 字符串里的裸谓词：index_status = 'X' / != 'X' / IN ('X'...（含 SUM(index_status='X')）
_SQL_RAW_LITERAL = re.compile(
    r"index_status\s*(?:!=|<>|=)\s*'[A-Z_]+'"
    r"|index_status\s+(?:NOT\s+)?IN\s*\(\s*'",
    re.IGNORECASE,
)


def _docstring_constants(tree):
    """id() 集合：模块/类/函数 docstring 的 Constant 节点（扫描时豁免）。"""
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc_ids.add(id(body[0].value))
    return doc_ids


def _violations(tree):
    """模块 AST 里所有「裸 index_status 字面量」用法（SQL 内嵌 + Python 层）。"""
    found = []
    doc_ids = _docstring_constants(tree)
    for node in ast.walk(tree):
        # (a) 字符串常量（≈SQL 文本）内的裸谓词；f-string 常量片段在插值处断开，天然不命中
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in doc_ids):
            m = _SQL_RAW_LITERAL.search(node.value)
            if m:
                found.append(f"L{node.lineno}: SQL literal {m.group(0)!r}")
        # (b) kwarg：Chunk(..., index_status="X")
        elif (isinstance(node, ast.keyword) and node.arg == "index_status"
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            found.append(f"kwarg index_status={node.value.value!r}")
        # (c) 属性赋值：chunk.index_status = "X"
        elif isinstance(node, ast.Assign):
            if (any(isinstance(t, ast.Attribute) and t.attr == "index_status"
                    for t in node.targets)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                found.append(f"L{node.lineno}: .index_status = {node.value.value!r}")
        # (d) 比较：chunk.index_status == "X"
        elif isinstance(node, ast.Compare):
            sides = [node.left] + list(node.comparators)
            if (any(isinstance(x, ast.Attribute) and x.attr == "index_status" for x in sides)
                    and any(isinstance(x, ast.Constant) and isinstance(x.value, str)
                            for x in sides)):
                found.append(f"L{node.lineno}: comparison against raw literal")
        # (e) getattr(c, "index_status", "X") 的裸默认值
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) == 3):
            a_name, a_default = node.args[1], node.args[2]
            if (isinstance(a_name, ast.Constant) and a_name.value == "index_status"
                    and isinstance(a_default, ast.Constant)
                    and isinstance(a_default.value, str)):
                found.append(f"L{node.lineno}: getattr default {a_default.value!r}")
    return found


@pytest.mark.parametrize("mod_name", _GUARDED_MODULES)
def test_no_raw_index_status_literals(mod_name):
    """关键转移全部经 reindex_states 常量——模块代码里不得再有裸 index_status 字面量。"""
    import importlib

    mod = importlib.import_module(mod_name)
    tree = ast.parse(inspect.getsource(mod))
    bad = _violations(tree)
    assert not bad, (
        f"{mod_name} 存在裸 index_status 字面量（请改用 reindex_states.ChunkIndexStatus / "
        f"DocVersionIndexStatus / sql_in_list 渲染）:\n  " + "\n  ".join(bad)
    )
