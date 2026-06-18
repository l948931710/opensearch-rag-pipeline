# Gold-set authoring guide — Step-5 de-skew (golden_50 → golden+)

Purpose: fill the coverage + type gaps the 2026-06-18 audit found, so the eval represents production
and the L2 off-topic-discrimination metric can actually run. **Author into `additions.template.json`,
validate with `scripts/validate_gold_additions.py`, then merge into `golden_50.json` (or a new
`golden_full+.json`).** Re-run `make release-gate` after merging (the gold sha changes → the regime
fingerprint changes → refreeze any baseline).

## What's missing today (the gaps to fill)

| category | have | target | why it matters |
|---|---|---|---|
| **off_topic negatives** | **0** | **≥ 5** | the ONLY negative class where a high retrieval score is a TRUE leak — without these the L2 off-topic-AUC metric stays N/A and can't gate |
| **metadata negatives** | **0** | **≥ 3** | genuinely-absent metadata (author/owner/版本日期 not in content). NB: some metadata IS in docs (e.g. 文控编号) — those are positives, not negatives |
| **image cases (positive)** | ~2 | **≥ 5** | L4-srv hard gates (marker validity / dangling) are `not_executed` below N=5 → currently unmeasured |
| **xlsx cases (positive)** | few | **+several** | xlsx recall is high-variance on a tiny n |
| **dept coverage (positive)** | HR/admin/finance only | **+IT, production, quality, marketing, rd** (≥3 each) | gold has ZERO questions for these depts; L1 recall only represents 3. NB: corpus has **no "sales" dept** — 外贸/客户 lives under `marketing`; `pmc`/`supply` (采购) are also thin, extend later |
| **finance** | present | **+3 (authorable NOW)** | verified 2026-06-18: **24 finance docs are ALL `permission_level=public` + registered today** (tax SOPs `FL-CW-SW-*`, 制度 `CWZD-*`, 内控手册V2). Author finance positives as public now (`--verify-live` confirms each is retrievable). Nothing finance is restricted yet — ACL (model B) only changes their permission LATER |

## Integrity rules (non-negotiable — these are the project's gold-quality principles)
1. **Verify before authoring.** A *positive* must be answerable from the corpus: run the query
   read-only (`retrieve_and_enrich`, rerank ON) and confirm the answer is in a retrieved chunk. An
   *off_topic negative* must be genuinely ABSENT (no relevant doc exists at all).
2. **`keyword_gt` must be VERBATIM substrings** of the answer-bearing chunk text — not paraphrase.
   (The L1 content-hit + L3 keyword-coverage match literally.)
3. **Fix the pipeline / correct verifiably-wrong GT — never loosen matching to fake a number.**
4. **`neg_type` decides expected behaviour**, NOT a blanket "negatives must score low":
   - `off_topic` → topic genuinely absent; a high reranker score here IS a real leak (the AUC gate measures this).
   - `near_miss_answer_absent` / `metadata` / `modality_gap` / `live_data` → a topically-relevant chunk EXISTS but the specific answer isn't in it; a **high score is EXPECTED** (informational, not a leak — the generator decides answerability, verified 0 fabrication 2026-06-18).
5. **All-public today** (`expected_permission: ["public"]`) — INCLUDING the 24 finance docs (verified
   2026-06-18: tax SOPs + `CWZD-*` 制度 + 内控手册, all public + registered). So author finance
   positives NOW. ACL (model B) only changes their permission LATER → when it lands, re-tag those
   entries to finance-internal + add permission-filtering negatives. Nothing is restricted yet, so
   nothing is actually deferred — the only ACL-gated work is the *future* permission re-tag.

## Schema — POSITIVE (mirror an existing positive, e.g. `J-water_soak`)
```jsonc
{
  "qid": "IT-erp-login-01",                 // unique, dept-prefixed
  "source": "docx",                          // docx | pdf | xlsx | json_text
  "module": "rag_retrieval",                 // rag_retrieval | rag_retrieval_json
  "dept": "it",                              // hr|admin|finance|it|production|quality|marketing|rd|pmc|supply (no "sales")
  "query": "U8 系统登录失败提示密码错误怎么处理？",
  "kind": "positive",
  "expected_docs": ["富岭U8+财务部操作手册"],   // exact title(s)
  "expected_doc_ids": ["DOC_IT_20260513120634_C6FD16"],  // resolve read-only; validator can help
  "resolution": [{"expected":"富岭U8+财务部操作手册","title":"富岭U8+财务部操作手册.docx",
                  "doc_id":"DOC_IT_20260513120634_C6FD16","sim":1.0,
                  "permission_level":"public","owner_dept":"it"}],
  "answer_points": "重置密码 / 联系系统管理员",   // the answer the doc supports (free text)
  "pass_criteria": "Top5召回目标文档 + 答案含目标信息",
  "keyword_gt": ["重置密码"],                  // VERBATIM substring(s) of the chunk
  "difficulty": null,
  "expect_images": false,                     // true → fill expected_images
  "expected_images": [],                      // VLM-caption gists of the bound image(s)
  "expected_permission": ["public"],
  "live_scorable": true
}
```
**Image case**: set `"expect_images": true` + `"expected_images": ["蓝色箱内透明杯浸水", ...]` (caption
gists). Aim for ≥5 across docx/pdf/xlsx.

## Schema — NEGATIVE
```jsonc
{
  "qid": "OFF-stock-price-01",
  "source": "json_text", "module": "rag_retrieval", "dept": null,
  "query": "富岭科技的股票代码是多少？",        // genuinely NOT in the internal doc corpus
  "kind": "negative",
  "neg_type": "off_topic",                    // off_topic | metadata | near_miss_answer_absent | modality_gap | live_data
  "expected_docs": [], "expected_doc_ids": [],
  "answer_points": "", "pass_criteria": "应拒答 / 无结果（语料中无此主题）",
  "keyword_gt": [], "difficulty": null,
  "expect_images": false, "expected_images": [],
  "expected_permission": ["public"], "live_scorable": true,
  "note": "off_topic: no internal doc covers stock/IR — a high reranker score here is a true leak"
}
```
**`neg_type` quick guide**
- `off_topic` —股票代码 / CEO 生日 / 竞品报价 / 天气 … topic absent from the corpus. **≥5 of these.**
- `metadata` — “X 文档的最后修改人/批准人是谁” where the doc body doesn't state it. (Doc *number* like 文控编号 IS often present → that's a positive.) **≥3.**
- `near_miss_answer_absent` — the right doc exists but lacks the specific value (品牌型号/限值/费用).
- `modality_gap` — “把…的图/示意图发我” where only text exists (or the boundary is ambiguous).
- `live_data` — 本月预算/实时库存/今日考勤 … real-time data not in static docs.

## Workflow
1. Copy `additions.template.json`, fill entries (positives + the missing negative types).
2. `RAG_ENV=prod_ro PYTHONPATH=. python -m eval_harness.scripts.validate_gold_additions \
       --additions eval_harness/goldset/additions.template.json --verify-live`
   (`--verify-live` resolves doc_ids + checks each positive's `keyword_gt` appears in a retrieved
   chunk, read-only; omit it for an offline schema/coverage check.)
3. Fix anything the validator flags; merge the entries into the gold file.
4. `make release-gate` → the L2 off-topic-AUC gate now measures real discrimination; refreeze baseline.
