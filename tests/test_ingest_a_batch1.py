# -*- coding: utf-8 -*-
"""摄取管线 A 类修复 batch-1 回归（A8 / A9 / A10 / A1，2026-07-25）。

A8：payload 归档失败降级为警告 —— 它发生在 HA3 push 成功【之后】、状态回写【之前】，
    raise 会让整轮 stage-3 白跑且 job 停在 PENDING；归档产物全仓无读回方，属辅助可观测性。
A9：per-doc 错误归因此前只有上界，err_idx=-1 会误标本子批最后一个 chunk；补下界 + 排除 bool
    + 非 dict 条目防御，并对不可归因/形态不符的响应采样（**行为不变**，拿到真实样本前不 fail-closed）。
A10：分类器 payload 钉死 enable_thinking/max_tokens，且刻意不读与问答共用的 config.llm。
A1：三个并发旋钮无条件打印 configured/effective。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

import inspect

import pytest

import opensearch_pipeline.pipeline_nodes as pn
from opensearch_pipeline.reindex_states import ChunkIndexStatus


# ═══════════════════ A9：错误归因边界 + 采样 ═══════════════════

class _Chunk:
    def __init__(self, cid):
        self.chunk_id = cid
        self.index_status = ChunkIndexStatus.NOT_INDEXED
        self.index_error_code = None
        self.index_error_message = None
        self.embedding_vector = [0.1] * 4
        self.rds_id = int(cid[1:])

    def to_ha3_doc(self, pk_field, include_allowed_depts=False):
        return {pk_field: self.rds_id, "chunk_id": self.chunk_id}


class _Resp:
    def __init__(self, body):
        self.body = body


class _FakeClient:
    def __init__(self, body):
        self.body = body

    def push_documents(self, table, pk, request):
        return _Resp(self.body)


class _Cfg:
    table_name = "t"
    pk_field = "id"


def _push(body, n=3):
    chunks = [_Chunk("c%d" % i) for i in range(n)]
    stats = pn._push_chunks_to_ha3(_FakeClient(body), _Cfg(), chunks, max_retries=0)
    return chunks, stats


def test_a9_negative_index_no_longer_mislabels_last_chunk():
    """err_idx=-1：旧判据 `-1 < len` 恒真 → sub_chunks[-1] 被误标 FAILED。"""
    chunks, stats = _push({"errors": [{"index": -1, "code": "E", "message": "boom"}]})
    assert [c.index_status for c in chunks] == [ChunkIndexStatus.INDEXED] * 3
    assert chunks[-1].index_error_code is None, "负索引绝不能归因到本子批最后一个 chunk"
    assert any("unattributable" in w for w in stats["warnings"])


def test_a9_bool_index_rejected():
    """True 是 int 子类，旧判据会把它当索引 1。"""
    chunks, stats = _push({"errors": [{"index": True, "code": "E", "message": "boom"}]})
    assert chunks[1].index_error_code is None
    assert any("unattributable" in w for w in stats["warnings"])


def test_a9_valid_index_still_attributed():
    chunks, _ = _push({"errors": [{"index": 1, "code": "E42", "message": "bad doc"}]})
    assert chunks[1].index_status == ChunkIndexStatus.FAILED
    assert chunks[1].index_error_code == "E42"
    assert chunks[0].index_status == ChunkIndexStatus.INDEXED
    assert chunks[2].index_status == ChunkIndexStatus.INDEXED


def test_a9_out_of_range_index_samples_without_crash():
    chunks, stats = _push({"errors": [{"index": 99, "code": "E", "message": "m"}]})
    assert all(c.index_status == ChunkIndexStatus.INDEXED for c in chunks)
    assert any("unattributable" in w for w in stats["warnings"])


def test_a9_non_dict_error_item_does_not_explode():
    """旧代码对非 dict 条目直接 .get → AttributeError 冒泡，把整个子批打成意外异常。"""
    chunks, stats = _push({"errors": ["just a string"]})
    assert all(c.index_status == ChunkIndexStatus.INDEXED for c in chunks)
    assert any("not dict" in w for w in stats["warnings"])


def test_a9_unparseable_body_is_sampled():
    chunks, stats = _push("{not json")
    assert all(c.index_status == ChunkIndexStatus.INDEXED for c in chunks)   # 行为不变
    assert any("failed JSON parse" in w for w in stats["warnings"])


def test_a9_errors_not_a_list_is_sampled():
    chunks, stats = _push({"errors": {"index": 0}})
    assert all(c.index_status == ChunkIndexStatus.INDEXED for c in chunks)
    assert any("not list" in w for w in stats["warnings"])


def test_a9_clean_response_emits_no_warnings():
    chunks, stats = _push({"errors": []})
    assert all(c.index_status == ChunkIndexStatus.INDEXED for c in chunks)
    assert stats["warnings"] == []


def test_a9_sample_repr_is_bounded_and_never_raises():
    assert len(pn._sample_repr("x" * 5000)) < 600

    class _Boom:
        def __repr__(self):
            raise RuntimeError("nope")

    assert pn._sample_repr(_Boom()) == "<unreprable>"


def test_a9_warnings_reach_ctx_not_via_helper_ctx_param():
    """helper 无 ctx（且被 04b parity 两处复用）→ warning 必须走返回值。"""
    assert "ctx" not in inspect.signature(pn._push_chunks_to_ha3).parameters
    src = " ".join(inspect.getsource(pn.node_push_to_opensearch).split())
    assert 'ctx.setdefault("validation_warnings", []).extend(push_stats["warnings"])' in src


# ═══════════════════ A8：归档失败降级 ═══════════════════

def test_a8_archive_warning_recorded_not_raised():
    ctx, batch = {}, {}
    pn._record_archive_warning(ctx, batch, "[ARCHIVE] failed to archive OSS payload x: boom")
    assert batch["archive_warning"].startswith("[ARCHIVE]")
    assert ctx["validation_warnings"] == [batch["archive_warning"]]


def test_a8_no_raise_left_in_archive_paths():
    src = " ".join(inspect.getsource(pn.node_push_to_opensearch).split())
    assert "Failed to archive OSS payload object during archive" not in src
    assert "Failed to move batch payload file during archive" not in src
    assert src.count("_record_archive_warning(") == 2, "sim + 真实 OSS 两条归档路径都要降级"


def test_a8_warning_persists_to_bulk_job_error_message():
    """只写 ctx 不够：pipeline_run 不收集 validation_warnings，运行结束线索就没了。"""
    src = " ".join(inspect.getsource(pn.node_update_index_status).split())
    assert 'batch.get("archive_warning")' in src
    assert "error_message=%s" in src


# ═══════════════════ A10：分类器钉死思考模式与输出上限 ═══════════════════

def test_a10_classifier_payload_pins_thinking_and_max_tokens():
    src = " ".join(inspect.getsource(pn.run_gemini_classification).split())
    assert '"enable_thinking": False' in src
    assert '"max_tokens": _CLASSIFY_MAX_TOKENS' in src
    assert pn._CLASSIFY_MAX_TOKENS >= 1024


def _code_only(src: str) -> str:
    """剥掉整行注释（本仓注释里会引用被禁用的符号名，断言必须只看代码）。"""
    return "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))


def test_a10_classifier_does_not_read_shared_llm_config():
    """LLMConfig 与问答共用：读它会让 RAG_LLM_ENABLE_THINKING 的服务侧翻转传导到摄取分类，
    而类别决定 chunk 家族路由 → 静默 re-roll。必须硬编码。"""
    code = _code_only(inspect.getsource(pn.run_gemini_classification))
    assert "config.llm" not in code
    assert "llm.enable_thinking" not in code
    assert "llm.max_tokens" not in code


# ═══════════════════ A1：并发旋钮可观测 ═══════════════════

@pytest.mark.parametrize("fn,knob", [
    ("node_extract_text_with_ocr", "RAG_EXTRACT_CONCURRENCY"),
    ("node_publish_to_rag_ready", "RAG_PUBLISH_CONCURRENCY"),
])
def test_a1_pipeline_knobs_log_configured_and_effective(fn, knob):
    src = " ".join(inspect.getsource(getattr(pn, fn)).split())
    assert "[concurrency]" in src and knob in src
    assert "configured=" in src and "effective=" in src


def test_a1_loader_knob_logs_configured_and_effective():
    from opensearch_pipeline import dataworks_orchestrator as orch
    src = " ".join(inspect.getsource(orch.run_stage).split())
    assert "[concurrency] loader-fetch" in src
    assert "configured=" in src and "effective=" in src
