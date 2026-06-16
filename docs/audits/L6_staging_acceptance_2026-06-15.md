# L6 chunker-fix — Full staging rebuild + live acceptance (2026-06-15)

Full 562-doc / 6690-chunk staging rebuild with the L6-fixed chunker (Fix A drop `[上文]` +
Fix B section_title de-staling, `ffb0150` + length-cap fix), then live L0–L6 + D1–D7 vs the
prod baseline. Staging targets: RDS `fuling_knowledge_stg` / HA3 `fuling_kb_chunks_s` / OSS
`fuling-knowledge-base-staging`. **Zero production writes.**

## Build (all staging-only)
seed 566 docs + 6690 chunks (after-corpus = chunker's real Fix A/B, re-chunk-equivalent) →
real orchestrator stage 3 embed+push → reconcile 139 stale dry-run chunks. Final: HA3 `_s`
docCount 6690 == RDS active 6690.

## Acceptance results

| layer | result |
|---|---|
| **D1–D7 structural** | **PASS** — `[上文]` residual **0**, orphan step_cards **0**, missing procedure_parent **0**, section_title >255 **0** |
| **L0 dense** (G2/G4) | **PASS** — dense self-query 60/60 @ score 1.0; vector fidelity cos 1.0 |
| **L0 sparse** (G3) | **FAIL — staging-infra artifact** (see below), 16/40 |
| **L1 retrieval** | **PASS** — see before/after below |
| **L2 calibration** | **FAIL — staging-infra artifact** (sparse-dependent score scale) |
| **L5 permission** | **N/A** — corpus all-public (same as prod) |
| **L6 chunk-quality** | **PASS** — RDS↔HA3 `_s` id-set parity: missing=0, extra=0, jaccard=1.0 |

## L1 before/after — the decisive retrieval check
Staging (after, **dense+BM25**) vs prod (before, **full hybrid w/ sparse**), same 162 gold queries + qwen3-rerank:

| metric | prod before | staging after | Δ |
|---|---|---|---|
| recall@1 | 0.8519 | 0.8519 | **+0.000** |
| recall@3 | 0.9321 | 0.9506 | +0.019 |
| recall@5 | 0.9568 | 0.9630 | +0.006 |
| recall@10 | 0.9753 | 0.9753 | +0.000 |
| MRR | 0.8974 | 0.9048 | +0.007 |
| nDCG@10 | 0.8891 | 0.8950 | +0.006 |
| content_hit | 0.862 | 0.862 | +0.000 |

Staging **matches-or-beats prod on every metric despite missing the sparse leg** (a handicap). Pre-set regression thresholds (recall@1 drop >2pp / rank1→>5 / MRR drop >0.02) — **none triggered**.

## The two staging "fails" are infrastructure, NOT the fix
The staging `_s` table (the hasty workaround created when the `_stg` table failed on the Alibaba console) **cannot fully build the sparse index via realtime push** — dense builds perfectly (G2 60/60), sparse only partially populates (G3 16/40) even after fresh native dense+sparse embeds (verified `embed_texts_native` returns sparse; `_s` sparse config identical to prod; cache cleared & re-embedded). Because staging is dense+BM25-only:
- **L0-G3** (sparse self-query) fails — no sparse index.
- **L2** calibration fails — score thresholds (7.7/5.8) are tuned to prod's full-hybrid score scale.

Both are independent of the chunker fix and **not a prod risk**: prod's real index has full working sparse (G3 passes on prod). The fix's full-hybrid retrieval safety is covered by the **offline dense+sparse A/B** (real native embeddings, before/after): recall@1 0.988→0.992, 0 regressions.

## Bugs the staging rebuild caught (value earned)
1. **section_title >255 overflow** — Fix B's own-heading could exceed varchar(255) → prod `chunk_meta` insert would fail. Fixed (≤60-char heading gate) + regression test, committed.
2. **embed-cache sparse gap** — `scratch/embedding_cache.json` held dense-only entries; cache-hit chunks skipped re-embed → no sparse. Cleared; surfaced the deeper `_s` realtime-build limitation.

## VERDICT — chunker fix = GO for production
Quality significantly improved (self_cont +0.83/+0.89, type_fid +0.59; `[上文]` eliminated; 1519 stale section_titles fixed) AND retrieval shows no regression — recall *up* in both the offline dense+sparse A/B and the live staging dense+BM25 run vs prod. Structure intact; unaffected types byte-equal.

**Outstanding:** staging full-hybrid (L0-G3/L2) is blocked by the `_s` table's sparse realtime-build — an infrastructure matter (resolve by getting a proper `_stg` table built, or accept that the offline dense+sparse A/B faithfully covers full hybrid). It does not gate the fix.

Next: production re-chunk plan for the 280 affected docs (for approval) — via the laptop push-then-purge / DataWorks path, with the standard 6-dim verify gate.
