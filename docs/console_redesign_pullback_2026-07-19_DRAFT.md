# Console 管理页重设计回拉台账(Claude Design → console-app)_2026-07-19_DRAFT

Sam 在 claude.ai/design「Design System」项目(`7f515669-76f6-42a5-9f9a-563a7f5917d3`)上
基于 17 张现状底稿卡完成重设计;本文是全量拉回比对的结论,作为落回 console-app 的实现规格来源。

- 底稿(基线)与远端镜像(终稿)在会话 scratchpad:`design_bundle/` vs `design_bundle_remote/`
  (scratchpad 会话结束即失效;终稿的耐久来源=远端设计项目本身,可随时 DesignSync 重拉)。
- 比对方法:逐文件拉取;大文件 diff、小文件多点 probe;ops/members/tabs-nav 三个"仅 tab 栏变化"
  的镜像由基线+补丁合成(diff 行数与预期逐一吻合)。

## 判定总表(基线 17 卡)

| 结论 | 文件 |
|---|---|
| **删除** (1) | screens/history.html — 采纳方案 B,独立审批历史页取消 |
| **大改** (5) | screens/approvals.html、screens/docs.html、screens/dashboard.html、components/doc-table.html、components/queues.html |
| **仅 tab 栏改** (3) | screens/ops.html、screens/members.html、components/tabs-nav.html |
| **未变** (8) | screens/employee.html、components/{upload-card,member-role,stat-card,modals,charts}.html、foundations/{colors,status,typography}.html |

注:foundations 3 张未直接重拉,判定依据=所有已拉文件的 token 基线 CSS 块与底稿逐字节一致
(用户没有动 token);employee/upload-card/member-role/stat-card/modals/charts 为多点 probe 全命中。

## 设计变更规格(落码依据)

### 1. IA:方案 B 落定
- 五 tab:概览看板 / 文档管理(**去角标**) / **审批**(角标=待办总数,示例 8) / 运营指标 / 成员管理。
- 「审批」tab 三分区:待处理(待审批队列+授权申请)→ 生效中·已授权(AccessGrantList)→ 历史(四流合并只读时间线,五筛选 chip 保留)。
- 文档管理 tab 瘦身为:待办摘要条 → 上传入库 → 文档台账(队列与授权治理区全部迁出)。
- 语义变化:**授权申请队列改橙头(qhead-busy)**——"待处理=琥珀"统一;差评复核卡改**红头(qhead-fail)**。

### 2. 列表体验统一升级(本次最大主题)
- **一切列表可分页可排序**:队列 2 条/页、历史时间线 3 条/页、台账 50 条/页(1–50 · 共 328/132 条 + 页码 1 2 3 … N)。
- 每个队列卡头新增**时间排序切换 chip**(新→旧 ⇄ 旧→新);终稿卡里带了可工作的 JS 翻页/排序实现(q-viewport/q-track/q-page + qfoot 翻页脚)可作交互参考。
- 仪表盘差评复核卡额外加**时间范围下拉**(近 7/30/90 天/全部)+ 排序 + 分页。

### 3. DocTable 重构(组件卡与整页同款)
- 顶部:「我的文档 N」+ **本部门/全部门 scope 切换** + 搜索;筛选行 8 状态 chips(带计数)+ 归属/可见范围/**利用度**三下拉。
- **行勾选 checkbox + 表头全选**(批量操作预备);全列排序按钮,更新列带方向箭头。
- 行副行新增**利用度信号**:「引用 N 次」/「从未被引用」(st-warn 色)+ 共享信息(「共享 品质技术、生产计划部」)。
- **操作列 200px→110px**:6 图标收敛为 3 控件——版本历史 + **下载原始文件(新动作)** + 「更多操作」菜单(menu-pop:可见范围 / 跨部门共享·权限 / 上传新版本 / 退役下线-红,退役行换恢复上线)。
- 失败/未入索引态 title 提示:「上传新版本(升版)可重灌;失败原因见版本历史」。

### 4. 其他
- 审批队列行的「预览」按钮图标改为下载语义(title=下载原始文件),行副行补等宽时间戳。
- 队列/历史样本量升到 4/4/4/9 条以演示分页——落码时行数来自数据,非规格。

## 落回 console-app 的映射(初步)

| 设计面 | 代码面 |
|---|---|
| 审批 tab + 三分区 | ManageView.vue tab 定义/深链校验/badge 来源迁移;新 ApprovalsTab 容器(或 ManageView 内分支) |
| 队列分页+排序 | ApprovalQueue / AccessRequestQueue / AccessGrantList / FeedbackReviewList / ApprovalHistory 加 pager+sort(前端分页起步) |
| DocTable 重构 | DocTable.vue:勾选、scope 切换、利用度筛选、操作列收敛+菜单、下载动作(需后端原件下载端点核对)、翻页替代「加载更多」 |
| tab 角标迁移 | reviewCount 挂到审批 tab;文档管理去角标 |

实现须走 console-app 的 `/ui-iterate` 硬门(独立审计→受限实现→Playwright→独立评审),
build 后落 `opensearch_pipeline/webconsole/next-dist/`,SAE 重打包才现网生效(B7 族)。
