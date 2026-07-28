# -*- coding: utf-8 -*-
"""方案 F（2026-07-25）：资产集变化的可见性。

要解决的沉默：同一 doc 的新版本入库时，"漏斗存活图集比上一版本少了几张"目前完全没有
任何信号。实测（选项 A）缓存旧判决与今天的 VLM 有 6.5% 不一致、**全部单向
ROUTE_TO_VECTOR → DISCARD**，其中 4 张是 GT 明确期望的步骤配图。

本文件钉死四类契约：
  1. 集合提取：状态过滤、缺 image_index、**缺 assets 键 = unknown 而显式 [] = 空集**；
  2. 事件派生：高置信 drift 的三个必要条件、partial 降级、任一侧 unknown 绝不产出全增全删；
  3. 挂载点：**生产 skip-gate 路径仍能观察到漂移**（这条最关键——生产默认
     RAG_SKIP_UNCHANGED_REINGEST=true，正文没变就 continue，永远到不了 chunk 写入）；
  4. fail-open：比对/留痕失败不影响摄取。
"""
import json

from opensearch_pipeline import asset_set_diff as asd


def _asset(idx, status="ROUTE_TO_VECTOR"):
    d = {"status": status}
    if idx is not None:
        d["image_index"] = idx
    return d


# ── 1. 集合提取 ──────────────────────────────────────────────────────────────

def test_missing_assets_key_is_unknown_not_empty():
    """缺键 = unknown。把它当空集会把读取失败伪造成"整篇图没了"。"""
    s = asd.survivors({"doc_id": "d"})
    assert s.known is False
    assert asd.survivors(None).known is False


def test_explicit_empty_list_is_a_real_empty_set():
    s = asd.survivors({"assets": []})
    assert s.known is True and s.indices == frozenset() and s.complete is True


def test_only_clean_routes_count():
    s = asd.survivors({"assets": [
        _asset(1, "ROUTE_TO_VECTOR"), _asset(2, "ROUTE_TO_TEXT"),
        _asset(3, "DISCARD_DECORATIVE"), _asset(4, "QUARANTINE_SENSITIVE"),
    ]})
    assert s.indices == frozenset({1, 2})


def test_index_zero_is_valid():
    """`0` 是合法 image_index —— 判定必须用 is not None，不能用真值。"""
    s = asd.survivors({"assets": [_asset(0)]})
    assert s.indices == frozenset({0}) and s.n_unindexed == 0


def test_clean_asset_without_index_marks_partial():
    s = asd.survivors({"assets": [_asset(1), _asset(None)]})
    assert s.indices == frozenset({1}) and s.n_unindexed == 1 and s.complete is False


# ── 2. 事件派生 ──────────────────────────────────────────────────────────────

def _ev(prev_idx, cur_idx, rel=asd.SourceRelation.SAME_CHECKSUM,
        prev_unindexed=0, cur_unindexed=0, stage="SKIP_GATE"):
    prev = asd.SurvivorSet(frozenset(prev_idx), prev_unindexed, True)
    cur = asd.SurvivorSet(frozenset(cur_idx), cur_unindexed, True)
    return asd.build_event("d", 1, 2, prev, cur, rel, stage)


def test_same_checksum_with_removal_is_high_confidence_drift():
    """核心用例：同一份文件，图少了 —— 正是实测的 12/23/29 那种。"""
    e = _ev({12, 23, 29, 8}, {8})
    assert e["kind"] == "same_source_drift"
    assert e["removed"] == [8 + 0] * 0 + [12, 23, 29] and e["n_removed"] == 3
    assert asd.is_high_confidence_loss(e) is True


def test_partial_downgrades_high_confidence():
    """任一侧缺 image_index ⇒ 只能称 ordinal_diff_partial，不得高置信声称 removal。"""
    e = _ev({12, 8}, {8}, cur_unindexed=1)
    assert e["kind"] == "ordinal_diff_partial"
    assert asd.is_high_confidence_loss(e) is False


def test_changed_source_is_informational():
    e = _ev({1, 2}, {3}, rel=asd.SourceRelation.CHANGED)
    assert e["kind"] == "source_changed" and asd.is_high_confidence_loss(e) is False


def test_unknown_side_never_yields_all_removed():
    """读取失败绝不能变成"全删"或"全增"。"""
    known = asd.SurvivorSet(frozenset({1, 2, 3}), 0, True)
    assert asd.build_event("d", 1, 2, asd.UNKNOWN_SET, known,
                           asd.SourceRelation.SAME_CHECKSUM, "SKIP_GATE") is None
    assert asd.build_event("d", 1, 2, known, asd.UNKNOWN_SET,
                           asd.SourceRelation.SAME_CHECKSUM, "SKIP_GATE") is None


def test_no_change_yields_no_event():
    assert _ev({1, 2}, {1, 2}) is None


def test_removed_list_is_bounded_and_flagged():
    e = _ev(set(range(50)), set())
    assert len(e["removed"]) == asd.MAX_LISTED and e["removed_truncated"] is True
    assert e["n_removed"] == 50


def test_message_has_no_ocr_or_caption_and_is_bounded():
    e = _ev({1}, set())
    msg = asd.event_message(e)
    assert len(msg) <= 1500
    for k in ("ocr", "visual_summary", "caption"):
        assert k not in msg.lower()
    assert json.loads(msg)["kind"] == "same_source_drift"


# ── source_relation ─────────────────────────────────────────────────────────

def test_checksum_beats_etag():
    assert asd.source_relation("a", "a", "x", "y") == asd.SourceRelation.SAME_CHECKSUM
    assert asd.source_relation("a", "b", "x", "x") == asd.SourceRelation.CHANGED


def test_etag_is_medium_confidence_and_normalised():
    assert asd.source_relation(None, None, '"ABC"', "abc") == asd.SourceRelation.SAME_ETAG
    assert asd.source_relation(None, None, "", "abc") == asd.SourceRelation.UNKNOWN
    assert asd.source_relation(None, None, None, None) == asd.SourceRelation.UNKNOWN


def test_baseline_sql_requires_success_and_canonical_key():
    """基线必须是**成功服务**的版本，且要有 canonical 可比 —— 失败/跳过/隔离版本不得入选。"""
    sql = asd.baseline_sql()
    assert "index_status='SUCCESS'" in sql
    assert "canonical_json_key IS NOT NULL" in sql
    assert "version_no<%s" in sql and "ORDER BY version_no DESC" in sql


def test_event_key_is_stable_but_not_claimed_idempotent():
    a = asd.event_key("d", 1, 2)
    assert a == asd.event_key("d", 1, 2) and a != asd.event_key("d", 1, 3)
    # kb_audit_log 目前没有业务唯一键 —— docstring 必须明说不保证幂等
    assert "不构成幂等保证" in asd.event_key.__doc__


# ── 3. 挂载点：生产 skip-gate 路径 ───────────────────────────────────────────

def test_skip_gate_mount_point_precedes_any_write():
    """**最关键的一条**：生产默认 RAG_SKIP_UNCHANGED_REINGEST=true，正文 hash 相同即在
    node_build_canonical 里 continue、从 canonicals 排除、永远到不了 node_write_chunk_meta。
    只挂 DAG 2 会把本观测要抓的核心场景整个漏掉。

    P1-5（2026-07-26）起这条挂载点还多一个**写序不变量**：它必须在 SKIPPED_DUPLICATE 的
    UPDATE **之前** —— 因为资产集新增会撤销 skip，而状态行一旦写下并 commit，再撤销就会
    留下自相矛盾的行（document_version 说 SKIPPED、文档却照常入库）。

    用 AST 判"真实调用"而非源码子串：注释掉的行同样含那些字面量。
    """
    import ast
    import pathlib
    src = pathlib.Path("opensearch_pipeline/pipeline_nodes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    emit = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "_emit_asset_set_diff"
            and any(k.arg == "observed_stage" and isinstance(k.value, ast.Constant)
                    and k.value.value == "SKIP_GATE" for k in n.keywords)]
    assert emit, "skip-gate 没有以 observed_stage='SKIP_GATE' 真实调用 _emit_asset_set_diff"
    # skip-gate 时 RDS 尚无 checksum，当前 checksum 只能取 ctx['_raw_checksum']
    assert any("_raw_checksum" in ast.unparse(k.value)
               for n in emit for k in n.keywords if k.arg == "cur_checksum"), \
        "cur_checksum 未取自 ctx['_raw_checksum']"
    # 写序不变量：emit 必须出现在 SKIPPED_DUPLICATE 的 UPDATE 之前
    upd = [n for n in ast.walk(tree)
           if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "execute"
           and n.args and isinstance(n.args[0], ast.Constant)
           and "SKIPPED_DUPLICATE" in str(n.args[0].value)]
    assert upd, "找不到 SKIPPED_DUPLICATE 的 UPDATE"
    assert min(n.lineno for n in emit) < min(n.lineno for n in upd), \
        "资产集比对必须在写 SKIPPED_DUPLICATE 之前（否则撤销 skip 会留下矛盾状态行）"


def test_chunk_committed_mount_point_is_a_real_call(monkeypatch):
    """DAG-2 挂载点必须是**真调用**，不是一句含该字面量的注释。

    ⚠️ 审查修正（2026-07-26）：初版是 `assert 'observed_stage="CHUNK_COMMITTED"' in src`，
    把那行换成带同字面量的注释后全量 3464 仍全绿 —— 与 skip-gate 侧（删掉即 5 条转红）
    的覆盖强度严重不对称。改用 AST 找实参为该字面量的真实 `Call`。
    """
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path("opensearch_pipeline/pipeline_nodes.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_emit_asset_set_diff"
             and any(k.arg == "observed_stage"
                     and isinstance(k.value, ast.Constant)
                     and k.value.value == "CHUNK_COMMITTED" for k in n.keywords)]
    assert calls, "DAG-2 没有以 observed_stage='CHUNK_COMMITTED' 真实调用 _emit_asset_set_diff"


def test_dataworks_loader_preserves_assets_presence():
    """stage 1→2 边界不得把"缺 assets 键"抹成空集（否则伪造全量 removal）。"""
    import pathlib
    src = pathlib.Path("opensearch_pipeline/dataworks_orchestrator.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert 'content_json.get("assets", [])' not in code, "loader 仍把缺键抹成空集"
    assert 'if "assets" in content_json:' in code


# ── 4. fail-open ────────────────────────────────────────────────────────────

def test_emit_is_fail_open(monkeypatch, capsys):
    """比对/留痕抛异常绝不能影响摄取。"""
    import opensearch_pipeline.pipeline_nodes as pn
    monkeypatch.setattr(pn, "_get_db_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pn._emit_asset_set_diff({}, {"doc_id": "d", "version_no": 2, "assets": []},
                            observed_stage="SKIP_GATE", cur_checksum="x", simulate_db=False)
    assert "asset-set diff 观测失败" in capsys.readouterr().out


def test_first_ingest_and_simulate_are_skipped(monkeypatch):
    """v1 首灌与 simulate 不产生事件，也不应触发任何 DB 访问。"""
    import opensearch_pipeline.pipeline_nodes as pn
    called = []
    monkeypatch.setattr(pn, "_get_db_conn", lambda *a, **k: called.append(1))
    pn._emit_asset_set_diff({}, {"doc_id": "d", "version_no": 1, "assets": []},
                            observed_stage="SKIP_GATE", simulate_db=False)
    pn._emit_asset_set_diff({}, {"doc_id": "d", "version_no": 3, "assets": []},
                            observed_stage="SKIP_GATE", simulate_db=True)
    assert called == []


def test_unknown_current_side_short_circuits(monkeypatch):
    """当前侧 unknown（canonical 缺 assets 键）⇒ 直接返回，不查库、不产出结论。"""
    import opensearch_pipeline.pipeline_nodes as pn
    called = []
    monkeypatch.setattr(pn, "_get_db_conn", lambda *a, **k: called.append(1))
    pn._emit_asset_set_diff({}, {"doc_id": "d", "version_no": 2},
                            observed_stage="SKIP_GATE", simulate_db=False)
    assert called == []


# ── P1-8（审查 2026-07-26）：抽取降级不得被读成"图集漂移" ──────────────────

def test_p18_degraded_extraction_is_not_a_clean_empty_set():
    """`survivors()` 只把**缺键**当 unknown，显式 `[]` 一律当真空集 —— 而"整段图提取失败"
    产出的正是一个**干净的 `[]`**（缺 PyMuPDF 时 `import fitz` 失败只 print、返回 []）。
    于是基线的每一张图都被算成 removed 并升为最高置信 same_source_drift，打出
    "图会从答案里消失"，而实际上一张都没丢。
    """
    assert asd.extraction_degraded({"assets": [], "warnings": ["pdf_image_extract_failed: …"]})
    assert asd.extraction_degraded({"assets": [], "vlm_degraded_count": 2})
    assert asd.extraction_degraded({"assets": [], "partial_loss_notes": ["ocr_partial"]})
    assert not asd.extraction_degraded({"assets": [], "warnings": []})
    assert not asd.extraction_degraded({"assets": []})


def test_p18_partial_downgrade_blocks_high_confidence():
    prev = asd.SurvivorSet(frozenset({1, 2, 3, 4}), 0, True)
    degraded = asd.SurvivorSet(frozenset(), 1, True)          # 被标 partial
    e = asd.build_event("d", 1, 2, prev, degraded, asd.SourceRelation.SAME_CHECKSUM, "SKIP_GATE")
    assert e["kind"] == "ordinal_diff_partial"
    assert asd.is_high_confidence_loss(e) is False, "降级抽取不得产出高置信丢图告警"


def test_p18_root_cause_signal_exists_at_source():
    """根因在抽取器：两处静默失败此前**只 print**、不写 stats ⇒ 下游无从区分。"""
    import pathlib
    src = pathlib.Path("opensearch_pipeline/extraction/image_extraction_utils.py").read_text(
        encoding="utf-8")
    assert src.count('stats["pdf_image_extract_failed"]') == 2, \
        "import fitz 失败与 fitz.open 失败都必须写 stats"
