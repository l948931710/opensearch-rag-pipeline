# chunker A/B report — mode=binding_only

- arms: off, on
- git_commit: 63a2d9d8eb77
- timestamp: 2026-06-14T14:02:51
- seed: 20260614

## Validity notes

- Tier 0 BINDING_ONLY ran 2 dimensions: (1) funnel image_index Jaccard (regression reference, from eval_image_binding_pdf.py); (2) semantic anchor key_jaccard / dual_jaccard (primary, v3 #15, from anchor GT). Dual = key_hit AND image_hit.

## Metrics (per arm)

| metric | off | on | Δ(ON-OFF) |
|---|---|---|---|
| img_dup_max | 1.0000 | 1.0000 | - |
| img_dup_p95 | 1.0000 | 1.0000 | - |
| mean_jaccard_pdf | 0.8389 | 0.9333 | +0.0944 |
| n_anchors_evaluated | 22 | 22 | - |
| n_docs | 3 | 3 | - |
| n_strong_chunks | 30 | 30 | - |
| semantic_anchor_dual_hits | 19 | 20 | - |
| semantic_anchor_dual_jaccard | 0.8636 | 0.9091 | +0.0455 |
| semantic_anchor_key_hits | 22 | 22 | - |
| semantic_anchor_key_jaccard | 1.0000 | 1.0000 | +0.0000 |
| std_jaccard_pdf | 0.3260 | 0.2537 | - |

## Win/Tie/Loss (per metric)

- **jaccard_pdf**: win=5 tie=25 loss=0
- **semantic_anchor_dual**: win=2 tie=19 loss=1

## Per-case (52 rows)

_dumped to per_case.json_