# L6 rollout Batch 1 — scoped purge (704 old PKs) — COMPLETE

2026-06-15. Final step of Batch 1 (50 docs): irreversible explicit-PK delete of the 704 old rebuild
HA3 PKs, after the maintenance re-chunk (704 new chunks, frozen routing) was embedded + pushed.

## Rollback snapshot (retained, untouched)
`scratch/l6_ab/batch1_rollback_2026-06-15T2240.json` · 704 chunks · 9.00 MB ·
sha256 `78dcabfbb9ed96819fb7b2615c207afe0ef56364861ddf96cd99def931a48c6c` — full re-pushable payload
(metadata + chunk_text + dense(1024) + sparse). Verified intact post-purge.

## Pre-delete real-time gates (all PASS)
- G1: snapshot sha256 unchanged; 704 distinct delete_pks, all payload docs ∈ the 50.
- G2: fresh `chunk_meta` active == 704, all INDEXED; `delete_pks ∩ chunk_meta.id == ∅`.
- G3: real-time HA3 — new 704 dense+sparse 100%; HA3 old set == the 704 delete_pks exactly; 0 non-batch
  PK; 0 corpus drift (no other-doc NOT_INDEXED).

## Delete (irreversible)
Explicit HA3 integer-PK `push_documents cmd=delete`, 8 batches of ≤100. **No range delete, no corpus
reconcile, no other-doc writes.** Result: **requested=704, deleted=704, failed=0, anomaly=0** (all status 200).

## Post-purge verification (all PASS)
| gate | result |
|---|---|
| 50-doc RDS↔HA3 parity: missing=0, extra=0, Jaccard=1.0 | ✅ |
| each doc HA3 count == current chunk_meta count | ✅ |
| old/new twin == 0; 704 old PKs not retrievable (0 still present) | ✅ |
| D4 orphan step_cards == 0; D7 missing procedure_parent == 0 | ✅ |
| `[上文]` residual == 0; section_title overflow == 0 | ✅ |
| dense+sparse+BM25+rerank clean e2e | ✅ no twin, no batch-`[上文]`; drift docs surface (14DFDF 0.92, QUALITY 0.74, HR 0.63) |
| rollback snapshot intact (sha256 unchanged) | ✅ |

## Batch 1 result
50 docs fully migrated to the L6-fixed chunks: 704 new chunks serving, 704 old rebuild PKs purged,
RDS↔HA3 parity=1.0, structurally clean, e2e twin-free. The maintenance freeze preserved every doc's
chunk family (3 drift docs pinned to rebuild structure). **Remaining 217 docs NOT authorized.**
