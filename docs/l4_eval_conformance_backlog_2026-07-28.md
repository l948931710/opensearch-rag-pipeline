# L4-serving 评测口径与生产渲染不一致——待办清单（codex 复核 2026-07-27 提出）

状态：**已修 1 条生产缺陷，评测侧 6 条记账待办**（Sam 拍板走 B：只修生产旁路）
来源：codex-review thread `019fa750-8f3b-7650-988b-7742502638c5`（VERDICT: REVISE，2 blocker）
本人独立核验：**5 条 major 全部属实**，其中 2 条用可执行反例复现

## 已修（本次）

**生产旁路：`generate_answer_via_stream` 丢掉 `included_doc_indices`**
该包装器逐帧收集 SSE 但只留 `sources`，`api.py:1407` 的 `result.get(...)` 拿到 `None`
⇒ 渲染器退回**全量** image map，把被 `max_context_chars` 截掉、模型根本没看见的图渲染给
员工。受影响：`thinking=True` 的 `/api/ask`。非流式与 SSE 直连都带了该字段，只有这条漏了
——所以既有测试全绿也没拦住。**这是用户可见缺陷，不是评测问题。**

## 待办（未做，需一次干净的重跑 + 第三次重冻）

| # | 项 | 依据 |
|---|---|---|
| 1 | `analyze_answer` 拆 `available/referenced` 的 **visible 与 all 两套** | codex BLOCKER-1：现在 `n_orphan_all` 与 `orphans` 共用同一个 `referenced`，一旦按可见集改，旧口径也被污染 |
| 2 | `aggregate` 用独立 `with_imgs_all` 分母 | 反例：全部图被截断的题，逐题 `orphan_rate_all=1.0`，聚合返回 `None` ⇒ **我文档里"旧口径可逐轮对照"是错的** |
| 3 | `marker_validity`/`n_shown`/`dangling` 也按可见集判 | 反例：ctx 只含图1、答案引图2 ⇒ evaluator `marker_validity=1.0`/渲染 1 张，生产 `included_indices=[1]` 实渲染 **0 张** |
| 4 | MM judge 的 `shown_image_captions` 喂 post-rotation 的**实际展示图** | codex BLOCKER-2：现在喂的是全量 `image_map_summary`，而 rubric 写的是"卡片实际展示的图" ⇒ `judge.mm.image_relevance` 也测错 |
| 5 | 评测改用 `_format_context_ex` 的**结构化** `included_doc_indices` | 现在是正则扫 context 串；结构化来源更权威，正则只作回放兼容 |
| 6 | `l4_serving_set_sha` 进 regime | `_regime` 只哈希主 goldset，`golden_l4_serving.json` 运行时并入却无指纹 |

## 已冻基线里两个数**不可信**

- `l4srv.marker_validity = 1.0` —— 可能高报（引用未进 context 的图仍判合法）
- `l4srv.dangling_ref_rate = 0.0286` —— **硬门 ≤0.05**；按可见集重判后 `n_shown` 可能从正数
  变 0，dangling 可能翻转。方向不定，但必须重算。

连带受影响：`n_invalid_markers`、`n_inrange_markers`、`marker_distinctness`、
`strategy/interleave_rate`、`placement_rate`、`rendered_any/answer_image_rate`、
`over_cap_rate`、`avg_images_shown`。

## 两条降级项（机制成立但现网零实例）

| 项 | 机制 | 现网实测 |
|---|---|---|
| 标题双向包含无长度护栏 | `title_similarity("通知（进出厂规定）","通知.pdf")=1.0` | 173 金集名 × 1481 活跃标题 = **256213 对，0 实例** |
| 语料正文自带 `<<IMG:` 污染协议 | 会误开规则 10、污染规则 13 清单、扩大可见集 | 含 `<<IMG:` 的活跃 chunk = **0**（含 `IMG:` 也是 0） |

codex 同意**不立即改匹配行为**（已知长度比 0.5 护栏会误伤《员工手册》↔《员工手册202108月》
等真匹配），但提醒：**不要写 passing test 断言假匹配 `==1.0`，那会把缺陷固化成契约**；
应写 xfail/known-risk 反例 + 真匹配保护 + 活语料碰撞审计（钉"保留字为零"的 invariant）。

## 做的时候要连带产出（codex 要求的证据）

- 修复后"引用了未入 context 图片"的样本数，及其中命中图示措辞的数量
- 新旧所有受影响 L4 指标的**逐题 diff**
- `orphan_rate_all` 在"全部图不可见"与"隐藏图被引用"两类反例下仍精确保留旧值的测试
- conformance 测试需固定 `img_subindex`、签名失败、近重复过滤三种姿态
- 两次全量扫描的脚本/语料快照标识/goldset SHA，供以后复核"零实例"的时点
