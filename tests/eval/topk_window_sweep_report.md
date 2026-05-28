# top_k × stitch_window 参数对比评测报告

**生成时间**: 2026-05-28 13:57
**评测 Query 数**: 120
**检索方式**: HA3 混合检索 (Dense + Sparse + BM25 Weighted)
**max_context_chars**: 6000

---

## 1. 总体对比

| Config | R@1 | R@5 | Context Coverage | Avg Context Len | Avg Effective Chunks | 溢出情况 |
|---|---|---|---|---|---|---|
| top_k=5, no stitch (当前 API) | 57.5% | 81.7% | 88.6% | 2618 | 5.0 | ✅ 无溢出 |
| top_k=5, window=±1 | 57.5% | 81.7% | 91.1% | 4309 | 4.1 | ✅ 无溢出 |
| top_k=5, expand+stitch=±1 (旧版) | 57.5% | 81.7% | 91.1% | 4332 | 4.1 | ✅ 无溢出 |
| top_k=7, no stitch | 57.5% | 81.7% | 91.5% | 3586 | 6.9 | ✅ 无溢出 |
| top_k=7, window=±1 (推荐) | 57.5% | 81.7% | 92.8% | 5195 | 5.1 | ✅ 无溢出 |
| top_k=7, expand+stitch=±1 | 57.5% | 81.7% | 92.8% | 5204 | 5.2 | ✅ 无溢出 |
| top_k=10, no stitch | 57.5% | 81.7% | 93.8% | 4689 | 9.4 | ✅ 无溢出 |
| top_k=10, window=±1 | 57.5% | 81.7% | 92.8% | 5789 | 6.0 | ✅ 无溢出 |
| top_k=10, window=±2 | 57.5% | 81.7% | 92.2% | 5937 | 4.5 | ✅ 无溢出 |
| top_k=20→10, window=±2 (当前 DingTalk) | 57.5% | 81.7% | 92.2% | 5937 | 4.5 | ✅ 无溢出 |

---

## 2. 按难度分类


### single_chunk

| Config | R@1 | CC | N |
|---|---|---|---|
| top_k=5, no stitch (当前 API) | 66.7% | 89.3% | 30 |
| top_k=5, window=±1 | 66.7% | 92.8% | 30 |
| top_k=5, expand+stitch=±1 (旧版) | 66.7% | 92.8% | 30 |
| top_k=7, no stitch | 66.7% | 95.0% | 30 |
| top_k=7, window=±1 (推荐) | 66.7% | 97.8% | 30 |
| top_k=7, expand+stitch=±1 | 66.7% | 97.8% | 30 |
| top_k=10, no stitch | 66.7% | 95.0% | 30 |
| top_k=10, window=±1 | 66.7% | 97.8% | 30 |
| top_k=10, window=±2 | 66.7% | 96.7% | 30 |
| top_k=20→10, window=±2 (当前 DingTalk) | 66.7% | 96.7% | 30 |

### cross_chunk

| Config | R@1 | CC | N |
|---|---|---|---|
| top_k=5, no stitch (当前 API) | 75.0% | 87.1% | 40 |
| top_k=5, window=±1 | 75.0% | 90.0% | 40 |
| top_k=5, expand+stitch=±1 (旧版) | 75.0% | 90.0% | 40 |
| top_k=7, no stitch | 75.0% | 87.9% | 40 |
| top_k=7, window=±1 (推荐) | 75.0% | 90.0% | 40 |
| top_k=7, expand+stitch=±1 | 75.0% | 90.0% | 40 |
| top_k=10, no stitch | 75.0% | 90.0% | 40 |
| top_k=10, window=±1 | 75.0% | 90.0% | 40 |
| top_k=10, window=±2 | 75.0% | 91.7% | 40 |
| top_k=20→10, window=±2 (当前 DingTalk) | 75.0% | 91.7% | 40 |

### multi_doc

| Config | R@1 | CC | N |
|---|---|---|---|
| top_k=5, no stitch (当前 API) | 8.3% | 63.9% | 12 |
| top_k=5, window=±1 | 8.3% | 70.8% | 12 |
| top_k=5, expand+stitch=±1 (旧版) | 8.3% | 70.8% | 12 |
| top_k=7, no stitch | 8.3% | 72.2% | 12 |
| top_k=7, window=±1 (推荐) | 8.3% | 70.8% | 12 |
| top_k=7, expand+stitch=±1 | 8.3% | 70.8% | 12 |
| top_k=10, no stitch | 8.3% | 87.5% | 12 |
| top_k=10, window=±1 | 8.3% | 70.8% | 12 |
| top_k=10, window=±2 | 8.3% | 70.8% | 12 |
| top_k=20→10, window=±2 (当前 DingTalk) | 8.3% | 70.8% | 12 |

### reasoning

| Config | R@1 | CC | N |
|---|---|---|---|
| top_k=5, no stitch (当前 API) | 60.0% | 90.0% | 10 |
| top_k=5, window=±1 | 60.0% | 90.0% | 10 |
| top_k=5, expand+stitch=±1 (旧版) | 60.0% | 90.0% | 10 |
| top_k=7, no stitch | 60.0% | 95.0% | 10 |
| top_k=7, window=±1 (推荐) | 60.0% | 95.0% | 10 |
| top_k=7, expand+stitch=±1 | 60.0% | 95.0% | 10 |
| top_k=10, no stitch | 60.0% | 95.0% | 10 |
| top_k=10, window=±1 | 60.0% | 95.0% | 10 |
| top_k=10, window=±2 | 60.0% | 95.0% | 10 |
| top_k=20→10, window=±2 (当前 DingTalk) | 60.0% | 95.0% | 10 |

### disambiguation

| Config | R@1 | CC | N |
|---|---|---|---|
| top_k=5, no stitch (当前 API) | 80.0% | 100.0% | 10 |
| top_k=5, window=±1 | 80.0% | 100.0% | 10 |
| top_k=5, expand+stitch=±1 (旧版) | 80.0% | 100.0% | 10 |
| top_k=7, no stitch | 80.0% | 100.0% | 10 |
| top_k=7, window=±1 (推荐) | 80.0% | 100.0% | 10 |
| top_k=7, expand+stitch=±1 | 80.0% | 100.0% | 10 |
| top_k=10, no stitch | 80.0% | 100.0% | 10 |
| top_k=10, window=±1 | 80.0% | 100.0% | 10 |
| top_k=10, window=±2 | 80.0% | 100.0% | 10 |
| top_k=20→10, window=±2 (当前 DingTalk) | 80.0% | 100.0% | 10 |

### query_robustness

| Config | R@1 | CC | N |
|---|---|---|---|
| top_k=5, no stitch (当前 API) | 50.0% | 100.0% | 8 |
| top_k=5, window=±1 | 50.0% | 100.0% | 8 |
| top_k=5, expand+stitch=±1 (旧版) | 50.0% | 100.0% | 8 |
| top_k=7, no stitch | 50.0% | 100.0% | 8 |
| top_k=7, window=±1 (推荐) | 50.0% | 100.0% | 8 |
| top_k=7, expand+stitch=±1 | 50.0% | 100.0% | 8 |
| top_k=10, no stitch | 50.0% | 100.0% | 8 |
| top_k=10, window=±1 | 50.0% | 100.0% | 8 |
| top_k=10, window=±2 | 50.0% | 87.5% | 8 |
| top_k=20→10, window=±2 (当前 DingTalk) | 50.0% | 87.5% | 8 |

### unanswerable

| Config | R@1 | CC | N |
|---|---|---|---|
| top_k=5, no stitch (当前 API) | 0.0% | 100.0% | 6 |
| top_k=5, window=±1 | 0.0% | 100.0% | 6 |
| top_k=5, expand+stitch=±1 (旧版) | 0.0% | 100.0% | 6 |
| top_k=7, no stitch | 0.0% | 100.0% | 6 |
| top_k=7, window=±1 (推荐) | 0.0% | 100.0% | 6 |
| top_k=7, expand+stitch=±1 | 0.0% | 100.0% | 6 |
| top_k=10, no stitch | 0.0% | 100.0% | 6 |
| top_k=10, window=±1 | 0.0% | 100.0% | 6 |
| top_k=10, window=±2 | 0.0% | 100.0% | 6 |
| top_k=20→10, window=±2 (当前 DingTalk) | 0.0% | 100.0% | 6 |

### permission

| Config | R@1 | CC | N |
|---|---|---|---|
| top_k=5, no stitch (当前 API) | 0.0% | 100.0% | 4 |
| top_k=5, window=±1 | 0.0% | 100.0% | 4 |
| top_k=5, expand+stitch=±1 (旧版) | 0.0% | 100.0% | 4 |
| top_k=7, no stitch | 0.0% | 100.0% | 4 |
| top_k=7, window=±1 (推荐) | 0.0% | 100.0% | 4 |
| top_k=7, expand+stitch=±1 | 0.0% | 100.0% | 4 |
| top_k=10, no stitch | 0.0% | 100.0% | 4 |
| top_k=10, window=±1 | 0.0% | 100.0% | 4 |
| top_k=10, window=±2 | 0.0% | 100.0% | 4 |
| top_k=20→10, window=±2 (当前 DingTalk) | 0.0% | 100.0% | 4 |

---

## 3. 按 Query 类别对比（推荐 vs 当前）

| 类别 | N | 当前 API CC | 旧版 CC | 推荐 CC | 当前 DingTalk CC |
|---|---|---|---|---|---|
| 上下文断裂 | 20 | 87.5% | 90.0% | 90.0% | 90.0% |
| 单点事实/明确条款 | 15 | 86.7% | 92.2% | 95.6% | 93.3% |
| 口语化/同义词/错别字 | 8 | 100.0% | 100.0% | 100.0% | 87.5% |
| 多文档综合 | 12 | 63.9% | 70.8% | 70.8% | 70.8% |
| 对比判断 | 10 | 90.0% | 90.0% | 95.0% | 95.0% |
| 无答案/拒答 | 6 | 100.0% | 100.0% | 100.0% | 100.0% |
| 权限过滤 | 4 | 100.0% | 100.0% | 100.0% | 100.0% |
| 流程步骤/SOP | 20 | 86.7% | 90.0% | 90.0% | 93.3% |
| 相似文档干扰/消歧 | 10 | 100.0% | 100.0% | 100.0% | 100.0% |
| 表格/数字/条件 | 15 | 92.0% | 93.3% | 100.0% | 100.0% |

---

## 4. 参数选择建议

基于以上数据，最终统一参数应选择 **R@1 和 Context Coverage 最优且无 context 溢出** 的组合。

> [!NOTE]
> R@1 衡量检索精度（第一条结果是否命中目标文档），Context Coverage 衡量上下文完整性（关键信息是否在 context 中），
> 两者需要平衡。R@1 高但 CC 低 = 找到了但信息不完整；R@1 低但 CC 高 = 信息分散在多个文档中。
