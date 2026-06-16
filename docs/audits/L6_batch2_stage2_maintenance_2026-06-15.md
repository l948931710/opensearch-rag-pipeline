# L6 rollout Batch 2 — Stage 2 (frozen-routing maintenance re-chunk) — ACCEPTED, nothing pushed

2026-06-15. 100 docs (dept × file_ext stratified). Maintenance re-chunk with frozen routing — only the
L6 text fix applies, LLM classifier frozen. No Stage 3 / embed / push / purge.

## Provenance (clean working tree)
- HEAD `93c875c` (after committing the unrelated docx-Subtitle fix separately — working tree clean).
- chunker.py blob `0865f871` (==worktree), pipeline_nodes.py `1b5164c3` (==worktree),
  reindex_states.py `17b591c9`, dataworks_orchestrator.py `5b50aadd`.
- frozen manifest `scratch/l6_ab/batch2_routing_manifest.json`,
  **SHA256 `b1f70e7d9a4b79a6415156a727d6f5b49f5613ca0aad6e22327aa355e170a78c`** (re-confirmed at run).

## Execution
reset (100, `index_status=NOT_INDEXED`) → Stage 2 with `RAG_MAINTENANCE_ROUTING` (manifest sha verified,
claimable set == exactly the 100, orchestrator logged "frozen routing for 100 docs (LLM classifier
disabled)", **classifier calls = 0** instrumented). 7/7 nodes SUCCESS, 0 FAILED, 920 valid chunks.

## Hard acceptance — ALL PASS
| gate | result |
|---|---|
| total chunks == 920 (manifest-derived) | ✅ |
| per-doc count + type mix == frozen manifest | ✅ 100/100 |
| all 920 NOT_INDEXED | ✅ |
| stranded active/INDEXED == 0 | ✅ |
| `[上文]` residual == 0 | ✅ |
| section_title overflow == 0 (max 60) | ✅ |
| orphan step_cards == 0 | ✅ |
| missing procedure_parent == 0 | ✅ |
| FAILED docs == 0 | ✅ |
| no active non-current version (100 docs) | ✅ |
| non-batch docs untouched | ✅ |

## Phase 0 (read-only) recap
Offline dry-run predicted 920 == rebuild, exact per-doc match, 0 anomaly. Fix A removed `[上文]` from
326 chunks / 44 docs; Fix B changed section_title on 382 chunks / 97 docs.

## State
100 docs at **920 NOT_INDEXED, nothing pushed**. HA3 still holds the 920 rebuild originals (untouched).
Awaiting separate authorization for Stage 3 (embed + push) → verify → scoped purge.
Progress after this Stage 2: 60 done + 100 re-chunked (awaiting index) of 277.
