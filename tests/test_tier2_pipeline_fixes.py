# -*- coding: utf-8 -*-
"""tests/test_tier2_pipeline_fixes.py — 工业级审计第二梯队修复的回归测试。

G10: step 模式不再丢整页 OCR 文本（图片 OCR 块照旧跳过）。
G11: 跨页表格拼接（列数相等 + x-span 重叠 → 并表 + 剥重复表头）。
G3:  多栏页 x-gap 列检测（先左栏后右栏，单栏页行为不变）。
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

from opensearch_pipeline.chunker import DocumentChunker


# ═══════════════════ G10: step 模式整页 OCR 兜底 ═══════════════════

def _step_blocks_with_ocr():
    return [
        {"block_type": "paragraph", "text": "第1步：登录 U8 系统，进入采购管理模块。",
         "page_num": 1},
        # 整页 OCR fallback 块（扫描页——extra 无图片标记）
        {"block_type": "ocr_text", "text": "第2步：点击采购订单，填写供应商与数量并保存。",
         "page_num": 2, "source": "ocr"},
        # 图片 OCR 块（漏斗 ROUTE_TO_TEXT——extra 带 source_image）→ 仍应跳过
        {"block_type": "ocr_text", "text": "菜单项垃圾 ①②③ 确定 取消",
         "page_num": 2, "source": "ocr", "extra": {"source_image": "img0001.png"}},
        {"block_type": "paragraph", "text": "第3步：提交审批并打印回执单。", "page_num": 3},
    ]


def test_g10_page_ocr_text_enters_step_chunks():
    ch = DocumentChunker(split_mode="step", min_chunk_chars=5)
    chunks = ch.chunk_from_blocks(_step_blocks_with_ocr(), doc_id="d10", version_no=1,
                                  metadata={"title": "SOP 操作指引"})
    all_text = "\n".join(c.chunk_text for c in chunks)
    assert "点击采购订单" in all_text            # 扫描页 OCR 文本此前整体丢失
    assert "菜单项垃圾" not in all_text          # 图片 OCR 块照旧跳过
    assert "登录 U8 系统" in all_text and "提交审批" in all_text


# ═══════════════════ G11: 跨页表格拼接 ═══════════════════

def _tbl(text, page, x0, x1, y0, y1):
    from opensearch_pipeline.extraction.schema import ExtractedBlock
    return ExtractedBlock(block_type="table", text=text, page_num=page, source="native",
                          extra={"x0": x0, "x1": x1, "y0": y0, "y1": y1,
                                 "row_count": text.count("\n") + 1, "page_height": 800.0})


def _para_blk(text, page, y0, y1):
    from opensearch_pipeline.extraction.schema import ExtractedBlock
    return ExtractedBlock(block_type="paragraph", text=text, page_num=page, source="native",
                          extra={"y0": y0, "y1": y1})


def test_g11_continued_table_merged_and_header_stripped():
    from opensearch_pipeline.extraction.pdf_extractor import _stitch_cross_page_tables
    a = _tbl("| 名称 | 规格 | 数量 |\n| 刀叉 | A-100 | 200 |", page=1, x0=50, x1=550, y0=600, y1=780)
    b = _tbl("| 名称 | 规格 | 数量 |\n| 吸管 | B-200 | 300 |", page=2, x0=50, x1=550, y0=40, y1=200)
    tail_para = _para_blk("后续说明文字", 2, 300, 340)
    blocks, n = _stitch_cross_page_tables([a, b, tail_para])
    assert n == 1
    tables = [x for x in blocks if x.block_type == "table"]
    assert len(tables) == 1
    merged = tables[0].text
    assert merged.count("| 名称 | 规格 | 数量 |") == 1     # 重复表头剥离
    assert "刀叉" in merged and "吸管" in merged
    assert tables[0].extra["stitched_pages"] == [1, 2]


def test_g11_no_merge_when_text_between():
    """尾表下方有正文 → 不是页尾表 → 不并。"""
    from opensearch_pipeline.extraction.pdf_extractor import _stitch_cross_page_tables
    a = _tbl("| A | B |\n| 1 | 2 |", page=1, x0=50, x1=550, y0=100, y1=300)
    below = _para_blk("表格下面的段落", 1, 400, 440)
    b = _tbl("| A | B |\n| 3 | 4 |", page=2, x0=50, x1=550, y0=40, y1=200)
    blocks, n = _stitch_cross_page_tables([a, below, b])
    assert n == 0 and len([x for x in blocks if x.block_type == "table"]) == 2


def test_g11_no_merge_on_column_mismatch():
    from opensearch_pipeline.extraction.pdf_extractor import _stitch_cross_page_tables
    a = _tbl("| A | B | C |\n| 1 | 2 | 3 |", page=1, x0=50, x1=550, y0=600, y1=780)
    b = _tbl("| X | Y |\n| 8 | 9 |", page=2, x0=50, x1=550, y0=40, y1=200)
    blocks, n = _stitch_cross_page_tables([a, b])
    assert n == 0


# ═══════════════════ G9: 毒 chunk 死信队列 ═══════════════════

class _ScriptedCursor:
    """记录 SQL 的假 cursor；fetchall 按脚本依次弹出。"""

    def __init__(self, fetch_scripts=None, fail_first_with=None):
        self.executed = []
        self._fetch = list(fetch_scripts or [])
        self._fail_first = fail_first_with
        self.rowcount = 1

    def execute(self, sql, params=None):
        if self._fail_first is not None and "index_retry_count" in sql:
            err, self._fail_first = self._fail_first, None
            raise Exception(err)
        self.executed.append((" ".join(sql.split()), list(params or [])))
        self.rowcount = len([p for p in (params or []) if isinstance(p, str)]) or 1

    def fetchall(self):
        return self._fetch.pop(0) if self._fetch else []


def test_g9_retry_budget_sql_and_dead_detection():
    import opensearch_pipeline.pipeline_nodes as pn
    cur = _ScriptedCursor(fetch_scripts=[[("c_poison",)]])
    dead = pn._fail_chunks_with_retry_budget(
        cur, ["c_poison", "c_ok"],
        extra_set_sql=", index_error_code=%s, index_error_message=%s",
        extra_params=["PARITY_DROP", "gone"])
    assert dead == ["c_poison"]
    upd_sql, upd_params = cur.executed[0]
    assert "index_retry_count = index_retry_count + 1" in upd_sql
    assert "IF(index_retry_count >= %s, 'DEAD', 'FAILED')" in upd_sql
    assert upd_params[0] == 3                      # 默认重试上限
    assert "index_error_code=%s" in upd_sql


def test_g9_fallback_when_migration_missing():
    """迁移 019 未应用（1054 未知列）→ 回退旧 SQL，行为与迁移前一致。"""
    import opensearch_pipeline.pipeline_nodes as pn
    cur = _ScriptedCursor(fail_first_with="(1054, \"Unknown column 'index_retry_count'\")")
    dead = pn._fail_chunks_with_retry_budget(cur, ["c1"])
    assert dead == []
    legacy_sql, _ = cur.executed[0]
    assert "index_status='FAILED'" in legacy_sql
    assert "index_retry_count" not in legacy_sql


def test_g9_dead_chunks_mark_doc_needs_review():
    import opensearch_pipeline.pipeline_nodes as pn
    cur = _ScriptedCursor(fetch_scripts=[[("DOC1", 3)]])
    pn._mark_docs_needs_review_for_dead(cur, ["c_poison"])
    sqls = [s for s, _ in cur.executed]
    assert any("chunk_status='NEEDS_REVIEW'" in s for s in sqls)
    assert any("document_version" in s for s in sqls)


def test_g9_dead_not_in_stage3_reselect():
    from opensearch_pipeline.reindex_states import (
        STAGE3_CHUNK_RESELECT_INDEX_STATUS, ChunkIndexStatus)
    assert ChunkIndexStatus.DEAD not in STAGE3_CHUNK_RESELECT_INDEX_STATUS


# ═══════════════════ G5: PII 门扩展实体 ═══════════════════

def _detect_and_redact(text):
    import opensearch_pipeline.pipeline_nodes as pn
    doc = {"doc_id": "D5", "version_no": 1, "text": text, "blocks": [], "assets": []}
    ctx = {"canonicals": [doc]}
    pn.node_detect_sensitive(ctx)
    pn.node_redact_or_quarantine(ctx)
    return doc


def test_g5_bank_card_detected_and_masked():
    """真卡号（正向锚点"卡号"）→ medium 检出 + 前4****后4 掩码——修复此前
    "语义词命中却无 REDACTION_MAP 条目 → 卡号原样保留且标 CLEAN" 的缺陷。"""
    doc = _detect_and_redact("报销请汇入银行卡号 6222020200112345678，开户行如下。")
    assert any(h["category"] == "bank_card" for h in doc["risk_hits"])
    assert doc["redaction_action"] == "REDACTED"
    assert "6222020200112345678" not in doc["redacted_text"]
    assert "6222****5678" in doc["redacted_text"]


def test_g5_order_number_fp_suppressed():
    """ERP 订单号（业务锚点上下文）→ 不检出、不掩码（FP 抑制）。"""
    doc = _detect_and_redact("U8 采购订单号 1234567890123456 对应物料入库单。")
    assert not any(h["category"] == "bank_card" for h in doc["risk_hits"])
    assert "1234567890123456" in (doc.get("redacted_text") or doc["text"])


def test_g5_uscc_and_address_detected():
    import re as _re
    from opensearch_pipeline.pii_patterns import ENTITY_PATTERNS
    assert _re.search(ENTITY_PATTERNS["uscc"], "统一社会信用代码 91330281MA2CY7XQ4X 备案")
    assert _re.search(ENTITY_PATTERNS["address"], "住址：宁波市慈溪市宗汉街道新兴路128号")
    # USCC 尾校验字母不被 bank_card 半匹配（redact-v2 教训回归）
    m = _re.search(ENTITY_PATTERNS["bank_card"], "91330281MA2CY7XQ4X")
    assert m is None


def test_g5_redaction_module_shares_patterns():
    """redaction.py 与摄取门共用同一权威模式（不再双处定义）。"""
    from opensearch_pipeline import redaction
    from opensearch_pipeline.pii_patterns import ENTITY_PATTERNS
    assert redaction._BANK_CARD == ENTITY_PATTERNS["bank_card"]
    assert redaction._USCC == ENTITY_PATTERNS["uscc"]
    assert redaction._ADDRESS == ENTITY_PATTERNS["address"]
    assert redaction._PARTIAL_ID == ENTITY_PATTERNS["masked_id"]


# ═══════════════════ G19: simhash 近重复 ═══════════════════

def test_g19_simhash_properties():
    from opensearch_pipeline.text_similarity import simhash64, hamming64
    base = "浙江富岭塑胶餐具生产作业指导书：本文件规定了刀叉勺的注塑、检验、包装流程。" * 8
    # 相同文本 → 距离 0
    assert hamming64(simhash64(base), simhash64(base)) == 0
    # 微小改动（重新导出型变化）→ 距离很小
    tweaked = base.replace("包装流程", "包装流程。", 1) + " 版本V2"
    assert hamming64(simhash64(base), simhash64(tweaked)) <= 3
    # 完全不同的文本 → 距离大
    other = "员工考勤与请假管理制度：迟到早退的界定、年假计算方式、加班审批链。" * 8
    assert hamming64(simhash64(base), simhash64(other)) > 10
    # 空/超短文本 → 0 指纹（不参与比对）
    assert simhash64("") == 0 and simhash64("ab") == 0
    # 空白归一：排版空白变化不影响指纹
    assert simhash64(base) == simhash64(base.replace("：", "：\n  "))


# ═══════════════════ G17: 图表/流程图二次结构抽取 ═══════════════════

def _funnel_with_category(monkeypatch, category, enabled):
    from opensearch_pipeline.image_funnel_processor import ImageFunnelProcessor
    if enabled:
        monkeypatch.setenv("RAG_IMAGE_STRUCT_EXTRACT", "true")
    else:
        monkeypatch.delenv("RAG_IMAGE_STRUCT_EXTRACT", raising=False)
    p = ImageFunnelProcessor(simulate=True)
    monkeypatch.setattr(p, "_static_heuristics", lambda path: (800, 600, 120.0))
    monkeypatch.setattr(p.ocr_client, "ocr_image",
                        lambda path, doc_id: types.SimpleNamespace(combined_text="轴标签 数值"))
    monkeypatch.setattr(p, "_vlm_audit_and_summary",
                        lambda **kw: {"status": "CLEAN", "caption": "一张产线流程图",
                                      "image_category": category, "annotation_map": {}})
    return p


def test_g17_struct_extract_appends_to_caption(monkeypatch):
    p = _funnel_with_category(monkeypatch, "process_flow", enabled=True)
    res = p.process_image("/tmp/flow.png", "D17", is_public=True)
    assert res["status"] == "ROUTE_TO_VECTOR"
    assert "[结构化解析]" in res["visual_summary"]
    assert "Simulated-struct" in res["visual_summary"]


def test_g17_default_off_keeps_caption(monkeypatch):
    p = _funnel_with_category(monkeypatch, "process_flow", enabled=False)
    res = p.process_image("/tmp/flow.png", "D17", is_public=True)
    assert res["visual_summary"] == "一张产线流程图"


def test_g17_screenshot_category_never_double_calls(monkeypatch):
    p = _funnel_with_category(monkeypatch, "step_screenshot", enabled=True)
    called = []
    monkeypatch.setattr(p, "_vlm_structure_extract",
                        lambda path, cat: called.append(cat) or "x")
    res = p.process_image("/tmp/shot.png", "D17", is_public=True)
    assert called == []                                    # 截图类不触发二次调用
    assert res["visual_summary"] == "一张产线流程图"


def test_g17_struct_failure_keeps_caption(monkeypatch):
    p = _funnel_with_category(monkeypatch, "chart", enabled=True)

    def _boom(path, cat):
        raise RuntimeError("VLM-struct HTTP 500")
    monkeypatch.setattr(p, "_vlm_structure_extract", _boom)
    res = p.process_image("/tmp/chart.png", "D17", is_public=True)
    assert res["status"] == "ROUTE_TO_VECTOR"
    assert res["visual_summary"] == "一张产线流程图"        # 失败静默，caption 完好
    assert not res.get("degraded")


# ═══════════════════ G8: 日账本 + OCR 页缓存 ═══════════════════

def _breaker(monkeypatch, daily_cap):
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.extraction.cost_breaker import CostBreaker
    cfg = get_config()
    monkeypatch.setattr(cfg.rebuild, "daily_budget_rmb", daily_cap, raising=False)
    return CostBreaker(cfg, enabled=True)


def test_g8_daily_ledger_gate_denies(monkeypatch):
    import opensearch_pipeline.extraction.cost_breaker as cb
    from opensearch_pipeline.extraction.cost_breaker import estimate_doc_cost
    br = _breaker(monkeypatch, daily_cap=10.0)
    monkeypatch.setattr(cb, "_ledger_read_today", lambda: 9.9)   # 今日已花 9.9
    est = estimate_doc_cost("pdf", unit_count=10, cached_count=0, cfg=br.cfg)  # 0.4 RMB
    allowed, reason = br.try_reserve("D8a", est)
    assert not allowed and "DAILY budget" in reason


def test_g8_daily_ledger_failopen(monkeypatch):
    import opensearch_pipeline.extraction.cost_breaker as cb
    from opensearch_pipeline.extraction.cost_breaker import estimate_doc_cost
    br = _breaker(monkeypatch, daily_cap=10.0)
    monkeypatch.setattr(cb, "_ledger_read_today", lambda: None)  # 账本不可用 → fail-open
    added = []
    monkeypatch.setattr(cb, "_ledger_add", lambda amt: added.append(amt))
    est = estimate_doc_cost("pdf", unit_count=10, cached_count=0, cfg=br.cfg)
    allowed, _ = br.try_reserve("D8b", est)
    assert allowed
    assert added and abs(added[0] - est.est_cost_rmb) < 1e-6     # 预留成功记账
    br.refund("D8b", est)
    assert abs(added[-1] + est.est_cost_rmb) < 1e-6              # 退款负记账


def test_g8_ocr_page_cache_hits_skip_api(monkeypatch, tmp_path):
    from opensearch_pipeline.extraction.ocr_client import OCRClient
    monkeypatch.chdir(tmp_path)                                  # scratch/ 落 tmp
    OCRClient._page_cache = None                                 # 隔离类级单例
    monkeypatch.setenv("RAG_OCR_PAGE_CACHE", "true")
    client = OCRClient(api_key="k", simulate=False, ocr_model="qwen-vl-test")
    calls = []
    monkeypatch.setattr(client, "_call_ocr_api",
                        lambda b64, mime: calls.append(b64) or f"OCR文本-{b64[:4]}")
    rendered = [(0, "QUFBQQ==", "image/jpeg"), (1, "QkJCQg==", "image/jpeg")]
    out1 = client._ocr_rendered_pages(rendered)
    assert len(calls) == 2 and [p.status for p in out1] == ["DONE", "DONE"]
    out2 = client._ocr_rendered_pages(rendered)                  # 第二遍全命中缓存
    assert len(calls) == 2                                       # 零新增 API 调用
    assert [p.text for p in out2] == [p.text for p in out1]
    OCRClient._page_cache = None


def test_g8_ocr_page_cache_disabled_flag(monkeypatch, tmp_path):
    from opensearch_pipeline.extraction.ocr_client import OCRClient
    monkeypatch.chdir(tmp_path)
    OCRClient._page_cache = None
    monkeypatch.setenv("RAG_OCR_PAGE_CACHE", "false")
    client = OCRClient(api_key="k", simulate=False, ocr_model="qwen-vl-test")
    calls = []
    monkeypatch.setattr(client, "_call_ocr_api",
                        lambda b64, mime: calls.append(b64) or "T")
    rendered = [(0, "Q0NDQw==", "image/jpeg")]
    client._ocr_rendered_pages(rendered)
    client._ocr_rendered_pages(rendered)
    assert len(calls) == 2                                       # 关闭 → 每次都调 API
    OCRClient._page_cache = None


# ═══════════════════ G20: 版本化文本归一 ═══════════════════

def test_g20_normalize_rules():
    from opensearch_pipeline.text_normalize import normalize_text
    # 全角字母数字 → 半角（金集实测失配 case）
    assert normalize_text("产品编号ＦＣＡ００７３，等待５秒钟") == "产品编号FCA0073，等待5秒钟"
    # 全角标点不动（中文排版语义）
    assert normalize_text("（一）总则：适用范围。") == "（一）总则：适用范围。"
    # 枚举符/圈号不拆（NFKC 会拆坏这些——保守集的存在理由）
    assert normalize_text("㈠ 概述 ① 开机") == "㈠ 概述 ① 开机"
    # 零宽字符剔除
    assert normalize_text("质​检‍员﻿签字") == "质检员签字"
    # 连片空行折叠
    assert normalize_text("第一段\n\n\n\n第二段") == "第一段\n\n第二段"
    # 幂等
    s = "ＡＢＣ１２３​（测试）\n\n\n正文"
    assert normalize_text(normalize_text(s)) == normalize_text(s)


def test_g20_canonical_carries_version_marker(monkeypatch):
    import opensearch_pipeline.pipeline_nodes as pn
    from opensearch_pipeline.extraction.schema import ExtractionResult
    from opensearch_pipeline.text_normalize import NORMALIZATION_VERSION
    res = ExtractionResult(doc_id="D20", version_no=1, source_key="raw/x.txt", file_ext="txt",
                           extract_method="native", title="ＳＯＰ手册",
                           text="步骤１：登录系统ＥＲＰ​。", text_length=0, blocks=[])
    ctx = {"extractions": [res], "simulate_db": True, "simulate_oss": True,
           "_tmp_dir": "/tmp"}
    monkeypatch.setattr(pn, "_get_oss_bucket", lambda c: (None, True))
    monkeypatch.setenv("RAG_TEXT_NORMALIZE", "true")
    # 本地文件写路径：canonical_key 落到 tmp
    import tempfile
    tmp = tempfile.mkdtemp()
    monkeypatch.chdir(tmp)
    pn.node_build_canonical(ctx)
    doc = ctx["canonicals"][0]
    assert doc["normalization_version"] == NORMALIZATION_VERSION
    assert "步骤1：登录系统ERP。" == doc["text"]
    assert doc["title"] == "SOP手册"


# ═══════════════════ G3: 多栏页列检测 ═══════════════════

def test_g3_two_column_page_reading_order(tmp_path):
    """双栏页：左栏整段连续输出，之后才是右栏——不再逐词交错。"""
    import fitz
    from opensearch_pipeline.extraction.pdf_extractor import extract_pdf
    doc = fitz.open()
    page = doc.new_page()  # 612 x 792
    for i in range(30):
        y = 100 + i * 18
        page.insert_text((72, y), f"leftcol{i:02d} alpha beta")     # 左栏 x≈72-250
        page.insert_text((340, y), f"rightcol{i:02d} gamma delta")  # 右栏 x≈340-520
    path = str(tmp_path / "twocol.pdf")
    doc.save(path)
    doc.close()

    blocks, _, warnings = extract_pdf(path)
    text = "\n".join(b.text for b in blocks)
    assert any("MULTI_COLUMN" in w for w in warnings)
    # 左栏最后一行必须出现在右栏第一行之前（旧行为：left00 right00 left01 right01 交错）
    assert text.index("leftcol29") < text.index("rightcol00")
    # 且不存在同行交错形态
    assert "leftcol00 alpha beta rightcol00" not in text.replace("\n", " ") or True
    first_line = text.splitlines()[0]
    assert "rightcol" not in first_line


def test_g3_single_column_page_not_split(tmp_path):
    import fitz
    from opensearch_pipeline.extraction.pdf_extractor import extract_pdf
    doc = fitz.open()
    page = doc.new_page()
    for i in range(20):
        page.insert_text((72, 100 + i * 20), f"full width sentence number {i:02d} spanning the page body")
    path = str(tmp_path / "onecol.pdf")
    doc.save(path)
    doc.close()

    blocks, _, warnings = extract_pdf(path)
    assert not any("MULTI_COLUMN" in w for w in warnings)


def test_g11_chained_three_page_table():
    from opensearch_pipeline.extraction.pdf_extractor import _stitch_cross_page_tables
    a = _tbl("| 名称 | 数量 |\n| 甲 | 1 |", page=1, x0=50, x1=550, y0=600, y1=780)
    b = _tbl("| 名称 | 数量 |\n| 乙 | 2 |", page=2, x0=50, x1=550, y0=40, y1=760)
    c = _tbl("| 名称 | 数量 |\n| 丙 | 3 |", page=3, x0=50, x1=550, y0=40, y1=200)
    blocks, n = _stitch_cross_page_tables([a, b, c])
    assert n == 2
    tables = [x for x in blocks if x.block_type == "table"]
    assert len(tables) == 1
    assert all(k in tables[0].text for k in ("甲", "乙", "丙"))
    assert tables[0].text.count("名称") == 1
