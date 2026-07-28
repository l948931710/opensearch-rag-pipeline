# HA3 RAG — End-to-End Evaluation Report

- **Run**: eval_refreeze_20260726  |  **Table**: `?`  |  **Generated**: 20260726_185818
- **Gold cases run**: 258  |  **LLM**: `qwen3.7-plus` (thinking OFF)  |  **Judge**: Claude panel (independent of generator)
- **Env**: test, simulate=false, endpoint=`ha-cn-kgl4slr1n01.public.ha.aliyuncs.com` (read-only)

## Verdict: 36 passed / 6 failed / 2 n-a

| Gate | Target | Value | Result |
|---|---|---|---|
| index_health (L0) | all gates pass | True | ✅ PASS |
| retrieval recall@5 (L1) | >= 0.85 (xlsx 正例召回率@5) | 0.9506 | ✅ PASS |
| source attribution recall@1 (L1, 来源标注) | >= 0.95 (xlsx 来源标注) | 0.947 | ❌ FAIL |
| xlsx RAG retrieval recall@5 (L1, clean gold) | >= 0.85 | 1.0 | ✅ PASS |
| score calibration (L2) | labels still fit | True | ✅ PASS |
| off-topic discrimination AUC (L2) | >= 0.85 (pos vs off-topic-negative top-1 separation) | 0.978 | ✅ PASS |
| thinking-off verified (L3) | 0 reasoning leaks | 0 | ✅ PASS |
| positive over-refusal (L3) | <= 0.10 (hard refusals) | 0.0 | ✅ PASS |
| answer source-leak (L3) | <= 0.05 | 0.0044 | ✅ PASS |
| answer keyword-coverage (L3, 完整性) | >= 0.70 | 0.7043 | ✅ PASS |
| binding pdf Jaccard (L4-ing) | >= 0.78 hard (#F-mm8 升档) | 0.9132 | ✅ PASS |
| binding xlsx Jaccard (L4-ing) | >= 0.85 hard (D7 升档) | 1.0 | ✅ PASS |
| binding docx Jaccard (L4-ing) | >= 0.95 hard (D6 设定) | 0.9898 | ✅ PASS |
| img_dup_factor p95 (L4-ing, 全格式) | <= 1.20 hard (>1.5 是已知 over-attach bug) | 1.0 | ✅ PASS |
| <<IMG:N>> marker validity (L4-srv) | >= 0.95 hard / >= 0.98 soft | 0.6667 | ❌ FAIL |
| dangling 口惠图但卡片无图 (L4-srv) | <= 0.05 hard (扩正则后) | 0.0 | ✅ PASS |
| orphan rate (L4-srv, trend 监控) | <= 0.30 soft (trend, advisory) | 0.5921 | ❌ FAIL |
| marker distinctness (L4-srv, advisory) | advisory — 1.0 = no image reuse (lower = bundled/reused markers) | 0.9118 | ❌ FAIL |
| answer image rate (L4-srv, advisory) | advisory — trend (锁分布后再定阈值; 主臂=生产 cosurface=False 姿态) | 0.7241 | ➖ N/A |
| permission filtering (L5) | no leak + injection-safe | True | ✅ PASS |
| chunk-quality verdict (L6) | GO (all hard gates measured & pass) | GO | ✅ PASS |
| [L6-hard] tokens in [5,2000] (B) | = 0 chunks out of band | 0 | ✅ PASS |
| [L6-hard] no oversize structural chunk (B/A2) | = 0 | 0 | ✅ PASS |
| [L6-hard] orphan step_cards = 0 (A/D4) | = 0 | 0 | ✅ PASS |
| [L6-hard] procedure_parent balance (A/D7) | missing=0 ∧ duplicate=0 | {'missing': 0, 'duplicate': 0} | ✅ PASS |
| [L6-hard] RDS↔HA3 step_card drift (A/D1) | sym_diff <= 15 (0.5%) | 0 | ✅ PASS |
| [L6-hard] image_refs shape compliance (A/D6) | >= 0.95 | 0.9834 | ✅ PASS |
| [L6-hard] img_dup_factor p95 (F) | <= 1.20 | 1.0 | ✅ PASS |
| [L6-hard] image_refs JSON parseable (F) | = 0 malformed | 0 | ✅ PASS |
| [L6-hard] RDS↔HA3 all-type id-set (H) | missing=0 ∧ extra=0 | {'missing': 0, 'extra': 0} | ✅ PASS |
| [L6-soft] mid-sentence cut rate (B) | CI-upper <= 0.05 | 0.5001 | ❌ FAIL |
| [L6-soft] dangling-anaphor rate (C) | CI-upper <= 0.05 | 0.0038 | ✅ PASS |
| [L6-soft] cross-doc near-dup factor (E) | <= 1.10 | 1.0271 | ✅ PASS |
| [L6-soft] routing-family match rate (D) | CI-lower >= 0.95 (soft, metadata-level) | 0.897 | ❌ FAIL |
| judge inter-rater agreement (L6 chunk panel) | mean overall inter-judge stdev <= 1.2 (1-5 scale) | 0.201 | ✅ PASS |
| answer faithfulness (Claude, L3) | >= 4.0 / 5 | 4.911 | ✅ PASS |
| answer correctness (Claude, L3) | >= 4.0 / 5 | 4.449 | ✅ PASS |
| answer completeness (Claude, L3) | >= 4.0 / 5 | 4.221 | ✅ PASS |
| positive fabrication (Claude, L3) | <= 0.05 | 0.022 | ✅ PASS |
| negative fabrication (Claude, L3) | <= 0.10 | 0.0 | ✅ PASS |
| judge inter-rater agreement (L3 panel) | mean overall inter-judge stdev <= 1.2 (1-5 scale; lower=more agreement) | 0.079 | ✅ PASS |
| image relevance (judge-mm, advisory) | >= 3.5 advisory（caption 贴题率;先锁两轮分布再议升 hard） | 3.914 | ✅ PASS |
| fusion/calibration regime (guard) | fusion == 'weighted' AND rerank == True (thresholds' calibration regime == production serving regime) | active fusion=weighted, rerank=True | ✅ PASS |
| baseline regression (regime) | baseline regime must match run regime to compare | REGIME MISMATCH on ['l4_ingestion_evaluator_version', 'funnel_policy'] — baseline not comparable | ➖ N/A |

## L0 — Index Health

- status/docCount: {"pass": true, "status": "IN_USE", "docCount": 27484, "rds_active": 27484, "delta": 0, "tolerance": 137, "interpretation": "exact match"}
- dense self-query: {"pass": true, "healthy": 60, "total": 60, "id_exact_match": 54, "identical_text_sibling": 6, "self_score_min_seen": 1.0, "self_score_mean": 1.0, "fails_sample": [], "note": "healthy = self-score>=0.99 returning same chunk or an identical-text sibling"}
- sparse self-query: {"pass": true, "ok": 40, "total": 40, "embed_sparse_ok": 40, "method": "stored sparse_data non-empty on dense self-match + query-embedding sparse non-empty (was: zero-dense sparse-only query — regime-fragile, replaced 2026-06-17)", "note": "sparse vector built in index & embeddable (else hybrid collapses to BM25)"}
- vector fidelity (drift): {"pass": true, "n": 60, "cos_mean": 1.0, "cos_min": 1.0, "note": "stored index vector vs fresh embedding (cos~1.0 => no drift/corruption)"}
- duplicate-content diagnostic: {"exact_id_self_match_rate": 0.9, "identical_text_sibling_rate": 0.1, "note": "high identical-text-sibling rate => duplicate chunk content (chunking quality), not an index fault"}

## L1 — Retrieval Ranking

- scorable positives: 165 public / 165 total  |  permission-gated excluded: 0  |  negatives: 33
- **ranking (single-target)**: {"n_queries": 162, "recall@1": 0.8642, "recall@1_ci": [0.8086, 0.9136], "recall@3": 0.9506, "recall@3_ci": [0.9136, 0.9815], "recall@5": 0.9506, "recall@5_ci": [0.9136, 0.9815], "recall@10": 0.9506, "recall@10_ci": [0.9136, 0.9815], "mrr": 0.9049, "mrr_ci": [0.8632, 0.9414], "found_rate": 0.9753, "ndcg@10": 0.8797, "ndcg@10_ci": [0.8411, 0.9142]}
- ranking (multi-doc, single-rank proxy): {"n_queries": 3, "recall@1": 0.0, "recall@1_ci": [0.0, 0.0], "recall@3": 0.0, "recall@3_ci": [0.0, 0.0], "recall@5": 0.0, "recall@5_ci": [0.0, 0.0], "recall@10": 0.0, "recall@10_ci": [0.0, 0.0], "mrr": 0.0133, "mrr_ci": [0.0, 0.04], "found_rate": 0.3333}
- content-hit rate (keyword GT in retrieved context, robust to mislabeled gold): 0.793 over 29 cases
- by module: {"rag_retrieval": {"n": 29, "recall@1": 1.0, "recall@5": 1.0, "mrr": 1.0}, "nlq": {"n": 89, "recall@1": 0.82, "recall@5": 0.944, "mrr": 0.879}, "source_attribution": {"n": 19, "recall@1": 0.947, "recall@5": 1.0, "mrr": 0.974}, "rag_retrieval_json": {"n": 25, "recall@1": 0.8, "recall@5": 0.88, "mrr": 0.833}}
- by source: {"xlsx": {"n": 137, "recall@1": 0.876, "recall@5": 0.964, "mrr": 0.918}, "json_text": {"n": 25, "recall@1": 0.8, "recall@5": 0.88, "mrr": 0.833}}
- by difficulty: {"None": {"n": 145, "recall@1": 0.883, "recall@5": 0.966, "mrr": 0.922}, "cross_chunk": {"n": 9, "recall@1": 0.889, "recall@5": 0.889, "mrr": 0.889}, "single_chunk": {"n": 3, "recall@1": 0.0, "recall@5": 0.667, "mrr": 0.278}, "disambiguation": {"n": 2, "recall@1": 0.5, "recall@5": 0.5, "mrr": 0.5}, "query_robustness": {"n": 3, "recall@1": 1.0, "recall@5": 1.0, "mrr": 1.0}}
- latency (ms): {"mean": 10137.3, "p50": 9848, "p90": 14362, "p95": 15774, "p99": 19243}

## L2 — Score Calibration

- thresholds: {"high": 0.9, "medium": 0.8}
- n_correct_top1_positives: 140
- positive_top1_mean: 0.9101
- negative_top1_mean: 0.6991
- separation_pos_minus_neg: 0.211
- label_bands_on_correct_hits: {"高": 97, "中": 32, "低": 11}
- frac_高: 0.693
- frac_at_least_中: 0.921
- frac_negatives_in_高: 0.182
- neg_high_by_type: {"off_topic": 0.0, "untyped": 0.231}
- n_offtopic_neg: 7
- separation_auc_offtopic: 0.978
- thresholds_ok: true
  - ⚠️ Score labels fit: positives calibrate; off-topic discrimination measured/within bounds.

## L3 — Answer Quality (deterministic)

```json
{
 "n_answered": 258,
 "errors": [],
 "reasoning_leak_count": 0,
 "positive": {
  "n": 225,
  "n_scorable": 165,
  "n_unresolved_gold": 60,
  "over_refusal_rate": 0.0,
  "coverage_gap_refusal_rate": 0.2333,
  "over_refusal_rate_all_positives": 0.0622,
  "soft_decline_rate": 0.0667,
  "source_leak_rate": 0.0044,
  "mean_keyword_coverage": 0.7043,
  "mean_chars": 490.9
 },
 "negative": {
  "n": 33,
  "interception_rate_rulebased": 0.6061,
  "source_leak_rate": 0.0
 },
 "mean_latency_ms": 7326.5
}
```

## L3 — Answer Quality (Claude panel)

```json
{
 "n_judges": 3,
 "judges": [
  "claude-auto-1",
  "claude-auto-2",
  "claude-auto-3"
 ],
 "positives": {
  "faithfulness": {
   "mean": 4.911,
   "ci": [
    4.879,
    4.942
   ],
   "n": 225
  },
  "correctness": {
   "mean": 4.449,
   "ci": [
    4.319,
    4.576
   ],
   "n": 225
  },
  "completeness": {
   "mean": 4.221,
   "ci": [
    4.07,
    4.37
   ],
   "n": 225
  },
  "relevance": {
   "mean": 4.693,
   "ci": [
    4.597,
    4.782
   ],
   "n": 225
  },
  "overall": {
   "mean": 4.44,
   "ci": [
    4.317,
    4.56
   ],
   "n": 225
  }
 },
 "binding": null,
 "negatives": {
  "overall": {
   "mean": 4.455,
   "ci": [
    4.131,
    4.748
   ],
   "n": 33
  },
  "faithfulness": {
   "mean": 4.909,
   "ci": [
    4.808,
    4.98
   ],
   "n": 33
  },
  "fabrication_rate": 0.0,
  "n": 33
 },
 "positives_fabrication_rate": 0.022,
 "mean_overall_interjudge_stdev": 0.079,
 "pass_rate_overall_ge4_positives": 0.804
}
```

## L4-ingestion — 摄入侧图文绑定精度(逐格式 Jaccard)

```json
{
 "img_dup_factor_p95": 1.0,
 "img_dup_factor_max": 1.0,
 "n_degraded_docs": 2,
 "errors": [],
 "per_fmt": {
  "pdf": {
   "n_docs": 6,
   "n_degraded_docs": 0,
   "n_strong_chunks": 48,
   "mean_jaccard": 0.9131958333333333,
   "std_jaccard": 0.2436643811076362
  },
  "xlsx": {
   "n_docs": 4,
   "n_degraded_docs": 1,
   "n_strong_chunks": 19,
   "mean_jaccard": 1.0,
   "std_jaccard": 0.0
  },
  "pptx": {
   "n_docs": 3,
   "n_degraded_docs": 1,
   "n_strong_chunks": 0,
   "mean_jaccard": null,
   "std_jaccard": null
  },
  "docx": {
   "n_docs": 20,
   "n_degraded_docs": 3,
   "n_strong_chunks": 689,
   "mean_jaccard": 0.9898,
   "std_jaccard": 0.013902530839305175,
   "_source": "strict_fixture"
  },
  "unknown": {
   "n_docs": 4,
   "n_degraded_docs": 0,
   "n_strong_chunks": 0,
   "mean_jaccard": null,
   "std_jaccard": null
  }
 },
 "binding_jaccard_pdf": 0.9131958333333333,
 "binding_jaccard_xlsx": 1.0,
 "binding_jaccard_pptx": null,
 "binding_jaccard_docx": 0.9898,
 "binding_jaccard_unknown": null
}
```

- evaluated docs: 53  |  judge_bundle_binding items: 52

## L4-serving — `<<IMG:N>>` 摆放质量(LLM 行为)

```json
{
 "n_answers": 35,
 "n_answers_with_images": 29,
 "answer_image_rate": 0.7241379310344828,
 "interleave_rate": 0.7241379310344828,
 "orphan_rate": 0.5921052631578947,
 "marker_validity": 0.6666666666666666,
 "marker_distinctness": 0.9117647058823529,
 "dangling_ref_rate": 0.0,
 "over_cap_rate": 0.27586206896551724,
 "placement_rate": 0.7241379310344828,
 "avg_images_shown": 3.103448275862069,
 "total_markers": 102,
 "total_invalid_markers": 34,
 "total_inrange_markers": 68,
 "total_distinct_markers": 62,
 "n_referenced_none_strategy": 8,
 "n_appended_strategy": 8
}
```

## L5 — Permission Filtering

```json
{
 "applicable": true,
 "n_gated_docs_tested": 5,
 "public_exclusion_ok": 5,
 "authorized_visibility_ok": 5,
 "no_public_leak": true,
 "injection_safe": true,
 "probes": [
  {
   "doc_id": "DOC_PRODUCTION_20260622141323_7BFA8A",
   "title": "包材规格书-纸杯亚太红蓝款-富岭.xlsx",
   "owner_dept": "production",
   "permission_level": "dept_internal",
   "leaked_on_public_path": false,
   "visible_to_authorized_dept": true
  },
  {
   "doc_id": "DOC_PRODUCTION_20260622202139_6EDAB1",
   "title": "GB 9685-2016 食品安全国家标准  食品接触材料及制品用添加剂使用标准.pdf",
   "owner_dept": "production",
   "permission_level": "dept_internal",
   "leaked_on_public_path": false,
   "visible_to_authorized_dept": true
  },
  {
   "doc_id": "DOC_PRODUCTION_20260622085831_899899",
   "title": "磨砂注塑杯（中杯、大杯、超大杯）、超大杯盖、500黑色杯盖产品规格书-富岭20250113_1738900812858.pdf",
   "owner_dept": "production",
   "permission_level": "dept_internal",
   "leaked_on_public_path": false,
   "visible_to_authorized_dept": true
  },
  {
   "doc_id": "DOC_RD_20260622105055_AA3F61",
   "title": "GB 4806.10-2025 食品安全国家标准 食品接触材料及制品用涂料及涂层.pdf",
   "owner_dept": "rd",
   "permission_level": "dept_internal",
   "leaked_on_public_path": false,
   "visible_to_authorized_dept": true
  },
  {
   "doc_id": "DOC_PRODUCTION_20260622085737_E34C05",
   "title": "可降解小勺、全降解水果叉、可降解水果针、PLA奶盖纸碗产品规格书-富岭20250123_1738900796715.pdf",
   "owner_dept": "production",
   "permission_level": "dept_internal",
   "leaked_on_public_path": false,
   "visible_to_authorized_dept": true
  }
 ],
 "injection": [
  {
   "payload": "x\" OR permission_level=\"dept_internal",
   "leaked_gated_doc": false,
   "error": null
  },
  {
   "payload": "x\" OR \"1\"=\"1",
   "leaked_gated_doc": false,
   "error": null
  },
  {
   "payload": "*",
   "leaked_gated_doc": false,
   "error": null
  },
  {
   "payload": "admin\" OR owner_dept=\"",
   "leaked_gated_doc": false,
   "error": null
  }
 ],
 "PASS": true
}
```

## L6 — Chunk-Artifact Content Quality

- **verdict**: `GO`  |  go_no_go: True  |  D1-D7 source: loaded
- fingerprint: {"active_chunks": 27484, "active_docs": 1481, "chunk_id_set_hash": "a8752660cb5ed728", "d7_json_hash": "62f2476c2b048c99", "code_commit": "277f740", "rubric_version": "chunk_rubric_v1"}
- RDS↔HA3 id-set (H): {"rds_active": 27484, "ha3_returned": 27484, "ha3_unique": 27484, "truncated": false, "unhealthy_buckets": {}, "missing_in_ha3": 0, "extra_in_ha3": 0, "idset_jaccard": 1.0, "missing_sample": [], "extra_sample": []}

### L6/boundary
```json
{
 "n_chunks": 27484,
 "size_distribution_by_type": {
  "clause_chunk": {
   "n": 6801,
   "min": 19,
   "max": 661,
   "mean": 158.0293,
   "median": 105,
   "stdev": 131.2953,
   "p25": 61,
   "p50": 105,
   "p75": 220,
   "p90": 374
  },
  "image": {
   "n": 3217,
   "min": 33,
   "max": 289,
   "mean": 66.6139,
   "median": 67,
   "stdev": 11.2979,
   "p25": 59,
   "p50": 67,
   "p75": 73,
   "p90": 80
  },
  "ocr_chunk": {
   "n": 1294,
   "min": 25,
   "max": 1869,
   "mean": 187.9104,
   "median": 181.5,
   "stdev": 93.9354,
   "p25": 134,
   "p50": 181,
   "p75": 247,
   "p90": 293
  },
  "procedure_parent": {
   "n": 312,
   "min": 38,
   "max": 1820,
   "mean": 298.3013,
   "median": 215.0,
   "stdev": 269.3363,
   "p25": 146,
   "p50": 214,
   "p75": 328,
   "p90": 579
  },
  "step_card": {
   "n": 4854,
   "min": 7,
   "max": 1840,
   "mean": 124.5733,
   "median": 62.0,
   "stdev": 141.4042,
   "p25": 31,
   "p50": 62,
   "p75": 195,
   "p90": 299
  },
  "table_chunk": {
   "n": 7256,
   "min": 7,
   "max": 1556,
   "mean": 129.9097,
   "median": 99.0,
   "stdev": 137.8359,
   "p25": 55,
   "p50": 99,
   "p75": 144,
   "p90": 224
  },
  "text_chunk": {
   "n": 3646,
   "min": 6,
   "max": 1961,
   "mean": 158.1061,
   "median": 146.0,
   "stdev": 123.6815,
   "p25": 61,
   "p50": 146,
   "p75": 222,
   "p90": 303
  },
  "visual_knowledge": {
   "n": 104,
   "min": 8,
   "max": 334,
   "mean": 93.3269,
   "median": 84.0,
   "stdev": 48.9408,
   "p25": 72,
   "p50": 84,
   "p75": 99,
   "p90": 154
  }
 },
 "oversize_count": 0,
 "oversize_structural_count": 0,
 "oversize_sample": [],
 "undersize_count": 0,
 "undersize_sample": [],
 "token_drift_count": 0,
 "token_drift_sample": [],
 "n_prose": 10447,
 "midsentence_cut_rate": 0.5001,
 "midsentence_cut_ci": [
  0.4807,
  0.5189
 ],
 "midsentence_unique_docs": 1256,
 "orphan_heading_rate": 0.0,
 "orphan_heading_ci_upper": 0.0
}
```

### L6/self_containedness
```json
{
 "n_prose": 10447,
 "dangling_anaphor_rate": 0.0038,
 "dangling_anaphor_ci_upper": 0.006,
 "sample": [
  {
   "chunk_id": "DOC_PRODUCTION_20260513120639_E1D6F8_v2_c0001_E3D6D73F",
   "doc_id": "DOC_PRODUCTION_20260513120639_E1D6F8",
   "preview": "此外车间涉及报废流程：返工明细单---品质主管签字报废---转接主任助理出报废明细单--转主管主任签字(主任主管商议责任"
  },
  {
   "chunk_id": "DOC_FINANCE_20260620150139_BCAE22_v1_c0000_60D9E2A8",
   "doc_id": "DOC_FINANCE_20260620150139_BCAE22",
   "preview": "其他应收款的管理\n控制目标：\n■保证公司资金的安全，减少风险\n■保证公司的付款符合公司的政策，并能够提高公司资金的利用效"
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260621124422_287ACB_v1_c0001_F11F27D0",
   "doc_id": "DOC_PRODUCTION_20260621124422_287ACB",
   "preview": "此控制卡的模具采购指：拉片模具采购、成型模具采购及其内部主要配件（刀口、型腔、拉伸\n头等）的批量采购（数量≥5）。 序\n"
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260621124422_287ACB_v1_c0011_4D847021",
   "doc_id": "DOC_PRODUCTION_20260621124422_287ACB",
   "preview": "此控制卡的模具采购指：拉片模具采购、成型模具采购及其内部主要配件（刀口、型腔、拉伸\n头等）的批量采购（数量≥5）。 序\n"
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260621124426_B714A3_v1_c0001_75D276BD",
   "doc_id": "DOC_PRODUCTION_20260621124426_B714A3",
   "preview": "此控制卡的模具采购指：拉片模具采购、成型模具采购及其内部主要配件（刀口、型腔、拉伸\n头等）的批量采购（数量≥5）。 序\n"
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260621124426_B714A3_v1_c0009_AC006114",
   "doc_id": "DOC_PRODUCTION_20260621124426_B714A3",
   "preview": "此控制卡的模具采购指：拉片模具采购、成型模具采购及其内部主要配件（刀口、型腔、拉伸\n头等）的批量采购（数量≥5）。 序\n"
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260621153131_7BE60A_v1_c0003_F29DAC6A",
   "doc_id": "DOC_PRODUCTION_20260621153131_7BE60A",
   "preview": "其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n生产工艺流程图\n\n"
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260621163118_918F4D_v1_c0003_EC25AE56",
   "doc_id": "DOC_PRODUCTION_20260621163118_918F4D",
   "preview": "其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n生产工艺流程图\n\n"
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260621225851_3BA8D5_v1_c0005_260ED19A",
   "doc_id": "DOC_PRODUCTION_20260621225851_3BA8D5",
   "preview": "其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n生产工艺流程图\n\n"
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260621225954_946B8E_v1_c0005_871472D8",
   "doc_id": "DOC_PRODUCTION_20260621225954_946B8E",
   "preview": "其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n生产工艺流程图\n\n"
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260622023314_7EAB04_v1_c0003_7C2BF10A",
   "doc_id": "DOC_PRODUCTION_20260622023314_7EAB04",
   "preview": "其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n生产工艺流程图\n\n"
  },
  {
   "chunk_id": "DOC_RD_20260622102310_464902_v1_c0001_EE3E07CF",
   "doc_id": "DOC_RD_20260622102310_464902",
   "preview": "此机器将对超过安全设置值的温度(T)，扭矩(M)和压力(p)发出警报。系统有下列安全值：T—最大360摄氏度，M—最大6"
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260622085722_B815D1_v1_c0003_7BED610B",
   "doc_id": "DOC_PRODUCTION_20260622085722_B815D1",
   "preview": "其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n生产工艺流程图\n\n"
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260622141313_E0E036_v1_c0003_6A939B8B",
   "doc_id": "DOC_PRODUCTION_20260622141313_E0E036",
   "preview": "其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n其他指标（使用性能、内控指标）\n\n生产工艺流程图\n\n"
  },
  {
   "chunk_id": "DOC_MARKETING_20260611201419_DF1486_v2_c0007_B90568F8",
   "doc_id": "DOC_MARKETING_20260611201419_DF1486",
   "preview": "其他可降解材料：PBS PBAT PLA CPLA PHA\n\nPBS :PBS 缓冲液，是生物化学研究中使用最为广泛的一"
  },
  {
   "chunk_id": "DOC_RD_20260622102422_2C25C6_v2_c0006_0D63B7D2",
   "doc_id": "DOC_RD_20260622102422_2C25C6",
   "preview": "其他国家测试要求（申请降解相关试验无需填写）：\n\n欧盟 南美 巴西 意大利 法国 美国\n\n玻璃陶瓷搪瓷：带盖 外部口缘2"
  },
  {
   "chunk_id": "DOC_SUPPLY_20260611201420_590293_v2_c0001_073F9C56",
   "doc_id": "DOC_SUPPLY_20260611201420_590293",
   "preview": "此控制卡所指的采购物料指：原料、盐胡椒、纸巾等实际边生产边检测的物料。 序\n号 流 程 责任部门 责任人 作业内容 1 "
  },
  {
   "chunk_id": "DOC_SUPPLY_20260611201420_D82A08_v2_c0001_3B656A05",
   "doc_id": "DOC_SUPPLY_20260611201420_D82A08",
   "preview": "此控制卡的新产品定义：在结构、材质、工艺等某一方面对比原有产品有改动。 序\n号 流 程 责任部门 责任人 作业内容 1 "
  },
  {
   "chunk_id": "DOC_PRODUCTION_20260622141220_1BCDD0_v2_c0004_A66FDFD7",
   "doc_id": "DOC_PRODUCTION_20260622141220_1BCDD0",
   "preview": "其他指标（使用性能、内控指标）\t\t漏水性\t试验后不应漏水\t\tGB/T18006.1\tGB/T18006.1\t∆\n\n其他指"
  }
 ]
}
```

### L6/dedup
```json
{
 "eligible_chunks": 26723,
 "exact_dup_groups": 1046,
 "exact_dup_same_doc": 565,
 "exact_dup_cross_doc": 481,
 "exact_dup_sample": [
  [
   "DOC_IT_20260513120632_AAF7F0_v3_c0003_FA0A9F96",
   "DOC_IT_20260513120632_AAF7F0_v3_c0023_413E1538"
  ],
  [
   "DOC_PRODUCTION_20260513120638_326895_v4_c0000_36C63258",
   "DOC_PRODUCTION_20260513120638_326895_v4_c0014_A8E53830"
  ],
  [
   "DOC_HR_20260514123022_4637BD_v2_c0007_A4E0B2A0",
   "DOC_HR_20260514123022_EBCB5C_v2_c0009_EC2CF5B6"
  ],
  [
   "DOC_FINANCE_20260611201418_02F466_v1_c0001_60269D53",
   "DOC_FINANCE_20260611201418_04BCE2_v1_c0001_49CB7A83"
  ],
  [
   "DOC_FINANCE_20260611201418_02F466_v1_c0002_38573FF1",
   "DOC_FINANCE_20260611201418_04BCE2_v1_c0002_4ED26BD0"
  ]
 ],
 "near_dup_pairs_same_doc": 96,
 "near_dup_pairs_cross_doc": 981,
 "near_dup_cross_factor": 1.0271,
 "near_dup_cross_sample": [
  {
   "a": "DOC_HR_20260514123022_4637BD_v2_c0007_A4E0B2A0",
   "b": "DOC_HR_20260514123022_EBCB5C_v2_c0009_EC2CF5B6",
   "jaccard": 1.0,
   "a_doc": "DOC_HR_20260514123022_4637BD",
   "b_doc": "DOC_HR_20260514123022_EBCB5C"
  },
  {
   "a": "DOC_FINANCE_20260611201418_02F466_v1_c0003_33B6AABD",
   "b": "DOC_FINANCE_20260611201418_04BCE2_v1_c0003_43FCECFD",
   "jaccard": 1.0,
   "a_doc": "DOC_FINANCE_20260611201418_02F466",
   "b_doc": "DOC_FINANCE_20260611201418_04BCE2"
  },
  {
   "a": "DOC_FINANCE_20260611201418_02F466_v1_c0011_0C74DBE0",
   "b": "DOC_FINANCE_20260611201418_04BCE2_v1_c0011_FEB82E17",
   "jaccard": 1.0,
   "a_doc": "DOC_FINANCE_20260611201418_02F466",
   "b_doc": "DOC_FINANCE_20260611201418_04BCE2"
  },
  {
   "a": "DOC_PRODUCTION_20260514123028_1993A7_v3_c0002_3C5459BE",
   "b": "DOC_PRODUCTION_20260514123028_782287_v3_c0002_3095E46F",
   "jaccard": 1.0,
   "a_doc": "DOC_PRODUCTION_20260514123028_1993A7",
   "b_doc": "DOC_PRODUCTION_20260514123028_782287"
  },
  {
   "a": "DOC_PRODUCTION_20260513120642_2A8CBA_v2_c0017_FCE8A8FB",
   "b": "DOC_PRODUCTION_20260513120642_6AF9D4_v2_c0017_3CB734EA",
   "jaccard": 1.0,
   "a_doc": "DOC_PRODUCTION_20260513120642_2A8CBA",
   "b_doc": "DOC_PRODUCTION_20260513120642_6AF9D4"
  },
  {
   "a": "DOC_PRODUCTION_20260513120642_2A8CBA_v2_c0018_207077E5",
   "b": "DOC_PRODUCTION_20260513120642_6AF9D4_v2_c0018_DE758834",
   "jaccard": 1.0,
   "a_doc": "DOC_PRODUCTION_20260513120642_2A8CBA",
   "b_doc": "DOC_PRODUCTION_20260513120642_6AF9D4"
  },
  {
   "a": "DOC_PRODUCTION_20260513120642_2A8CBA_v2_c0019_3C985AF0",
   "b": "DOC_PRODUCTION_20260513120642_6AF9D4_v2_c0019_62C51B55",
   "jaccard": 1.0,
   "a_doc": "DOC_PRODUCTION_20260513120642_2A8CBA",
   "b_doc": "DOC_PRODUCTION_20260513120642_6AF9D4"
  },
  {
   "a": "DOC_PRODUCTION_20260513120642_2A8CBA_v2_c0020_29CF9730",
   "b": "DOC_PRODUCTION_20260513120642_6AF9D4_v2_c0020_29501D61",
   "jaccard": 1.0,
   "a_doc": "DOC_PRODUCTION_20260513120642_2A8CBA",
   "b_doc": "DOC_PRODUCTION_20260513120642_6AF9D4"
  },
  {
   "a": "DOC_PRODUCTION_20260513120642_2A8CBA_v2_c0049_56C340C6",
   "b": "DOC_PRODUCTION_20260513120642_6AF9D4_v2_c0049_64089224",
   "jaccard": 1.0,
   "a_doc": "DOC_PRODUCTION_20260513120642_2A8CBA",
   "b_doc": "DOC_PRODUCTION_20260513120642_6AF9D4"
  },
  {
   "a": "DOC_PRODUCTION_20260621133441_D31EFA_v1_c0003_8B060D96",
   "b": "DOC_PRODUCTION_20260621133445_14DB1B_v1_c0003_CE4D5BAF",
   "jaccard": 1.0,
   "a_doc": "DOC_PRODUCTION_20260621133441_D31EFA",
   "b_doc": "DOC_PRODUCTION_20260621133445_14DB1B"
  }
 ],
 "blocking_truncated_blocks": 15
}
```

### L6/image_binding
```json
{
 "n_chunks_with_images": 1300,
 "malformed_json": 0,
 "img_dup_factor_p95": 1.0,
 "img_dup_factor_max": 1.0,
 "per_format": {
  "docx": {
   "n": 734,
   "p95": 1.0,
   "max": 1.0
  },
  "jpg": {
   "n": 2,
   "p95": 1.0,
   "max": 1.0
  },
  "pdf": {
   "n": 404,
   "p95": 1.0,
   "max": 1.0
  },
  "png": {
   "n": 3,
   "p95": 1.0,
   "max": 1.0
  },
  "pptx": {
   "n": 43,
   "p95": 1.0,
   "max": 1.0
  },
  "xlsx": {
   "n": 114,
   "p95": 1.0,
   "max": 1.0
  }
 },
 "overattach_sample": []
}
```

### L6/routing
```json
{
 "n_docs_checked": 1165,
 "routing_match_rate": 0.897,
 "routing_match_ci_lower": 0.8798,
 "mismatch_count": 120,
 "mismatch_sample": [
  {
   "doc_id": "DOC_PRODUCTION_20260514123027_F8D2F8",
   "expected": [
    "clause_chunk"
   ],
   "observed": [
    "text_chunk"
   ]
  },
  {
   "doc_id": "DOC_PRODUCTION_20260514123027_8C73BA",
   "expected": [
    "clause_chunk"
   ],
   "observed": [
    "text_chunk"
   ]
  },
  {
   "doc_id": "DOC_HR_20260514123022_BF412B",
   "expected": [
    "clause_chunk"
   ],
   "observed": [
    "text_chunk"
   ]
  },
  {
   "doc_id": "DOC_HR_20260514123019_BD1491",
   "expected": [
    "clause_chunk"
   ],
   "observed": [
    "text_chunk"
   ]
  },
  {
   "doc_id": "DOC_HR_20260514123025_CC99BF",
   "expected": [
    "clause_chunk"
   ],
   "observed": [
    "text_chunk"
   ]
  },
  {
   "doc_id": "DOC_HR_20260514123025_486CC5",
   "expected": [
    "clause_chunk"
   ],
   "observed": [
    "text_chunk"
   ]
  },
  {
   "doc_id": "DOC_IT_20260513120632_8E976C",
   "expected": [
    "clause_chunk"
   ],
   "observed": [
    "text_chunk"
   ]
  },
  {
   "doc_id": "DOC_ADMIN_20260513120215_EDF59A",
   "expected": [
    "clause_chunk"
   ],
   "observed": [
    "text_chunk"
   ]
  },
  {
   "doc_id": "DOC_ADMIN_20260513120217_08D91B",
   "expected": [
    "clause_chunk"
   ],
   "observed": [
    "text_chunk"
   ]
  },
  {
   "doc_id": "DOC_ADMIN_20260513120215_A4D6AE",
   "expected": [
    "clause_chunk"
   ],
   "observed": [
    "text_chunk"
   ]
  }
 ],
 "note": "metadata-level (xlsx/pptx + step-detect abstained — need canonical, phase-2)",
 "d3_under_chunk_candidates": 105,
 "d3_routed_total": 306
}
```

### L6/chunk-judge (Claude panel — representative is the gate metric, risk-enriched kept separate)
```json
{
 "representative": {
  "n": 139,
  "self_containedness": {
   "mean": 3.636,
   "ci": [
    3.472,
    3.789
   ],
   "n": 139
  },
  "coherence": {
   "mean": 3.89,
   "ci": [
    3.705,
    4.062
   ],
   "n": 139
  },
  "type_fidelity": {
   "mean": 3.532,
   "ci": [
    3.341,
    3.712
   ],
   "n": 139
  },
  "truncation": {
   "mean": 3.869,
   "ci": [
    3.655,
    4.068
   ],
   "n": 139
  },
  "overall": {
   "mean": 3.369,
   "ci": [
    3.199,
    3.535
   ],
   "n": 139
  },
  "pass_rate_overall_ge4": 0.432
 },
 "risk_enriched": {
  "n": 39,
  "self_containedness": {
   "mean": 3.094,
   "ci": [
    2.838,
    3.359
   ],
   "n": 39
  },
  "coherence": {
   "mean": 3.444,
   "ci": [
    3.154,
    3.735
   ],
   "n": 39
  },
  "type_fidelity": {
   "mean": 2.863,
   "ci": [
    2.624,
    3.111
   ],
   "n": 39
  },
  "truncation": {
   "mean": 3.385,
   "ci": [
    3.086,
    3.692
   ],
   "n": 39
  },
  "overall": {
   "mean": 2.761,
   "ci": [
    2.538,
    2.991
   ],
   "n": 39
  },
  "pass_rate_overall_ge4": 0.077
 },
 "by_chunk_type": {
  "clause_chunk": {
   "n": 32,
   "self_containedness": {
    "mean": 3.49,
    "ci": [
     3.156,
     3.823
    ],
    "n": 32
   },
   "coherence": {
    "mean": 3.573,
    "ci": [
     3.219,
     3.937
    ],
    "n": 32
   },
   "type_fidelity": {
    "mean": 3.229,
    "ci": [
     2.896,
     3.583
    ],
    "n": 32
   },
   "truncation": {
    "mean": 3.583,
    "ci": [
     3.146,
     4.01
    ],
    "n": 32
   },
   "overall": {
    "mean": 3.24,
    "ci": [
     2.885,
     3.615
    ],
    "n": 32
   },
   "pass_rate_overall_ge4": 0.375
  },
  "image": {
   "n": 17,
   "self_containedness": {
    "mean": 4.471,
    "ci": [
     4.137,
     4.667
    ],
    "n": 17
   },
   "coherence": {
    "mean": 4.745,
    "ci": [
     4.353,
     5.0
    ],
    "n": 17
   },
   "type_fidelity": {
    "mean": 4.824,
    "ci": [
     4.471,
     5.0
    ],
    "n": 17
   },
   "truncation": {
    "mean": 4.863,
    "ci": [
     4.588,
     5.0
    ],
    "n": 17
   },
   "overall": {
    "mean": 4.432,
    "ci": [
     4.079,
     4.667
    ],
    "n": 17
   },
   "pass_rate_overall_ge4": 0.941
  },
  "ocr_chunk": {
   "n": 8,
   "self_containedness": {
    "mean": 2.958,
    "ci": [
     2.375,
     3.542
    ],
    "n": 8
   },
   "coherence": {
    "mean": 3.167,
    "ci": [
     2.583,
     3.833
    ],
    "n": 8
   },
   "type_fidelity": {
    "mean": 3.584,
    "ci": [
     3.167,
     4.0
    ],
    "n": 8
   },
   "truncation": {
    "mean": 2.75,
    "ci": [
     2.25,
     3.25
    ],
    "n": 8
   },
   "overall": {
    "mean": 2.875,
    "ci": [
     2.417,
     3.375
    ],
    "n": 8
   },
   "pass_rate_overall_ge4": 0.125
  },
  "procedure_parent": {
   "n": 3,
   "self_containedness": {
    "mean": 3.889,
    "ci": [
     3.667,
     4.0
    ],
    "n": 3
   },
   "coherence": {
    "mean": 3.889,
    "ci": [
     3.667,
     4.0
    ],
    "n": 3
   },
   "type_fidelity": {
    "mean": 3.778,
    "ci": [
     3.667,
     4.0
    ],
    "n": 3
   },
   "truncation": {
    "mean": 2.0,
    "ci": [
     2.0,
     2.0
    ],
    "n": 3
   },
   "overall": {
    "mean": 3.0,
    "ci": [
     3.0,
     3.0
    ],
    "n": 3
   },
   "pass_rate_overall_ge4": 0.0
  },
  "step_card": {
   "n": 31,
   "self_containedness": {
    "mean": 3.452,
    "ci": [
     3.161,
     3.71
    ],
    "n": 31
   },
   "coherence": {
    "mean": 4.0,
    "ci": [
     3.613,
     4.387
    ],
    "n": 31
   },
   "type_fidelity": {
    "mean": 3.107,
    "ci": [
     2.71,
     3.505
    ],
    "n": 31
   },
   "truncation": {
    "mean": 3.93,
    "ci": [
     3.522,
     4.285
    ],
    "n": 31
   },
   "overall": {
    "mean": 3.075,
    "ci": [
     2.731,
     3.398
    ],
    "n": 31
   },
   "pass_rate_overall_ge4": 0.29
  },
  "table_chunk": {
   "n": 36,
   "self_containedness": {
    "mean": 3.852,
    "ci": [
     3.62,
     4.065
    ],
    "n": 36
   },
   "coherence": {
    "mean": 4.259,
    "ci": [
     4.028,
     4.482
    ],
    "n": 36
   },
   "type_fidelity": {
    "mean": 3.722,
    "ci": [
     3.407,
     4.046
    ],
    "n": 36
   },
   "truncation": {
    "mean": 4.519,
    "ci": [
     4.148,
     4.806
    ],
    "n": 36
   },
   "overall": {
    "mean": 3.463,
    "ci": [
     3.185,
     3.732
    ],
    "n": 36
   },
   "pass_rate_overall_ge4": 0.444
  },
  "text_chunk": {
   "n": 48,
   "self_containedness": {
    "mean": 3.056,
    "ci": [
     2.806,
     3.34
    ],
    "n": 48
   },
   "coherence": {
    "mean": 3.236,
    "ci": [
     2.993,
     3.5
    ],
    "n": 48
   },
   "type_fidelity": {
    "mean": 2.882,
    "ci": [
     2.681,
     3.125
    ],
    "n": 48
   },
   "truncation": {
    "mean": 3.111,
    "ci": [
     2.854,
     3.389
    ],
    "n": 48
   },
   "overall": {
    "mean": 2.833,
    "ci": [
     2.618,
     3.083
    ],
    "n": 48
   },
   "pass_rate_overall_ge4": 0.146
  },
  "visual_knowledge": {
   "n": 3,
   "self_containedness": {
    "mean": 3.556,
    "ci": [
     1.667,
     5.0
    ],
    "n": 3
   },
   "coherence": {
    "mean": 3.444,
    "ci": [
     2.0,
     4.333
    ],
    "n": 3
   },
   "type_fidelity": {
    "mean": 2.889,
    "ci": [
     1.0,
     4.667
    ],
    "n": 3
   },
   "truncation": {
    "mean": 3.555,
    "ci": [
     2.333,
     4.333
    ],
    "n": 3
   },
   "overall": {
    "mean": 3.0,
    "ci": [
     1.0,
     4.0
    ],
    "n": 3
   },
   "pass_rate_overall_ge4": 0.667
  }
 },
 "mean_overall_interjudge_stdev": 0.201,
 "rubric_version": "chunk_rubric_v1"
}
```
