# L6 rollout Batch 1 — Stage 2 (frozen-routing maintenance re-chunk) — ACCEPTED, nothing pushed

2026-06-15. First batch of the remaining 267 affected docs: **50 docs** (dept-stratified, all 10
depts). Re-chunked under **maintenance mode** so only the L6 text fix (Fix A drop `[上文]` + Fix B
section_title) applies — the LLM classifier is frozen, preserving chunk family. No Stage 3 / push / purge.

## Why maintenance mode (the bug it fixes)
The first batch Stage-2 runs exposed that re-running Stage 2 **re-runs the LLM classifier**, whose
`category_l1/l2` drives chunk-strategy routing — flipping a doc's chunk family run-to-run
(PRODUCTION_14DFDF: `sop`→step→79 vs `standard`→clause→47; counts jumped 704→676 across runs). Category
is the *only* non-deterministic routing input, so freezing it makes routing deterministic.

## Three bugs found + fixed + committed during this batch
| commit | fix |
|---|---|
| `844483e` | reset must set `index_status='NOT_INDEXED'` (stage-3 lock predicate) + `rechunk_reset_state()` + test |
| `edbe1ef` | `node_write_chunk_meta` full-replace by `(doc_id,version_no)` — kills same-version re-chunk **strand** (shrink left old high-index chunks active); ownership guard + 6 tests |
| `e0146db` | **maintenance re-chunk**: `RAG_MAINTENANCE_ROUTING` → `node_classify` reuses frozen category (0 LLM calls), fail-closed on missing entry; normal ingestion unchanged; 5 tests |

Full suite **1048 green**, edits ruff-neutral.

## Frozen routing manifest
Built from **HA3** (the rebuild's actual chunks — RDS `chunk_meta` was overwritten by the earlier
re-chunks; HA3 was never pushed/purged for this batch). `scratch/l6_ab/batch1_routing_manifest.json`,
**sha256 `dd5721cdd77d892a850a95683d9631d13eb2ca4a6ab76832988ffaadb7ad31b3`**, 50 docs, modes text 26 /
clause 16 / step 8. Offline dry-run (OSS read + in-memory chunk, no writes) predicted **704** = rebuild,
exact per-doc count+type match.

## Execution
reset (50, `index_status=NOT_INDEXED`) → Stage 2 with `RAG_MAINTENANCE_ROUTING` (e0146db).
Pre-flight: manifest == exactly 50 docs; sha256 recorded; claimable set == exactly the 50;
orchestrator logged "MAINTENANCE re-chunk: frozen routing for 50 docs (LLM classifier disabled)";
**LLM classifier calls = 0** (instrumented counter). 7/7 nodes SUCCESS, 0 FAILED, 704 valid chunks.

## Hard acceptance — ALL PASS
| gate | result |
|---|---|
| total chunks == 704 | ✅ |
| per-doc count + type mix == frozen manifest | ✅ 50/50 |
| all 704 NOT_INDEXED | ✅ |
| stranded active/INDEXED == 0 | ✅ |
| `[上文]` residual == 0 | ✅ |
| section_title overflow == 0 (max 60) | ✅ |
| orphan step_cards == 0 | ✅ |
| missing procedure_parent == 0 | ✅ |
| FAILED docs == 0 | ✅ |
| no active non-current version (50 docs) | ✅ |
| non-batch docs untouched (0 other NOT_INDEXED) | ✅ |

3 drift docs pinned to rebuild structure: PRODUCTION_14DFDF→step 79 (73 text+4 step+1 proc+1 visual),
QUALITY_7FA6C7→step 8, HR_8BD7C3→clause 4.

## State
50 docs at **704 NOT_INDEXED, nothing pushed**. HA3 still holds the rebuild's 704 originals (untouched).
Awaiting separate authorization for Stage 3 (embed + push) → verify → scoped purge.
