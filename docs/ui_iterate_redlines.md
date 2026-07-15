# /ui-iterate 红线与 WONTFIX 登记表（console-app）

> 每轮 /ui-iterate 的 auditor 必读。KEEP 后如出现新的用户拍板（WONTFIX / 证伪的审计结论 / 口径约束），追加进本表并注明日期。
> 来源：2026-07 console UX 迭代各批次（α/β/γ/δ）+ kb_admin 全面评审（docs/console_kb_admin_ux_review_2026-07-14.md）。

## 红线（勿再建议 / 勿实现）

| 条目 | 拍板 | 日期 |
|---|---|---|
| 375px / 移动端专项适配 | WONTFIX——console 桌面优先，钉钉小程序另有前端 | 2026-07-04 |
| 假阶段状态条（上传/入库进度用假百分比） | 禁止——诚实降级优先，宁可只显状态词 | 2026-07-14 |
| 审批卡内嵌文档 diff | 不做——属 Agent 底座 PR-4 范围，勿在 console 侧抢做 | 2026-07-14 |
| 批量审批（一键全批/全驳） | 不做——审批必须逐条过人 | 2026-07-14 |
| 聊天内消歧（问答流里插澄清交互） | 不做 | 2026-07-14 |
| 治理/审批类端点自动轮询加密 | 治理端点吃 ask 限流桶——绝不自动轮询（手动刷新可）；ManageView 60s 轻轮询是唯一豁免且不碰 loadDocs | 2026-07-14 |
| 无 flag 语义的端点抄 agent_gov 的 supported 三态 | 禁止——members 档纯角色门即可（γ 审计裁定） | 2026-07-14 |
| 贡献修订改归属加二次确认弹窗 | 不加——「采纳到「X」」按钮文案的零点击线索已拍板足够，审核动线不插确认摩擦（ε-1 遗留项用户裁决） | 2026-07-14 |
| hits=null 显式「—」占位 | 不做——拍板维持**自隐**（算不出就不显示，cost_estimate 同款诚实纪律；ε-2 R2 遗留项用户裁决） | 2026-07-14 |
| 审核漏斗（采纳率/审核时长）暴露面 | 拍板 **API 层只给管理员**——员工的 gaps 响应恒 None；字段按请求身份补注、绝不进共享缓存（缓存按 depts 键跨角色共享，进缓存必泄漏） | 2026-07-15 |
| 「待回答」列表形态 | 拍板 **高频无人回答排行 Top 30**——缺口窗 365 天（与 asks 的 30 天近期热度口径**解耦**，勿共享常量）、只显前 30 不翻页、截断尾注如实披露；真·无窗需 qa_session_log hash 列/物化表=远期立项 | 2026-07-15 |

## 口径约束（跨批次 durable）

- governance「问答 API 成功率」= 实时 30 天滑窗且 SQL 排除 model_name='agent'；qa_daily_metrics error_rate = 按北京日物化且**含** agent——同名指标两套算法必然对不上，任何看板复用这两源必须就地标注口径。三个 p95 各不同体（qa_session_log 端到端 / llm_call_log 调用粒度 / qa_daily_metrics 物化）。
- llm_call_log.cost_estimate 有意 NULL（023 拍板「不编单价」）——任何成本展示必须诚实 NULL，禁止自编单价折算。
- DeptTable 使用量列固定 30 天口径（排序/无答案率同体），概览柱图 7/30 可切——两处并存靠列头 hint 标注，不强行联动。

## 工程惯例（实现约束）

- composable 模块级单例 + 30s staleness 门（`freshEnough`）：筛选/参数变更必须 force=true，否则静默跳过。
- 新 store 必须 `registerIdentityScopedStore`（P0-D 跨身份清空）。
- dev-preview mock 分支判 `import.meta.env.DEV && token==='dev-preview'`。
- 404/403 → supported=false / 静默兜底（fail-closed 探测，端点未部署不报错）。
- confirm 弹窗放组件层，绝不进 composable（useKb.spec 直调用例会挂）。
- fixture/mock 日期一律相对 Date.now()。
- 贡献页共享授权函数（_kb_owner_scope_sql/_kb_can_manage/managed_owner_depts）被 20+ 处复用——绝不修改共享入口，贡献归属走专属函数（δ-3）。

## 已证伪的审计结论（勿重复报告）

- 「/console/manage 渲染成侧栏空态」= 浏览器面板层间歇拦截 vite 懒加载模块 fetch（curl 200、Playwright 自有浏览器全绿）——环境问题，非应用 bug（2026-07-14 锤死）。
- dev-HMR 长寿命 tab router 失活（URL 对但渲染 QA 空态）——开新 tab 即愈，生产构建无 HMR 零关联。
