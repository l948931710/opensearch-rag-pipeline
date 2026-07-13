# HA3 522 文档失明 — 根因终局 + 去 sparse 金集 A/B（决策文档）

> 2026-07-13。承接 `docs/audits/l7_refreeze_release_gate_2026-07-12.md`（现象发现）、
> `docs/ha3_sparse_missing_evidence_2026-07-12.md`（工单证据）、memory `ha3-blind-485-incident-2026-07-12`。
> **状态：根因锁定 + 修复验证完成；剩 flip flag 部署 + release-gate refreeze（user-gated）。**

## 一句话结论

现网检索走的混合接口 `/vector-service/search` **不支持 sparse 参数**（阿里工程师 + 官方 inverted-query 文档确认，knn 仅 vector/topk/filter/weight）。我方 retriever 长期向该接口的 knn 臂传 `sparse_data`，引擎对这个不支持的参数做**未文档化处理，把"无 sparse 前排值的行"静默排除**。6-27 上一次全量构建之后、经 API/Swift 实时推送进来的文档没有 sparse（sparse 仅全量物化），于是被成片排除——**522 篇文档（占语料 35%）对现网检索失明**。去掉 sparse 参数即修复，且**金集 A/B 证明去 sparse 净提升召回 +14~17pp**。

## 症状与规模（实测）

- 全语料 1479 docs 逐个自查询：**盲 522（35%）/ 可召回 957**（脚本 `full_corpus_blind_scan_20260712.py`，清单 `scratch/true_blind_docids_20260712.txt`）。盲 by perm：dept_internal 393 / public 129。
- 盲人群 = 6-27 全量之后 API/Swift 实时推送的行（含 7-06/07 大重灌批）；"485" 是早期按时间窗切出的子集，已作废。

## 根因证据链（全可复现，请求体见 `ha3_sparse_evidence_requestbodies_2026-07-12.json`）

对同一行（盲行 pk=74583 vs 健康行 pk=22743，同 admin 部门 / public，仅写入时间不同）：

| 请求 | 盲行 74583 | 健康行 22743 |
|---|---|---|
| A `/query` 仅 dense | rank 1 | rank 1 |
| B `/query` dense **+ sparse** | **None（消失）** | rank 1 |
| C `/search` knn(dense+sparse) 隔离臂（text=乱码+RRF） | **None** | rank 2 |
| D `/search` 纯 BM25 文本 | rank 1 | rank 1 |

判读：dense/HNSW、BM25、正排都正常；**唯 sparse 一加就把盲行排除**。C 组证明「RRF 能救回盲行」是靠 BM25 文本臂兜底、非 sparse（隔离 knn+sparse 臂后盲行 None、健康行 rank2）。→ sparse 倒排缺这批实时推送行；根因是「往不支持 sparse 的 /search 传 sparse」+「实时推送不物化 sparse」。

## 去 sparse 金集 A/B（决策依据）

`RAG_HA3_KNN_SPARSE_ENABLE` flag（retriever.py，默认 true 保现状；false 去 sparse）。251-q 金集 165 可评分正例（全 public，user_dept=None，top_k=10，live HA3；脚本 `sparse_goldset_ab_20260712.py`）：

| 指标 | 现状(含 sparse) | 去 sparse | Δ |
|---|---|---|---|
| recall@1 | 0.6485 | **0.8061** | +15.8pp |
| recall@5 | 0.7758 | **0.9152** | +13.9pp |
| recall@10 | 0.8061 | **0.9758** | +17.0pp |
| MRR | 0.7056 | **0.8585** | +15.3pp |

逐题：**改善 43 / 回退 16 / 不变 106**。回退多为 rank1→2 琐碎（QA-34/49/91/110、SRC-06/19），少数真降（RAG-10 2→8、QA-15 1→5、QA-21 1→9 = 这些 gold 本有 sparse、sparse 确帮过它们），被净收益完全压倒。此为 public-only 测量，dept_internal 393 盲行的复原未计入（直接测试已证会一起复原），**真实收益更大**。

结论：**sparse 在 /search 上净有害**（旧「去 sparse tanks recall」结论源于离线 embedding 质量测量，不适用本端点）；去 sparse = 止血即根治（就现网检索质量而言）。

## 历史勘误

- 3-way hybrid（dense+sparse+BM25 走 /search）2026-05-23(6d9eac4) 引入，基于「/search 支持 sparse」的未验证假设。
- sparse 在 /search 上**确实参与排序**（健康行含/去 sparse top-10 排序 0/8 相同，非摆设）——但依赖未文档化行为，且对无 sparse 行是排除而非降权。
- 全量 rebuild 后每行都有 sparse 时该 bug 不显形；实时无 sparse 行堆积才暴露。

## 落地建议（user-gated）

1. **部署 `RAG_HA3_KNN_SPARSE_ENABLE=false`**（SAE env）——唯一需授权动作；纯客户端、可逆、无生产写、不依赖阿里、不用全量 rebuild。
2. **release-gate refreeze**——现 baseline 冻于盲行堆积前，去 sparse 后召回态变化，须重冻新基线（否则门失真）。
3. **工单继续问「正确的三路（dense+sparse+BM25）做法」**——为将来找回 sparse 帮过的少数查询保留选项（走支持路径 / 全量 rebuild），非现网急需。
4. 未决脆弱性：即便健康行的 sparse 也依赖未支持参数，引擎升级可能变化——长期要么正式弃 sparse、要么走支持路径。

## 附：本轮只读脚本（会话 scratchpad / scratch，可重跑）

full_corpus_blind_scan / paired_probe_485 / isolate_arms / contrast_sparse / rrf_source / rrf_contrast / sparse_effect_healthy / export_evidence / sparse_goldset_ab（+ report json）。
