# -*- coding: utf-8 -*-
"""RAG_OCR_PAGE_CONCURRENCY — 扫描页 OCR 的有界并发。

守护点（perf：50 页扫描 SOP 串行 OCR 2.5-7 分钟 → 并发 4）：
  1. =1 时走纯串行路径，行为与历史串行实现完全一致（调用顺序、线程、结果）；
  2. 并发下结果必须按页序 keyed 重组（page_num 1-based），不受完成顺序影响；
  3. 单页失败语义不变：该页 FAILED（text=""、error=repr(e)），邻页不受污染；
     全页 FAILED → 聚合 FAILED 的收口（batch-1 修复）在并发下仍成立。
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

from opensearch_pipeline.extraction.ocr_client import OCRClient


def _client():
    return OCRClient(
        api_key="sk-test",
        api_base_url="https://dashscope.aliyuncs.com/api/v1",
        ocr_model="qwen-vl-max",
        simulate=False,
    )


def _rendered(n, start_idx=0):
    """构造 _ocr_rendered_pages 的输入：payload 编码页号，mock 可据此识别页。"""
    return [(i, f"payload-{i}", "image/jpeg") for i in range(start_idx, start_idx + n)]


def _pdf(n_pages):
    """n 页空白 PDF（全部触发 OCR 路径时页序可预期）。"""
    import fitz
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    path = tempfile.mktemp(suffix=".pdf")
    doc.save(path)
    doc.close()
    return path


# ── 直接测 _ocr_rendered_pages（payload 可识别页，断言最精确） ──

def test_concurrency_one_is_serial_in_caller_thread_and_in_order(monkeypatch):
    """=1：调用顺序 == 页序，全部在调用线程执行（无线程池），结果与旧串行一致。"""
    monkeypatch.setenv("RAG_OCR_PAGE_CONCURRENCY", "1")
    client = _client()

    call_order, threads = [], set()

    def fake_api(b64, mime):
        call_order.append(b64)
        threads.add(threading.get_ident())
        return f"ocr:{b64}"

    monkeypatch.setattr(client, "_call_ocr_api", fake_api)
    pages = client._ocr_rendered_pages(_rendered(4))

    assert call_order == [f"payload-{i}" for i in range(4)]  # 严格页序调用
    assert threads == {threading.get_ident()}  # 无 worker 线程
    assert [p.page_num for p in pages] == [1, 2, 3, 4]  # 1-based
    assert [p.text for p in pages] == [f"ocr:payload-{i}" for i in range(4)]
    assert all(p.status == "DONE" for p in pages)


def test_parallel_reassembles_in_page_order_despite_completion_order(monkeypatch):
    """并发 4：早页故意慢（完成序≠提交序），输出仍严格按页序；in-flight ≤ 4。"""
    monkeypatch.setenv("RAG_OCR_PAGE_CONCURRENCY", "4")
    client = _client()

    lock = threading.Lock()
    threads = set()
    inflight = {"now": 0, "max": 0}

    def fake_api(b64, mime):
        with lock:
            threads.add(threading.get_ident())
            inflight["now"] += 1
            inflight["max"] = max(inflight["max"], inflight["now"])
        idx = int(b64.rsplit("-", 1)[1])
        time.sleep((6 - idx) * 0.03)  # 页 0 最慢 → 完成顺序大概率倒转
        with lock:
            inflight["now"] -= 1
        return f"ocr:{b64}"

    monkeypatch.setattr(client, "_call_ocr_api", fake_api)
    pages = client._ocr_rendered_pages(_rendered(6))

    assert [p.page_num for p in pages] == [1, 2, 3, 4, 5, 6]
    assert [p.text for p in pages] == [f"ocr:payload-{i}" for i in range(6)]
    assert all(p.status == "DONE" for p in pages)
    assert len(threads) >= 2  # 真的并发了
    assert inflight["max"] <= 4  # 有界


def test_mid_page_failure_does_not_corrupt_neighbors(monkeypatch):
    """并发下第 3 页（idx=2）失败：该页 FAILED/text=""/error 保留，邻页文本完好且页序不乱。"""
    monkeypatch.setenv("RAG_OCR_PAGE_CONCURRENCY", "4")
    client = _client()

    def fake_api(b64, mime):
        if b64 == "payload-2":
            raise RuntimeError("DashScope OCR HTTP 500: boom")
        return f"ocr:{b64}"

    monkeypatch.setattr(client, "_call_ocr_api", fake_api)
    pages = client._ocr_rendered_pages(_rendered(5))

    assert [p.page_num for p in pages] == [1, 2, 3, 4, 5]
    bad = pages[2]
    assert bad.status == "FAILED" and bad.text == ""
    assert "HTTP 500" in bad.error
    for p in pages:
        if p.page_num != 3:
            assert p.status == "DONE"
            assert p.text == f"ocr:payload-{p.page_num - 1}"


def test_invalid_env_value_falls_back_to_default(monkeypatch):
    """RAG_OCR_PAGE_CONCURRENCY 非法值不炸 OCR（回退默认 4）。"""
    monkeypatch.setenv("RAG_OCR_PAGE_CONCURRENCY", "not-an-int")
    client = _client()
    monkeypatch.setattr(client, "_call_ocr_api", lambda b64, mime: f"ocr:{b64}")
    pages = client._ocr_rendered_pages(_rendered(3))
    assert [p.page_num for p in pages] == [1, 2, 3]
    assert all(p.status == "DONE" for p in pages)


# ── 走完整 _real_pdf_ocr（真实 fitz 渲染 → 并发 API mock） ──

def test_real_pdf_ocr_page_numbering_stays_1based(monkeypatch):
    """完整路径：3 页 PDF 并发 OCR，页号 1-based 且升序（keyed merge 未被并发打乱）。"""
    monkeypatch.setenv("RAG_OCR_PAGE_CONCURRENCY", "4")
    client = _client()
    monkeypatch.setattr(client, "_call_ocr_api", lambda b64, mime: "文字")
    result = client._real_pdf_ocr(_pdf(3), "DOC-CONC")

    assert result.status == "DONE"
    assert [p.page_num for p in result.pages] == [1, 2, 3]
    assert result.combined_text == "文字\n\n文字\n\n文字"


def test_real_pdf_ocr_page_nums_subset_keeps_original_page_numbers(monkeypatch):
    """page_nums=[2,4]：并发下仍只 OCR 这些页，且 page_num 保留原始页号。"""
    monkeypatch.setenv("RAG_OCR_PAGE_CONCURRENCY", "4")
    client = _client()
    monkeypatch.setattr(client, "_call_ocr_api", lambda b64, mime: "x")
    result = client._real_pdf_ocr(_pdf(4), "DOC-SUBSET", page_nums=[4, 2])

    assert [p.page_num for p in result.pages] == [2, 4]
    assert result.status == "DONE"


def test_real_pdf_ocr_all_pages_failed_aggregates_failed(monkeypatch):
    """全页失败 → 聚合 FAILED（batch-1 OCR-all-fail 收口在并发下不回归）。"""
    monkeypatch.setenv("RAG_OCR_PAGE_CONCURRENCY", "4")
    client = _client()

    def always_fail(b64, mime):
        raise RuntimeError("DashScope OCR HTTP 429: throttled")

    monkeypatch.setattr(client, "_call_ocr_api", always_fail)
    result = client._real_pdf_ocr(_pdf(3), "DOC-ALLFAIL")

    assert result.status == "FAILED"
    assert [p.page_num for p in result.pages] == [1, 2, 3]
    assert all(p.status == "FAILED" for p in result.pages)
    assert result.combined_text == ""


def test_real_pdf_ocr_partial_failure_stays_done(monkeypatch):
    """部分失败（3 页中 1 页挂）→ 聚合仍 DONE，幸存页文本进 combined（旧语义）。"""
    monkeypatch.setenv("RAG_OCR_PAGE_CONCURRENCY", "4")
    client = _client()

    lock = threading.Lock()
    calls = {"n": 0}

    def flaky(b64, mime):
        with lock:
            calls["n"] += 1
            n = calls["n"]
        if n == 2:  # 恰好挂掉一次调用（哪一页不重要——语义按页隔离）
            raise RuntimeError("DashScope OCR HTTP 500: boom")
        return "内容"

    monkeypatch.setattr(client, "_call_ocr_api", flaky)
    result = client._real_pdf_ocr(_pdf(3), "DOC-PARTIAL")

    assert result.status == "DONE"
    assert [p.page_num for p in result.pages] == [1, 2, 3]
    assert sorted(p.status for p in result.pages) == ["DONE", "DONE", "FAILED"]
    assert result.combined_text == "内容\n\n内容"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
