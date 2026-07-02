# HA3 RAG — End-to-End Evaluation Report

- **Run**: eval_l4_lock  |  **Table**: `?`  |  **Generated**: 20260701_222146
- **Gold cases run**: 76  |  **LLM**: `qwen3.6-plus` (thinking OFF)  |  **Judge**: Claude panel (independent of generator)
- **Env**: test, simulate=false, endpoint=`ha-cn-kgl4slr1n01.public.ha.aliyuncs.com` (read-only)

## Verdict: 5 passed / 4 failed / 1 n-a

| Gate | Target | Value | Result |
|---|---|---|---|
| binding pdf Jaccard (L4-ing) | >= 0.78 hard (#F-mm8 升档) | 0.8556 | ✅ PASS |
| binding xlsx Jaccard (L4-ing) | >= 0.85 hard (D7 升档) | 0.6 | ❌ FAIL |
| binding docx Jaccard (L4-ing) | >= 0.95 hard (D6 设定) | 0.9898 | ✅ PASS |
| img_dup_factor p95 (L4-ing, 全格式) | <= 1.20 hard (>1.5 是已知 over-attach bug) | 1.0 | ✅ PASS |
| <<IMG:N>> marker validity (L4-srv) | >= 0.95 hard / >= 0.98 soft | 0.6637 | ❌ FAIL |
| dangling 口惠图但卡片无图 (L4-srv) | <= 0.05 hard (扩正则后) | 0.0263 | ✅ PASS |
| orphan rate (L4-srv, trend 监控) | <= 0.30 soft (trend, advisory) | 0.6118 | ❌ FAIL |
| marker distinctness (L4-srv, advisory) | advisory — 1.0 = no image reuse (lower = bundled/reused markers) | 0.88 | ❌ FAIL |
| answer image rate (L4-srv, advisory) | advisory — trend (锁分布后再定阈值; 主臂=生产 cosurface=False 姿态) | 0.75 | ➖ N/A |
| fusion/calibration regime (guard) | fusion == 'weighted' AND rerank == True (thresholds' calibration regime == production serving regime) | active fusion=weighted, rerank=True | ✅ PASS |

## L4-ingestion — 摄入侧图文绑定精度(逐格式 Jaccard)

```json
{
 "img_dup_factor_p95": 1.0,
 "img_dup_factor_max": 1.0,
 "n_degraded_docs": 2,
 "errors": [],
 "per_fmt": {
  "pdf": {
   "n_docs": 3,
   "n_degraded_docs": 0,
   "n_strong_chunks": 30,
   "mean_jaccard": 0.8555566666666666,
   "std_jaccard": 0.3208379434987479
  },
  "xlsx": {
   "n_docs": 4,
   "n_degraded_docs": 1,
   "n_strong_chunks": 20,
   "mean_jaccard": 0.6,
   "std_jaccard": 0.5026246899500346
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
 "binding_jaccard_pdf": 0.8555566666666666,
 "binding_jaccard_xlsx": 0.6,
 "binding_jaccard_pptx": null,
 "binding_jaccard_docx": 0.9898,
 "binding_jaccard_unknown": null
}
```

- evaluated docs: 50  |  judge_bundle_binding items: 35

## L4-serving — `<<IMG:N>>` 摆放质量(LLM 行为)

```json
{
 "n_answers": 38,
 "n_answers_with_images": 32,
 "answer_image_rate": 0.75,
 "interleave_rate": 0.75,
 "orphan_rate": 0.611764705882353,
 "marker_validity": 0.6637168141592921,
 "marker_distinctness": 0.88,
 "dangling_ref_rate": 0.02631578947368421,
 "over_cap_rate": 0.28125,
 "placement_rate": 0.75,
 "avg_images_shown": 2.875,
 "total_markers": 113,
 "total_invalid_markers": 38,
 "total_inrange_markers": 75,
 "total_distinct_markers": 66,
 "n_referenced_none_strategy": 8,
 "n_appended_strategy": 8
}
```
