<!-- 由 day7_chunker_postfix_compare.py 自动生成 -->
<!-- verdict=ALL_EQUAL | exit_code=0 -->

# Day 7 chunker post-fix verify — compare

*generated: 2026-06-13 04:55* — runs=3


## ✅ Day 7 verdict: ALL_EQUAL — chunker 已确定

- per_fmt mean across 3 runs std = 0.0000
- per_chunk byte-equal: 55/55
- img_dup_factor_p95 全部 ≤ 1.2: True

**下一步**:在最后一轮 outdir 跑 1 次 panel,看 image_binding mean ≥ 4.0?

## 表 1 — per_fmt mean_jaccard 跨 N 轮
| fmt | run1 | run2 | run3 | mean | std↓ | spread↓ |
|---|---|---|---|---|---|---|
| docx | 0.9898 | 0.9898 | 0.9898 | 0.9898 | 0.0000 | 0.0000 |
| pdf | 0.7722 | 0.7722 | 0.7722 | 0.7722 | 0.0000 | 0.0000 |
| pptx | n/a | n/a | n/a | n/a | n/a | 0.0000 |
| xlsx | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.0000 | 0.0000 |

- ALL_EQUAL 条件:std_across_runs ≤ 0.0(byte-equal)
- STD_OK 上限:std_across_runs ≤ 0.02

## 表 2 — top-10 飘动 per_chunk
(✅ 全部 per_chunk byte-equal,无飘动)

- per_chunk 总数: 55;byte-equal: 55;飘动: 0
- chunk_key 取 ingestion_binding.per_chunk.gt_label(P0 amendment)

## 表 3 — img_dup_factor
| metric | run1 | run2 | run3 | hard |
|---|---|---|---|---|
| img_dup_factor_p95 | 1.130 | 1.130 | 1.130 | ≤1.2 |
| img_dup_factor_max | 1.200 | 1.200 | 1.200 | — |

_排除 degraded doc(与 ingestion_binding.py L313-316 一致)_

## 表 4 — vs D6 baseline
| fmt | D6 mean | D7 mean | Δ |
|---|---|---|---|
| docx | n/a | 0.9898 | n/a |
| pdf | 0.7273 | 0.7722 | 0.0450 |
| pptx | n/a | n/a | n/a |
| xlsx | 0.7727 | 1.0000 | 0.2273 |


---

## 后续手写部分(panel mean / 升 hard 决策 / D8 计划)

> 这部分由人接着补:在最后一轮 outdir 上跑 panel 后,把 image_binding mean、
> top-3 评委分歧、是否升 hard 写下来,锁档完成。
