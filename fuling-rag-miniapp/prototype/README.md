# 富岭知识助手 · 钉钉小程序 UX 原型

一个**自包含、可点击**的 HTML 原型（"Claude design" mockup），用于在开发真正的 `.axml` 小程序之前，让开发者 / 干系人**看到并点击**预期的交互体验。

它复刻了浙江富岭塑胶（Fuling Plastics）企业知识库 RAG 问答机器人在**钉钉小程序**中的回答渲染方式 —— 与后端 `content_blocks_builder.py` 输出的**图文穿插内容块**一一对应。

## 这是什么

- 一部 **390px 宽的手机框**，居中显示在中性背景上。
- 钉钉风格 chrome：顶部蓝色（`#1677ff`）导航栏「富岭知识助手」、聊天区、底部输入栏 + 「发送」按钮。
- AI 回答由一个 **`blocks[]` 数组**渲染（`{type:'text'}` / `{type:'image'}`），文本块渲染为段落、图片块渲染为内联图 + 灰色图注 —— 这是核心的「图文 generation」体验。
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
| 示例问答 | 点击底部「示例问题」快捷栏（如 *U8+ 如何登录？*）→ 自动填入输入框并发送 |
| 加载骨架 | 发送后约 1.2s 显示 typing 骨架气泡，随后流式吐字 |
| 打字机 | AI 文本逐字（约 28ms/字）揭示，图片在前序文本完成后淡入插入 |
| 图文穿插 | 多步 SOP 答案里文字与截图交替排布 |
| 图片放大 | 点击任意内联图 → 全屏黑底 lightbox，左右箭头 / 键盘 ←→ / 触摸滑动切换，× 关闭 |
| 参考来源 | 每条回答底部可折叠的「参考来源」（文档标题 · 章节 · 分数） |
| 赞 | 点击 👍 → 高亮 + 「已记录」toast，按钮锁定 |
| 踩 | 点击 👎 → 展开原因 chips（内容不准确 / 答非所问 / 图片不对 / 信息过时）+ 「其他原因」文本框 → 提交 → 「感谢反馈」toast |
| 转人工 | 点击 🧑‍💼 → 「已转人工，稍后联系您」toast |

预置 3 段脚本问答：1 段截图密集的多步 SOP（*U8+ 登录*，含 2 张截图）、1 段带流程图的中等答案（*请假流程*）、1 段纯文本短答案（*访客 WiFi*）。

## 给开发者：1:1 映射

原型把所有逻辑放在 vanilla JS 里，开发者可直接映射到真实小程序组件 / AXML：

| 原型元素 (index.html) | 真实小程序组件 / AXML / 后端 |
| --- | --- |
| `.phone` / `.screen` | 钉钉小程序运行容器（无需自己画，仅原型用） |
| `.navbar`「富岭知识助手」 | `app.json` `window.navigationBarTitleText` + `navigationBarBackgroundColor:#1677ff` |
| `.chat` 消息列表 | 页面 `<scroll-view scroll-y scroll-into-view>`（聊天页 `pages/chat/index.axml`） |
| `.msg.user .bubble` | 用户气泡 `<view class="bubble user">`，右对齐 |
| `.ai-card`（答案卡片） | **`answer-bubble` 自定义组件**（核心复刻目标） |
| `renderAnswer()` / `typeBlocks(blocks)` | 组件内 `<block wx:for="{{blocks}}">` 循环渲染（对应钉钉模板的 Loop 组件） |
| `blocks[] {type:'text', text}` | `{type:'markdown', content}`（后端 `content_blocks_builder.py`）→ `<text>` / `<rich-text>` 节点 |
| `blocks[] {type:'image', svg, caption, alt}` | `{type:'image', url, caption}`（`url` = OSS 签名 URL，源自 `image_refs.oss_key` → `oss_url.generate_signed_url`）→ `<image mode="widthFix">` + `<text class="caption">` |
| 内联 SVG 截图 | 真机为 `<image src="{{block.url}}">`（OSS 签名 URL，1h 过期） |
| `typeText()` 逐字 | 流式回答：SSE / 钉钉流式卡片，**stream 变量 = `content`**（见 streaming-feature 记录） |
| 加载骨架 `.skel` | answer-bubble 的 `loading` 态 / 钉钉流式卡片占位 |
| 图片 lightbox | **`dd.previewImage({ urls, current })`**（钉钉小程序内置预览 API） |
| `.sources`「参考来源」 | 折叠组件 `<view>`，数据来自检索 `chunks[].{doc_title, section, score}`（`retriever.py`） |
| `.fb` 反馈条 👍/👎/🧑‍💼 | answer-bubble footer；上报 `POST /api/feedback`（`feedback_handler.py`，`feedback_type` = upvote/downvote/handoff） |
| 👎 原因 chips | `feedback_reason`（内容不准确 / 答非所问 / 图片不对 / 信息过时） |
| 「其他原因」文本框 | `feedback_comment`（真机里 = DM-reply 补充原因流程） |
| `toast()` | `dd.showToast({ content })` |
| `.inputbar` + 发送 | 底部输入组件 → `POST /api/ask`（带 `session_id`，返回 `message_id` 供反馈关联） |
| `.quick` 示例问题 | 首屏快捷提问 chips（引导首次使用） |

### 与后端契约对齐的关键点

- 真实 `content_blocks` 元素为 `{type:'markdown', content}` 与 `{type:'image', title, url, caption}`；原型用 `{type:'text', text}` / `{type:'image', ...}` 表意等价，开发时按上表换名即可。
- 图片来源是 `image_refs` 字典（`oss_key` / `source_image` / `visual_summary` / `ocr_text` / `image_index`），这是 extractor → chunker → content_blocks_builder → 钉钉卡片的 **load-bearing contract**，渲染层只消费 `url`(=签名 oss_key) 与 `caption`(=visual_summary/caption)。
- LLM 用 `<<IMG:N>>` 标记图片插入位置；后端据此把 markdown 拆成图文穿插块。原型已按"图文交替"的最终形态呈现。
- 反馈与回答通过 `message_id` 关联；流式 `/api/ask/stream` 当前不记录日志、无 `message_id`（已知限制），生产建议走 `/api/ask`。

## 注意

- 纯前端原型，**未接入真实检索 / LLM**。脚本之外的自由提问会返回固定的"演示说明"兜底回答。
- 所有反馈状态仅存在于浏览器内存，刷新即重置。
