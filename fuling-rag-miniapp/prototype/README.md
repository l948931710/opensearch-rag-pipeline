# 富岭知识助手 · 钉钉小程序 UX 原型

一个**自包含、可点击**的 HTML 原型（"Claude design" mockup），用于在开发真正的 `.axml` 小程序之前，让开发者 / 干系人**看到并点击**预期的交互体验。

它复刻了浙江富岭塑胶（Fuling Plastics）企业知识库 RAG 问答机器人在**钉钉小程序**中的回答渲染方式 —— 与后端 `content_blocks_builder.py` 输出的**图文穿插内容块**一一对应。

## 这是什么

- 一部 **390px 宽的手机框**，居中显示在中性背景上。
- **Aurora-Forest 设计系统**（v1 已锁定）：深林绿品牌色 + 极光渐变、棉纸纹理底（feTurbulence）、暖墨文字；浅色/深色双主题（右上角切换，真机走 `prefers-color-scheme`）。设计令牌全部在 `:root` / `html[data-theme=dark]`，port 时即 ACSS 的 `page{}` / `@media (prefers-color-scheme: dark)`。
- AI 回答由一个 **`blocks[]` 数组**渲染（`{type:'text'}` / `{type:'image'}`），文本块渲染为段落（`**…**` 为唯一加粗标记）、图片块渲染为内联图 + 图注行（类型图标 + 说明 + 点按放大提示）。
- 截图用**内联 SVG** 模拟（U8+ 登录界面、系统管理面板、请假流程图），完全离线、无任何网络依赖。

## 如何打开 / 预览

**方式一（最简单）**：直接双击 `index.html`，用任意浏览器打开即可。无需联网、无需构建、无需服务器。

**方式二（本地静态服务器，可选）**：
```bash
cd fuling-rag-miniapp/prototype
python3 -m http.server 4599
# 浏览器访问 http://localhost:4599/index.html
```

> 提示：为了更接近真机比例，可把浏览器窗口拉宽一些（≥ 480px），避免顶部标题被挤压换行。

## 可体验的交互

| 功能 | 操作 |
| --- | --- |
| 示例问答 | 点击底部「示例问题」快捷栏（如 *U8+ 如何登录？*）→ 直接发送 |
| 加载骨架 | 发送后约 1.2s 显示 typing 骨架气泡，随后流式吐字 |
| 打字机 + 停止 | AI 文本逐字（约 28ms/字）揭示；打字中发送键变「**停止**」，点按整段直出（不是丢弃） |
| 钉住才跟滚 | 打字中向上翻阅不会被抢滚动条；右下角出现「回到底部」浮标 |
| 图文穿插 | 多步 SOP 答案里文字与截图交替排布；图注行右侧有「点按放大」提示 |
| 图片放大 | 点击内联图 → 全屏 lightbox（关闭键在**左上**，避让钉钉胶囊），箭头 / ←→ / 滑动切换，点暗处即关 |
| 参考来源 | 可折叠「参考来源 · N 条」，每条带 **相关度高/中/低** 徽章（服务端 `level` 下发，原型按 7.7/5.8 标定兜底） |
| 赞 | 👍 → 保持饱和高亮 + 「感谢反馈 👍」toast + 持久「已反馈」标记；复制 / 转人工仍可用 |
| 踩 | 👎 → 原因 chips（内容不准确 / 答非所问 / **答案不完整** / **没找到我要的文档** / 图片不对 / 信息过时）+ 文本框 → 提交 |
| 复制回答 | 复制图标 → 全文（纯文本）入剪贴板（真机 `dd.setClipboard`） |
| 转人工 | 耳机图标 → toast + 卡片底部**持久确认行**「已转交管理员跟进，请留意钉钉消息」 |
| 低匹配 guard | 来源全为中/低时回答前置琥珀色提示条（*模具保养周期？*） |
| 未找到（NO_RESULT） | 空结果卡：放大镜 + 标准文案 + 换说法 chips + 「转人工协助」出口（演示·未找到） |
| 出错重试 | 错误卡（danger 左标尺 + 重试 pill），导航栏副标题降级为「服务暂不可用」（演示·出错重试） |
| 图片过期 | OSS 签名 URL 过期占位「图片已过期 · 点按重新加载」，点按重载（演示·图片过期） |

预置 4 段真实问答（*U8+ 登录* 含 2 截图 / *请假流程* 含流程图 / *访客 WiFi* 纯文本 / *模具保养* 低匹配 guard）+ 3 个状态演示 chip（未找到 / 出错重试 / 图片过期，虚线样式）。

## 给开发者：1:1 映射

原型把所有逻辑放在 vanilla JS 里，开发者可直接映射到真实小程序组件 / AXML：

| 原型元素 (index.html) | 真实小程序组件 / AXML / 后端 |
| --- | --- |
| `.phone` / `.screen` | 钉钉小程序运行容器（无需自己画，仅原型用） |
| `.navbar`「富岭知识助手」 | 首选 `chat.json` `"transparentTitle":"always"` + 自绘渐变导航（`padding-top` 用 `statusBarHeight`）；不可用时退化为纯色 `#15604a` 原生标题栏 |
| `.chat` 消息列表 | 页面 `<scroll-view scroll-y scroll-into-view>`（聊天页 `pages/chat/index.axml`） |
| `.msg.user .bubble` | 用户气泡 `<view class="bubble user">`，右对齐 |
| `.ai-card`（答案卡片） | **`answer-bubble` 自定义组件**（核心复刻目标） |
| `renderAnswer()` / `typeBlocks(blocks)` | 组件内 `<block wx:for="{{blocks}}">` 循环渲染（对应钉钉模板的 Loop 组件） |
| `blocks[] {type:'text', text}` | `{type:'markdown', content}`（后端 `content_blocks_builder.py`）→ `<text>` / `<rich-text>` 节点 |
| `blocks[] {type:'image', svg, caption, alt}` | `{type:'image', url, caption}`（`url` = OSS 签名 URL，源自 `image_refs.oss_key` → `oss_url.generate_signed_url`）→ `<image mode="widthFix">` + `<text class="caption">` |
| 内联 SVG 截图 | 真机为 `<image src="{{block.url}}">`（OSS 签名 URL，1h 过期） |
| `typeText()` 逐字 | 流式回答：SSE / 钉钉流式卡片，**stream 变量 = `content`**（见 streaming-feature 记录） |
| 加载骨架 `.skel` | answer-bubble 的 `loading` 态 / 钉钉流式卡片占位 |
| 图片 lightbox | **自绘 page 根级 `position:fixed` 覆盖层**（保留图注 / 计数 / 森林色 scrim —— `dd.previewImage` 会全部丢掉）；如需捏合缩放，在层内加「查看原图」按钮调 `dd.previewImage` |
| `.sources`「参考来源」 | 折叠组件 `<view>`，数据来自检索 `chunks[].{doc_title, section}` + 服务端 `level: high\|mid\|low`（**不要在前端用 score 重算** —— rerank 开启后是 0-1 量纲） |
| `.fb` 反馈条（SVG 图标按钮） | answer-bubble footer；上报 `POST /api/feedback`（`feedback_handler.py`，`feedback_type` = upvote/downvote/handoff）；赞踩互斥锁定，复制 / 转人工**不**随锁 |
| 👎 原因 chips | `feedback_reason`（内容不准确 / 答非所问 / 答案不完整 / 没找到我要的文档 / 图片不对 / 信息过时，`VARCHAR(128)` 无需改表） |
| 「其他原因」文本框 | `feedback_comment`（真机里 = DM-reply 补充原因流程） |
| `toast()` | `dd.showToast({ content })` |
| `.inputbar` + 发送 | 底部输入组件 → `POST /api/ask`（带 `session_id`，返回 `message_id` 供反馈关联） |
| `.quick` 示例问题 | 首屏快捷提问 chips（引导首次使用） |

### 与后端契约对齐的关键点

- 真实 `content_blocks` 元素为 `{type:'markdown', content}` 与 `{type:'image', title, url, caption}`；原型用 `{type:'text', text}` / `{type:'image', ...}` 表意等价，开发时按上表换名即可。
- 图片来源是 `image_refs` 字典（`oss_key` / `source_image` / `visual_summary` / `ocr_text` / `image_index`），这是 extractor → chunker → content_blocks_builder → 钉钉卡片的 **load-bearing contract**，渲染层只消费 `url`(=签名 oss_key) 与 `caption`(=visual_summary/caption)。
- LLM 用 `<<IMG:N>>` 标记图片插入位置；后端据此把 markdown 拆成图文穿插块。原型已按"图文交替"的最终形态呈现。
- 反馈与回答通过 `message_id` 关联；流式 `/api/ask/stream` 当前不记录日志、无 `message_id`（已知限制），生产建议走 `/api/ask`。

## Port 到 ACSS 前必须先定的三件事（gating）

1. **图标管线**：AXML 无内联 `<svg>`，而反馈条 SVG 图标是锁定设计。方案：把所有 `ICON.*` 描边图形打包为一个 woff2 **iconfont**，base64 内嵌 `app.acss` 的 `@font-face`，用 `<text class="fb-ico-glyph">` 渲染 —— 颜色继承现有 token，所有状态×主题免图片资产。点赞激活态的 16% 内填充用 `background:var(--brand-soft)` 表达。
2. **令牌迁移**：浅色块整体迁入 `app.acss` 的 `page{}`，深色块迁入 `@media (prefers-color-scheme: dark){ page{} }`（替代原型的 `html[data-theme]` 手动切换）。同时改 `app.json`：`titleBarColor:#15604a`、tabBar `selectedColor:#15604a`（现仍是旧蓝 `#1677ff` —— 不改则绿卡片嵌在蓝 chrome 里）。
3. **`/api/ask` 契约补充**（一次后端改动打包做）：来源对象加 `level: "high"|"mid"|"low"`（`llm_generator` 已有 高/中/低 逻辑，rerank 开启后分数为 0-1 量纲，前端不可重算）；响应加 `guard: true`（低匹配提示条）与 `no_result: true`（空结果卡）标志。

## ACSS port 体检清单（原型里能跑 ≠ 小程序里能跑）

- `color-mix()` 已全部替换为预计算 `--brand-a16/a30/a40` rgba token —— port 时直接照搬，**不要**复原 color-mix。
- 所有 `:hover` 仅为桌面预览（已标注）；port 时删除，交互反馈用 `:active` 或 AXML `hover-class`。
- `:focus-visible` / `user-select` / `cursor` / `::-webkit-scrollbar` 全部删除（无键鼠；未知伪类可能绊倒 ACSS 编译器）。
- flex `gap` → 子元素 margin（老核默默忽略 gap）；快捷栏改 `<scroll-view scroll-x>`。
- 输入框用 `<textarea auto-height confirm-type="send" onConfirm>`（替代原型的 JS 自增高；`lineCount>4` 后定高内滚）。
- 棉纸 feTurbulence 纹理需在**真机 Android 钉钉**冒烟验证；不行就预栅格化两张 ~440×440 灰度 PNG（base64 进 `--paper`），分层声明 `var(--paper), radial-gradient(...), var(--page)` 保持不变（纯色兜底优雅退化）。
- lightbox 提升为 page 根级 fixed 视图（scroll-view 内 absolute 出不来）；关闭键**保持左上**（右上是钉钉胶囊）。
- 打字机揭示文本必须**整串重设**（原型 `typeText` 的 acc 方案），AXML 侧建议 build 时把 `**…**` 解析为 runs（`[{t,b}]`）再按全局字符偏移揭示 —— 否则会把字面 `**` 打给用户。
- 图片 `onError` **不要** `hidden:true` 静默移除（现 `answer-bubble.js` 的行为）—— 用「图片已过期 · 点按重新加载」占位 + 重签 URL 重载。

## 文案基准（与 `feedback-bar.js` / `chat.js` 对齐用）

| 场景 | 唯一标准文案 |
| --- | --- |
| 点赞 toast | `感谢反馈 👍` |
| 转人工 toast + 持久行 | `已转交管理员跟进，请留意钉钉消息`（只承诺已发生的事，**禁**「稍后联系您」类时限承诺） |
| 踩提交 toast | `感谢反馈，我们会持续改进` |
| 出错卡 | `回答失败，请检查网络后重试。` + 「重试」 |
| NO_RESULT | 与 `answer_flow.NO_RESULT_MESSAGE` 严格一致 |
| guard 提示条 | `相关资料匹配度较低，以下回答仅供参考，请核对原文或转人工确认。` |
| 副标题健康态 | `企业知识库 · 在线` / `企业知识库 · 服务暂不可用`（由最近一次请求结果驱动，不做无依据承诺） |

## 注意

- 纯前端原型，**未接入真实检索 / LLM**。脚本之外的自由提问会返回固定的"演示说明"兜底回答。
- 所有反馈状态仅存在于浏览器内存，刷新即重置。
