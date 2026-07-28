# -*- coding: utf-8 -*-
"""选项 E（2026-07-26）：漏斗判决从"可淘汰的缓存"升级为"不可淘汰的记录"。

**要挡的事**：漏斗判决决定哪些图能进知识库，但它今天只活在 VLM 缓存里 —— advisory、
按 rowid FIFO 淘汰、可被 `RAG_VLM_CACHE_VERSION` 一键作废。实测（方案 §8）缓存里
07-20 的判决与今天的 VLM 有 6.5% 不一致、**全部单向 keep→discard**，4 张是 GT 期望的
步骤配图。也就是说现网图覆盖此刻靠缓存条目撑着。

本文件钉五类契约：
  1. **三态**（HIT/MISS/**UNAVAILABLE**）—— 把 error 当 miss 就等于"库一抖就允许重判"；
  2. **载荷不含派生内容**（caption/OCR 不进 RDS 这个新查询面）；
  3. **first-writer-wins** 与 `cache_promoted` 的溯源诚实（不拿当前模型冒充历史）；
  4. **内容纯度前提**：与 `RAG_VLM_DOC_CONTEXT` 互斥；
  5. **降级不静默**：拿不到权威判决且无缓存兜底 ⇒ 进 `partial_loss_notes` → NEEDS_REVIEW。
"""
import pathlib

import pytest

from opensearch_pipeline import funnel_verdict_store as vs

FLAG = "RAG_FUNNEL_VERDICT_STORE"


class _Conn:
    """最小 DB 替身。`raise_on` 让读/写各自可控地炸。"""

    def __init__(self, rows=(), raise_on=None):
        self.rows, self.raise_on = list(rows), raise_on
        self.executed, self.many, self.commits = [], [], 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.raise_on == "read":
            raise RuntimeError("1146 table doesn't exist")
        self.executed.append((sql, params))

    def executemany(self, sql, rows):
        if self.raise_on == "write":
            raise RuntimeError("write blew up")
        self.many.append((sql, rows))

    def fetchall(self):
        return self.rows

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _res(status="ROUTE_TO_VECTOR", **kw):
    d = {"status": status, "reason": "Funnel 3: something", "image_category": "unknown",
         "visual_summary": "一只手正在接线", "ocr_text": "机密 手机号 13800000000"}
    d.update(kw)
    return d


# ── 1. 三态 ─────────────────────────────────────────────────────────────────

def test_read_error_is_unavailable_not_miss():
    """**最要紧的一条**：库一抖就返回 miss，等于允许重判 —— 而重判正是 E 要挡的事。"""
    state, rows = vs.fetch_many(_Conn(raise_on="read"), ["a"], True, "")
    assert state == vs.UNAVAILABLE and rows == {}


def test_empty_result_is_a_real_miss():
    state, rows = vs.fetch_many(_Conn(rows=[]), ["a"], True, "")
    assert state == vs.HIT and rows == {}      # 查得到库、只是没有这些行


def test_no_hashes_does_not_touch_the_db():
    conn = _Conn()
    assert vs.fetch_many(conn, [], True, "") == (vs.MISS, {})
    assert conn.executed == []


def test_fallback_hashes_are_never_queried():
    """`fallback_` 前缀键基于内存地址，跨进程无意义 —— 与缓存写入门同判据。"""
    conn = _Conn()
    assert vs.fetch_many(conn, ["fallback_123"], True, "") == (vs.MISS, {})
    assert conn.executed == []


def test_query_pins_namespace_policy_and_excludes_superseded():
    conn = _Conn(rows=[("h1", "ROUTE_TO_VECTOR", "unknown", "funnel3", "fresh_decision")])
    state, rows = vs.fetch_many(conn, ["h1", "h1"], False, "c1")
    sql, params = conn.executed[0]
    assert "namespace=%s" in sql and "funnel_policy_version=%s" in sql
    assert "superseded_by IS NULL" in sql
    assert params[:2] == ["sec", "c1"]
    assert len(params) == 3, "重复 sha256 应去重后再查"
    assert state == vs.HIT and rows["h1"]["status"] == "ROUTE_TO_VECTOR"


# ── 2. 载荷：只存判决，不存派生内容 ─────────────────────────────────────────

def test_payload_excludes_caption_and_ocr():
    """codex BLOCKER-2：ocr_text/visual_summary 可能含 PII（图像 OCR 脱敏默认 OFF 且
    「仅本轮内存生效，不回写 canonical」），它们留在既有缓存里，**不复制进 RDS 这个
    新查询面**。"""
    conn = _Conn()
    vs.record_many(conn, [{"image_sha256": "h", "funnel_res": _res()}],
                   True, "", "qwen3-vl-plus", "D", 1)
    sql, rows = conn.many[0]
    for banned in ("ocr_text", "visual_summary", "caption", "annotation"):
        assert banned not in sql, f"{banned} 不得进 059"
    blob = str(rows)
    assert "13800000000" not in blob and "一只手正在接线" not in blob


def test_ddl_has_no_derived_content_columns():
    ddl = pathlib.Path("schema/059_image_funnel_verdict.sql").read_text(encoding="utf-8")
    body = ddl[ddl.index("CREATE TABLE"):]
    for banned in ("ocr_text", "visual_summary", "vlm_annotation_map"):
        assert banned not in body, f"059 的建表体不得含 {banned}"
    assert "PRIMARY KEY (image_sha256, namespace, funnel_policy_version)" in body
    assert "utf8mb4" in body and "COLLATE" in body


# ── 3. 写入语义 ─────────────────────────────────────────────────────────────

def test_first_writer_wins():
    """并发下两个线程可能各付一次 VLM、得到不同结果后竞争同一主键。主键只防重复行，
    不定义谁赢 —— 这里明确选先写者，因为"权威"的意义在于稳定，不在于新。"""
    conn = _Conn()
    vs.record_many(conn, [{"image_sha256": "h", "funnel_res": _res()}], True, "", "m", "D", 1)
    sql, _ = conn.many[0]
    assert "ON DUPLICATE KEY UPDATE image_sha256=image_sha256" in sql, "后写者不得覆盖"
    assert conn.commits == 1


@pytest.mark.parametrize("res", [
    _res(degraded=True),                       # 一次性故障，缓存写入门同款拒绝
    _res(status="DISCARD_UNREADABLE"),         # A11：解码失败往往是一次性的
    _res(status="WHATEVER"),
])
def test_unrecordable_verdicts_are_not_persisted(res):
    """把一次性故障固化成**不会被淘汰**的记录，比不记录坏得多。"""
    conn = _Conn()
    assert vs.record_many(conn, [{"image_sha256": "h", "funnel_res": res}],
                          True, "", "m", "D", 1) == 0
    assert conn.many == []


def test_promoted_rows_do_not_fake_provenance():
    """存量缓存没有模型/时间/首篇信息 —— 拿当前值冒充历史事实会让溯源列不可信。"""
    conn = _Conn()
    vs.record_many(conn, [{"image_sha256": "h", "funnel_res": _res()}],
                   True, "", None, "D", 1, provenance="cache_promoted")
    row = conn.many[0][1][0]
    assert "cache_promoted" in row and None in row


def test_write_error_is_swallowed():
    assert vs.record_many(_Conn(raise_on="write"),
                          [{"image_sha256": "h", "funnel_res": _res()}],
                          True, "", "m", "D", 1) == 0


def test_funnel1_discards_are_tagged_as_such():
    """Funnel 1 是确定的几何判据、与 VLM 制度无关 —— 值得在记录里单独标出。"""
    conn = _Conn()
    vs.record_many(conn, [{"image_sha256": "h", "funnel_res": _res(
        status="DISCARD_DECORATIVE", reason="Funnel 1: Structural Heuristics")}],
        True, "", "m", "D", 1)
    assert "funnel1" in conn.many[0][1][0]


# ── 4. 内容纯度前提 ─────────────────────────────────────────────────────────

def test_default_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    assert vs.store_enabled() is False


def test_mutually_exclusive_with_doc_context(monkeypatch, capsys):
    """`RAG_VLM_DOC_CONTEXT` 把文档标题塞进 prompt ⇒ 判决不再是图片字节的纯函数，
    内容寻址主键随即失效（同一张图在不同文档下该有不同判决，却共用一行）。"""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv("RAG_VLM_DOC_CONTEXT", "true")
    assert vs.store_enabled() is False
    assert "互斥" in capsys.readouterr().out
    monkeypatch.setenv("RAG_VLM_DOC_CONTEXT", "0")
    assert vs.store_enabled() is True


# ── 5. 权威 status 覆盖 + 降级不静默 ────────────────────────────────────────

def test_recorded_status_is_pinned_over_a_fresh_decision():
    """RDS 命中但缓存未命中：caption 只能重掷（本表刻意不存它），但**图集不能因此改变**
    —— 这正是 E 的主目标：已判过的图免疫制度漂移。"""
    fresh = _res(status="DISCARD_DECORATIVE", image_category="decorative")
    out = vs.apply_verdict(fresh, {"status": "ROUTE_TO_VECTOR", "image_category": "unknown"})
    assert out["status"] == "ROUTE_TO_VECTOR"
    assert out["verdict_pinned"] is True
    assert out["visual_summary"] == fresh["visual_summary"], "caption 保留新算的值"
    assert "verdict-store" in out["reason"]


def test_unavailable_note_names_the_doc_and_count():
    note = vs.unavailable_note("DOC_X", 7)
    assert "DOC_X" in note and "7" in note and "VERDICT-STORE" in note


def test_extractor_routes_the_note_into_partial_loss(monkeypatch):
    """降级必须走 `partial_loss_notes` → NEEDS_REVIEW，不能静默定稿（codex BLOCKER-3：
    靠方案 F 兜住不成立 —— F 依赖同一个 RDS、自身 fail-open，且正文改一个字只产
    `source_changed` 普通审计）。"""
    import opensearch_pipeline.extraction.unified_extractor as ue
    warnings, stats = [], {"verdict_store_note": vs.unavailable_note("D", 3)}
    notes = ue._warn_verdict_store_unavailable(warnings, stats)
    assert notes and notes == warnings
    assert ue._warn_verdict_store_unavailable([], {}) == []
    assert ue._warn_verdict_store_unavailable([], None) == []


def test_note_uses_the_per_call_stats_channel_not_instance_state():
    """一个 UnifiedExtractor 会被跨文档并发复用 —— 实例属性会串篇。"""
    src = pathlib.Path("opensearch_pipeline/extraction/unified_extractor.py").read_text(
        encoding="utf-8")
    assert 'stats["verdict_store_note"]' in src
    assert "self._funnel_verdict_notes" not in src


def test_all_four_extract_paths_drain_the_note():
    """四个格式分支都要接，**且返回值真的汇进 partial_loss_notes** —— 只接一个等于三个
    格式静默定稿。

    ⚠️ 审查修正（2026-07-26）：初版只数调用点个数（`src.count(...) == 4`），而四个分支
    下游接法各不相同 —— 删掉 DOCX 那句 `partial_loss_notes=_verdict_notes,` 后全量 3464
    仍全绿，DOCX 静默定稿。现在同时钉住"调用了"与"接住了"。
    """
    src = pathlib.Path("opensearch_pipeline/extraction/unified_extractor.py").read_text(
        encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert code.count("_warn_verdict_store_unavailable(warnings, _funnel_stats)") == 4
    # 四个分支各自的承接方式（缺任一即有格式会静默定稿）
    for sink in ("_page_cap_notes.extend(_warn_verdict_store_unavailable",   # PDF
                 "_verdict_notes = _warn_verdict_store_unavailable"):        # DOCX
        assert sink in code, f"缺承接：{sink}"
    assert code.count("partial_notes.extend(_warn_verdict_store_unavailable") == 2  # XLSX/PPTX
    assert "partial_loss_notes=_verdict_notes," in code, "DOCX 的 note 没进 ExtractionResult"


def test_simulate_never_writes_verdicts():
    """simulate 的 mock 判决落进**不会被淘汰**的记录 = 永久投毒。

    守卫在**调用点**（`_process_embedded_images` 的 `if … and not self.simulate:`），
    不在 `_record_funnel_verdicts` 内部 —— 所以这里断言的是那个 `If` 结构本身。

    ⚠️ 审查修正（2026-07-26）：初版是 260 字符邻近 grep（`"not self.simulate" in head[-260:]`）
    外加一句恒真的 `assert seg`；拆掉守卫、上一行留个含该字面量的注释就能骗过。
    改用 AST：必须存在一个测试含 `not self.simulate` 的 `If`，且落库调用在其**子树内**。
    （我第一版修的时候还犯过另一个错：直接调 `_record_funnel_verdicts` 并期望它自己挡
    simulate —— 它不挡，守卫从来在外层。这条注释就是防下一个人重犯。）
    """
    import ast
    tree = ast.parse(pathlib.Path(
        "opensearch_pipeline/extraction/unified_extractor.py").read_text(encoding="utf-8"))
    guarded = [n for n in ast.walk(tree)
               if isinstance(n, ast.If) and "not self.simulate" in ast.unparse(n.test)
               and any(isinstance(c, ast.Call)
                       and getattr(c.func, "attr", None) == "_record_funnel_verdicts"
                       for c in ast.walk(n))]
    assert guarded, "_record_funnel_verdicts 的调用不在 `not self.simulate` 守卫之内"


def test_schema_discipline_files_updated():
    """F-35 纪律：新 schema 文件须同步 README 的 file→DB 矩阵与 CI loader 清单。"""
    assert "059_image_funnel_verdict.sql" in pathlib.Path(
        "scripts/ci_load_schema.sh").read_text(encoding="utf-8"), "CI loader 清单缺 059"
    assert "059_image_funnel_verdict.sql" in pathlib.Path(
        "schema/README.md").read_text(encoding="utf-8"), "schema/README 矩阵缺 059"


# ── 审查修复批次（2026-07-26）────────────────────────────────────────────────

def test_p01_fresh_sensitive_is_never_pinned_away():
    """**P0-1**：一条陈旧的 keep 记录不得把本轮刚认出的公章/证件图改回入索引。

    后果不只是"多一张图"：`node_detect_sensitive` 的「任一 asset 是 QUARANTINE ⇒ 整篇
    隔离」也随之永不触发，敏感图静默进向量索引与钉钉卡片。安全判定只能收紧、不能被历史
    记录放松 —— 同款纪律 `_vlm_cache_lookup` 的 legacy 回退白名单早就有（刻意排除该状态）。
    """
    out = vs.apply_verdict({"status": "QUARANTINE_SENSITIVE", "reason": "Funnel 3: Sensitive"},
                           {"status": "ROUTE_TO_VECTOR", "image_category": "step_screenshot"})
    assert out["status"] == "QUARANTINE_SENSITIVE"
    assert out["verdict_pin_declined"] == "fresh_QUARANTINE_SENSITIVE_wins"
    assert not out.get("verdict_pinned")


def test_p02_pin_declines_when_there_is_no_content_to_carry():
    """**P0-2**：要钉成 content-bearing 就必须真带得动内容。

    Funnel-1 结构性弃图压根没调过 VLM ⇒ 无 caption 无 OCR；钉回 keep 只会产出**空壳资产**：
    检索侧零可检文本、内容覆写 Path A/B 失去输入，而且这条"keep + 空 caption"还会被写进
    跨文档共享缓存永久固化。
    """
    out = vs.apply_verdict({"status": "DISCARD_DECORATIVE",
                            "reason": "Funnel 1: Structural Heuristics"},
                           {"status": "ROUTE_TO_VECTOR", "image_category": "x"})
    assert out["status"] == "DISCARD_DECORATIVE"
    assert out["verdict_pin_declined"] == "no_content_to_pin"


def test_p02_funnel3_discard_now_carries_content_so_pin_works():
    """P0-2 的**根因修复**：Funnel-3 弃图分支现在带上本次调用早已拿到的 caption/OCR，
    于是 E 在它的主场景（100% 单向 keep→DISCARD）上钉回来的是完整资产而非空壳。"""
    fresh = {"status": "DISCARD_DECORATIVE", "reason": "Funnel 3: Low Semantic Value",
             "visual_summary": "一只手正在接线", "ocr_text": "", "image_category": "unknown"}
    out = vs.apply_verdict(fresh, {"status": "ROUTE_TO_VECTOR", "image_category": "step_screenshot"})
    assert out["status"] == "ROUTE_TO_VECTOR" and out["verdict_pinned"] is True
    assert out["visual_summary"] == "一只手正在接线"


def test_p02_discard_branch_returns_content_fields():
    """源头钉子：`process_image` 的 Funnel-3 DISCARD 返回体必须含那三个键
    （它们本来就在同一次 `vlm_result` 里，只是曾被这条分支丢掉）。"""
    import ast
    import pathlib
    src = pathlib.Path("opensearch_pipeline/image_funnel_processor.py").read_text(encoding="utf-8")
    seg = src[src.index('"status": "DISCARD_DECORATIVE"',
                        src.index("Funnel 3] Discarded low-relevance")):][:900]
    for k in ("visual_summary", "vlm_annotation_map", "ocr_text"):
        assert f'"{k}"' in seg, f"Funnel-3 弃图返回体缺 {k}（E 的 pin 会因此产出空壳）"
    ast.parse(src)


def test_p17_cached_hits_are_also_pinned():
    """**P1-7**：pin 此前只作用于"需重跑 VLM"的图，缓存与 059 冲突时**缓存获胜** ——
    一次 RDS 抖动下写进缓存的漂移判决会被永久沿用，且**零信号**。"""
    import pathlib
    src = pathlib.Path("opensearch_pipeline/extraction/unified_extractor.py").read_text(
        encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    i_cached = code.index("hash_to_result = dict(hash_to_cached_result)")
    i_vlm = code.index("if need_vlm_hashes:")
    seg = code[i_cached:i_vlm]
    assert "for _h, _cached in list(hash_to_result.items())" in seg, \
        "缓存命中项未接受权威判决约束"
    assert "apply_verdict" in seg
