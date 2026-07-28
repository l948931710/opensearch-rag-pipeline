# 基线重冻 20260727b：L4SRV 3.0.0 口径对齐（第三次）

日期：2026-07-27（跨日至 07-28 完成） · `code_commit=6c88ffa`
证据：`docs/evidence/baseline_refreeze_20260727b/`（gate.log + report.md）
备份：`eval_harness/goldset/baseline.json.bak-pre-refreeze-20260727b`

## 为什么重冻

codex 复核后把 L4-serving 判定口径升到 3.0.0（全部主指标按可见集判、旧口径拆独立、
MM judge 改喂实渲图、可见集取结构化 `included_doc_indices`、新增 `l4_serving_set_sha`）。
regime 两键变更 ⇒ `--strict` 必红。

## 修复在本轮**真的开火了一次**（关键证据）

`BIND-03`「U8系统怎么导出国外销售出货情况？」：

```
可见集 available_idxs = [2, 5, 6, 9, 12, 13]      （全量带图文档 9 篇）
模型发出 <<IMG:4>>  → 文档 4 有图，但被 max_context_chars 截掉、不在可见集里
生产 build_content_blocks(included_indices=…) 对它渲染 0 张
旧口径（全量 image_map）判它**合法**；3.0.0 判它 invalid
⇒ marker_validity 1 − 1/82 = 0.9878，与实测逐位吻合
```

**这个 0.9878（对比上一基线 1.0）是修复生效，不是回退。**

⚠️ 我此前基于影响实测说过"两个硬门读数一动没动" —— 那是在一个**恰好没触发**的 35 题
样本上测的（0/35 引用不可见图）。本轮触发 1 次。**该说法要修正为**：触发率低（约 1/35）
但非零，旧口径确实会高报。

## 逐指标核对（73 项，5 差 / 4 好 / 0 新增消失）

| 变差 | 旧 → 新 | 判定 |
|---|---|---|
| `judge.mm.image_relevance` | 3.886 → 3.838 | **口径变更所致**：judge 现在只看 post-rotation 实渲图，不再看全量候选。advisory |
| `l4srv.marker_validity` | 1.0 → 0.9878 | **修复生效**（见上），非回退 |
| `judge.correctness` | 4.462 → 4.444 | 面板波动 |
| `judge.completeness` | 4.218 → 4.204 | 面板波动 |
| `l6.judge_chunk.repr_pass_rate_ge4` | 0.446 → 0.432 | 面板波动 |

| 变好 | 旧 → 新 |
|---|---|
| `judge.negatives.overall` | 4.293 → **4.394** |
| `l4srv.orphan_rate` | 0.4182 → **0.3545** |
| `l4srv.marker_distinctness` | 0.8312 → **0.8765** |
| `l4srv.answer_image_rate` | 0.8214 → **0.8571** |

`dangling_ref_rate` 0.0286 **两轮完全不变**（硬门 ≤0.05，仍过）。

## 仍红的硬门（跨**三次**重冻稳定，非本轮引入）

`source attribution recall@1` = **0.947**（n=19，门槛 ≥0.95）。三次重冻读数完全相同。
已查清：门槛设在能力线之上（详见 `docs/src_attribution_goldset_expansion_2026-07-27.md`），
补题到 n=38 仍是 0.947。待 Sam 定：提升能力（样板段降权，需先 A/B）或重设门槛到 0.90。

## regime 现状（今天累计新增 4 键 + 1 键升版）

```
l4_ingestion_evaluator_version 2.0.0   l6_evaluator_version           2.0.0
l1_matcher_version             2.0.0   l4_serving_evaluator_version   3.0.0
l4_serving_set_sha             4dab01066ec5482b
```

## 未决

- 补题 19 条**未并入**（我建议先做逐题语义复核；SRC-30 金档确定不全，需补 A8 或换问法）。
  并入会移动 `eval_set_sha` ⇒ 第四次重冻。
- `source attribution` 门槛重标。
- `recall@1` 作主闸的问题：实测 reranker 收益是 `17→1` 这类大跳跃、代价是 12 次 `1→2`，
  而 `recall@1` 对两者同权；`mrr`/`ndcg@10` 对位移平滑。换主闸是更根本的修法，属下一批。
