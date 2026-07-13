# 三路客户端融合 A/B——既救盲行又保 sparse（实验判决）

> 2026-07-13，Sam 授权只读实测。承接 `docs/ha3_sparse_rootcause_and_ab_2026-07-13.md`
> 落地建议#3（「正确的三路做法」）。脚本：`scratch/client_fusion_3way_ab_20260713.py` +
> `scratch/sparse_reality_precheck_20260713.py`；报告 JSON 同目录。全程零写。

## 一句话结论

**可行，且归一化加权版（w3_s10 = dense 0.7 / sparse 0.1 / BM25 0.3，min-max 归一，缺席不罚分）
全面优于现默认去-sparse 路径**（recall@1 +3.6pp / MRR +2.6pp），同时盲行 5/5 救活、
sparse 受益题 9 题中 7 题回到 rank1。**RRF 被证伪**——缺席罚分正好打在无 sparse 行上。

## 前置验证：/query 上 sparse 真实存在且参与打分 ✅

5 健康行（最老 active，全量构建期）+ 2 盲行，自查询：

- E2(dense+sparse) vs E1(纯 dense)：**5/5 排序改变、5/5 目标行分数上升**，且量级悬殊
  （dense IP ≈1.0，加 sparse 后 51~69 —— **/query 分值里 sparse 占主导两个数量级**）。
- E3(零向量+纯 sparse)：健康行 **5/5 可召回**（名次同 E2）；盲行 **0/2**（无 sparse 项，正反证闭合）。

⇒ S 臂是真信号；/query 是 sparse 的**官方支持路径**，不再依赖 /search 未文档化行为。

## Part 1 盲行救活探针（5 真盲 doc 自查询）

D(纯dense)=rank1、S(+sparse)=None、B(BM25)=rank1 → **w3 融合全部 rank1**；rrf3 在 2~9。
S 臂在场不再引起失明——缺席只是拿不到加分。5/5 前 10 救活。

## Part 2 金集五臂 A/B（165 可评分正例，public，pool=50/臂）

| 指标 | server_on(旧) | server_off(现默认) | rrf3 | rrf2 | w2 | **w3_s10** | w3_s20 | w3_s30 |
|---|---|---|---|---|---|---|---|---|
| recall@1 | 0.6485 | 0.8061 | 0.6970 | 0.7758 | 0.8364 | **0.8424** | 0.8303 | 0.8303 |
| recall@3 | 0.7515 | 0.8909 | 0.8000 | 0.9152 | 0.9212 | **0.9212** | 0.9212 | 0.9212 |
| recall@5 | 0.7758 | 0.9152 | 0.8485 | 0.9515 | 0.9333 | **0.9333** | 0.9333 | 0.9273 |
| recall@10 | 0.8061 | 0.9758 | 0.9333 | 0.9636 | 0.9636 | **0.9636** | 0.9636 | 0.9636 |
| MRR | 0.7056 | 0.8585 | 0.7688 | 0.8488 | 0.8818 | **0.8848** | 0.8785 | 0.8769 |

- **w3_s10 vs server_off 逐题：改善 21 / 回退 10 / 不变 134**。回退 10 题中 8 题是 rank1→2/3
  琐碎；仅 2 题真降（J-r120_32 10→27、J-r120_97 9→30 = recall@10 唯一 -1.2pp 来源），
  且 **w2（不含 sparse）同样回退**（10→24、9→24）——归因客户端 BM25 min-max 归一化机制，
  与 sparse 无关，可通过归一化方法/权重调参再修。
- **sparse 受益 9 题**：QA-15 5→**1**、QA-34/49/91/110/SRC-06/SRC-19 2→**1**（7/9 全恢复）、
  QA-21 9→7（部分）、RAG-10 8→8（未恢复）。
- **意外发现**：客户端加权融合本身（w2，不含 sparse）就超过 HA3 服务端加权融合
  （recall@1 0.8364 vs 0.8061）——归一化后加权优于引擎内部的原始分加权。
  sparse 臂净边际 = +0.6pp recall@1 + QA-110/QA-21 的恢复；**weight 0.1 最优，0.2/0.3 开始反噬**。
- **RRF 判死**：rrf3 recall@1 0.697、回退 35 题。RRF 按名次计分、缺席=0 票，
  无 sparse 行（=盲行人群）天然少一臂票数——与「救盲行」目标结构性冲突。

## 延迟（串行实测）

D 229ms / S 201ms / B 211ms（p95 266~310ms）；三路并行墙钟≈最慢一臂，
优于现 server 路径单次 /search（875ms 均值，含嵌入+后处理，口径不同仅作量级参考）。

## 落地建议（user-gated）

1. retriever 新 flag（建议 `RAG_HA3_CLIENT_FUSION`，默认 off）：三路并行 + min-max
   加权 0.7/0.1/0.3，融合后进入既有后处理链（cover 降权/复核/邻居拼接不动）。
2. **高/中/低标签阈值 7.7/5.8 按服务端 weighted 分数标定，客户端融合分是 [0,1] 归一分，
   必须重标定**（或依赖 rerank-on 路径的 rerank 分数标签）。
3. sparse 覆盖随时间衰减（实时推送行无 sparse，仅全量构建物化）——融合对缺席鲁棒，
   但 S 臂收益会缩水；阿里工单（sparse 实时物化路径）仍值得追。
4. 换检索形态 → release-gate 须 refreeze。
5. J-r120_32/97 两题的 BM25 归一化回退值得先看一眼再灰度。

---

## Serving 实现（2026-07-13 同日落地，Sam 指示接线）

`opensearch_pipeline/retriever.py::_client_fusion_search` + `search_chunks` 接线，
flag `RAG_HA3_CLIENT_FUSION`（config `client_fusion_*` 五字段，默认 off；权重/池 env 可调）：

- 三臂 ThreadPoolExecutor 并行（D `/query` 纯 dense / S `/query` dense+sparse / B `/search`
  knn 权重 0 纯 BM25），min-max 归一加权 0.7/0.1/0.3，缺席不罚分；融合后走既有后处理链
  （cover 降权 / ACL 复核 / 邻居拼接不动）。S 臂与 `RAG_HA3_KNN_SPARSE_ENABLE`（只管 /search）
  互不相干——融合开启时 sparse 信号总是可用。
- **降级语义**：S/B 辅臂失败按空臂继续；D 主臂异常返回 None → 回落服务端混合（fail-open）。
- **对外 score = knn_weight×dense_IP + text_weight×BM25_raw**（服务端可比分）；融合名次分
  存 `_fused_score` 仅供诊断。
- 单测 `tests/test_client_fusion.py`（12 例：门控/三臂形态/缺席不罚分/score 语义/三种降级/
  阈值自动标定）；全套 3480 绿 + ruff clean。

### 真栈只读 smoke（走改后 search_chunks 本体）

- 盲行自查询 3/3 top-3 召回（1/1/3）；金集关键题 11/12 与实验一致（±2 容差；QA-15 漂到 5
  =封面降权后处理所致，off 臂同值，非融合回归）。
- 端到端延迟无罚分：fusion on mean 1313ms ≈ off 1317ms（嵌入占大头，三臂并行）。

### ⚠️ 档位阈值重标定（同时是 8fc80f8 单独部署的拦路雷）

smoke 实测：**去-sparse 服务端路径与融合路径的 top-7 在 7.7/5.8 旧阈值下 100% 标「低」**
——旧阈值的标定母体是含-sparse 分数（sparse 是分数主体）。40q×top7 分位数匹配标定
（`scratch/score_threshold_calibration_20260713.json`，旧路径 P(高)=12.1%/P(高∪中)=46.8%）：

| 路径 | 分数域 | HIGH | MEDIUM |
|---|---|---|---|
| 客户端融合 | [0.02, 0.64] | **0.57** | **0.52** |
| 去-sparse 服务端（8fc80f8 默认） | [0.54, 0.71] | 0.65 | 0.60 |

处置（config.py load_config 守卫）：融合开启且未显式设 env → **自动套 0.57/0.52**（显式
`RAG_SCORE_THRESHOLD_HIGH/MEDIUM` 永远优先）；去-sparse 路径在 production/staging 且未设
阈值 → loud-warn（沿 rrf 守卫先例，正式值随 release-gate refreeze 复标）。

### 生产开启（Sam 执行）

**默认开（2026-07-13 随包生效，Sam 拍板「env 太多一个个输太麻烦」）**：`client_fusion_enable`
代码默认 true（dataclass + load_config 工厂双默认，同 8fc80f8 先例），档位阈值由守卫自动套
0.57/0.52——**SAE 部署本包即生效，一个 env 都不用加**。部署后 release-gate refreeze。

Kill switch：`RAG_HA3_CLIENT_FUSION=false` 回落去-sparse 服务端混合（此时档位阈值恢复
7.7/5.8 旧值——production/staging 下守卫会 loud-warn 提醒该尺度已失真，参考 0.65/0.60）。

评测注意：默认开意味着 eval harness / release-gate 的 live 检索臂也默认走融合——refreeze
基线即融合形态；对照旧形态须显式 `RAG_HA3_CLIENT_FUSION=false`。锚定服务端混合请求构造的
单测（test_rrf_hybrid_search / test_degraded_bm25）已显式关闭融合以隔离被测路径。
