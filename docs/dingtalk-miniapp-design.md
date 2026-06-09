# 钉钉小程序前端设计与开发指南（Fuling RAG）

> 面向：在现有 FastAPI RAG 服务之上，新增一个钉钉**小程序（小程序 / E应用）**前端，提供更好的端内体验，
> 尤其是**图文（screenshot-heavy SOP/ERP）**回答、可靠的赞踩反馈、图片点按放大与服务端部门权限。
>
> 本文反映**已实现**的后端改动 + 已生成的小程序脚手架 + 交互原型，并保留官方参考与上线清单。

---

## 0. 结论速览

- **形态选择：小程序优先。** 现有钉钉「流式机器人卡片」结构性受限——流式路径被强制 `pure_text=True`
  **无法展示图片**（`dingtalk_bot.py:474`），原生赞踩**无可捕获回调**，每个回调必须 **ACK-only** 否则白屏
  （`dingtalk_bot.py:962`），「其他原因」要靠笨拙的回复拦截。小程序拥有自己的渲染树，可原生图文渲染、
  100% 捕获赞踩、内联文本框、`dd.previewImage` 缩放。
- **是否要单独的 SAE 实例？不需要。** 小程序是由钉钉托管分发的客户端包，不在你的服务器上运行。
  **复用现有 api.py 所在的同一 SAE 实例**即可，只需保证**公网 HTTPS 域名**（手机在 VPC 外）并加入安全域名。
- **传输：缓冲 `/api/ask` + 客户端打字机。** 小程序 `dd.httpRequest` **只能缓冲整包、不能流式**
  （`enableChunked`/`onChunkReceived` 是微信独有）。真流式只能走 `dd.connectSocket`（WSS、单连接），v1 不做。
- **安全核心：部门一律服务端解析，绝不信任客户端传入的部门。** 否则任意用户可越权读取他部门
  `dept_internal` 文档。

---

## 1. 目标架构

```
钉钉小程序 (AXML/ACSS/JS, dd.*)
  chat 页
   ├─ dd.getAuthCode() ─► authCode (5 分钟、单次)
   ├─ POST /api/auth/dingtalk {auth_code}        ◄─ {token, user_id, display_name, dept}   ★新增
   ├─ POST /api/ask  (Authorization: Bearer)     ◄─ {message_id, blocks[], sources, model}  ◐改造
   │     └─ 客户端打字机渲染 blocks[]
   ├─ <image mode=widthFix onTap> ─► dd.previewImage()
   └─ POST /api/feedback (Authorization: Bearer) {message_id, feedback_type, ...}            ✓复用
        │ HTTPS（host 必须在安全域名白名单）
   FastAPI api.py（SAE，与机器人同实例）
     ★新增 /api/auth/dingtalk : authCode → userid → 部门/姓名 → 签名令牌
              复用 dingtalk_card._get_access_token + dingtalk_identity（用户身份/部门解析，机器人+小程序共用）
     ◐改造 /api/ask /api/search /api/ask/stream : 部门来自令牌/服务端解析（删除 body user_dept 信任）
              复用 retrieve_and_enrich → generate_answer → build_mini_program_blocks → log_qa_session
     ✓复用 /api/feedback → handle_feedback（user_id 取自令牌）
        └─► HA3（服务端部门过滤）· DashScope Qwen · OSS（签名 HTTPS） · RDS
```

---

## 2. 已实现的后端改动（`opensearch_pipeline/`）

| 文件 | 改动 |
|---|---|
| `auth_token.py`（新增） | 仅用标准库 HMAC-SHA256 的签名会话令牌：`issue_session_token(uid, dept, name, ttl=8h)` / `verify_session_token`。密钥来自 `RAG_SESSION_SIGNING_KEY`（生产缺失则报错，开发缺失则进程级临时密钥）。**部门写在令牌里，客户端不可篡改。** |
| `dingtalk_identity.py`（新增） | **机器人 + 小程序共用的身份基础设施**（从 `dingtalk_bot.py` 抽离，使机器人专注于收发消息）：`_resolve_user_dept`/`_fetch_dingtalk_user_info`/`_fetch_dept_name`（原属 bot，已迁入）；`_get_miniapp_access_token()`（读 `DINGTALK_MINIAPP_CLIENT_ID/SECRET`，**未配置则回退机器人应用凭证**）；`_exchange_authcode_for_userid(code)`（`topapi/v2/user/getuserinfo`，模拟模式返回 `RAG_SIM_USER_ID`）；`_resolve_user_identity(userid)`（返回 `{dept, name}`，**部门为名称字符串**，与 HA3 `owner_dept` 对齐）。 |
| `dingtalk_bot.py` | 瘦身 ~249 行：身份/部门解析迁至 `dingtalk_identity.py`，webhook 仍 `from dingtalk_identity import _resolve_user_dept` 使用。机器人回到「纯收发消息」职责。 |
| `content_blocks_builder.py` | `build_content_blocks` 新增可选 `max_caption_len=100`（默认不变，卡片路径零影响）；新增 `build_mini_program_blocks()`：复用核心逻辑，仅把 `markdown→{type:text}`、`image→{type:image,url,caption,alt}`，caption 取全文。 |
| `api.py` | 新增 `Identity` + `current_identity`（可选 Bearer 依赖）；新增 `POST /api/auth/dingtalk`；`/api/ask`、`/api/search`、`/api/ask/stream` **删除 body `user_dept` 信任**，统一 `uid/部门` 服务端解析；`AskResponse` 新增 `blocks[]`；`/api/feedback` 的 `user_id` 取自令牌。 |
| `tests/test_miniapp_serving.py`（新增） | 9 个测试：令牌往返/防篡改/过期、blocks 重映射（全文 caption）、`/api/auth/dingtalk` 免登、**`/api/ask` 部门取令牌而非 body**、无令牌按 `user_id` 解析、`/api/feedback` user_id 取令牌。 |

> 安全不变量：部门**只**来自签名令牌或服务端按 `user_id` 解析，**绝不**接受 body `user_dept`（字段保留但服务端忽略，描述已标注「已废弃·服务端忽略」）。`_sanitize_ha3_filter_value` 仍作纵深防御。未解析到部门 ⇒ **只返回 public**。

### `/api/ask` 返回的 blocks 结构（图文）

```json
{
  "message_id": "uuid", "session_id": "uuid", "model": "qwen3.6-plus",
  "answer": "（向后兼容的纯文本）",
  "blocks": [
    { "type": "text",  "format": "markdown", "text": "第一步，打开 U8+ 系统设置…" },
    { "type": "image", "url": "https://…oss-cn-chengdu.aliyuncs.com/…?Signature=…",
      "caption": "图片内容：登录界面，点击右上角设置图标", "alt": "登录界面" },
    { "type": "text",  "format": "markdown", "text": "第二步，…" }
  ],
  "sources": [ { "doc_id": "…", "title": "U8+操作手册", "section": "用户登录", "score": 8.4 } ]
}
```
纯文字 / LLM 未用 `<<IMG:N>>` 引用图片时 `blocks` 为 `[]`（沿用 referenced-only 行为）。

---

## 3. 免登与权限（关键）

1. **客户端** `dd.getAuthCode()`（**不是** `dd.runtime.permission.requestAuthCode`，那是 H5 的）→ 5 分钟单次 `authCode` → POST 后端。
2. **服务端 `/api/auth/dingtalk`**：缓存 app `access_token` → `getuserinfo` 换 `userid` → `_resolve_user_identity` 解析部门(名称)+姓名 → 签发 8h 令牌。
3. **后续请求** 带 `Authorization: Bearer`；`current_identity` 注入 `user_id/dept`；端点**只**用令牌里的部门。

**纠正既有文档的两处过时说法（已核实代码）：**
- `/api/ask/stream` **已经**生成并下发 `message_id`（`api.py` `session` 帧）并在 `finally` 落库——流式回答可被反馈，CLAUDE.md 旧注「streaming 无 message_id」**已过时**。
- `/api/debug/rds` **已不存在**（当前仅 5 条 `/api` 路由）。

**新应用 vs 复用机器人应用：** 小程序通常是**独立 app**（独立 AppKey/AppSecret/AgentId），其 `authCode` 必须用**该 app 的** `access_token` 兑换。代码已用 `_get_miniapp_access_token()` 处理：配 `DINGTALK_MINIAPP_CLIENT_ID/SECRET` 用独立应用，未配则回退机器人应用。

---

## 4. 前端：小程序客户端（`fuling-rag-miniapp/`）

已生成（Alipay 引擎 AXML/ACSS、`dd.*`、`didMount/didUpdate/didUnmount`）：

```
fuling-rag-miniapp/
├── app.json / app.js / app.acss / package.json / .gitignore / README.md
├── utils/   config.js(BASE_URL) · auth.js(免登+缓存) · api.js(dd.httpRequest+401重登) · typewriter.js
├── pages/   chat/*（核心问答：scroll-view 气泡 + 输入栏） · settings/*（部门/会话只读 + 清除会话）
└── components/  answer-bubble/*（blocks 图文 + 打字机 + previewImage） · feedback-bar/*（赞踩/转人工 + 原因/文本框）
```

要点：缓冲 `/api/ask` + 客户端打字机；`<image mode="widthFix">` 自适应未知尺寸 OSS 截图；点按 `dd.previewImage`
（传全部图片 url）；`feedback_type ∈ {upvote,downvote,downvote+reason/comment,handoff}` 直接 POST `/api/feedback`；
`session_id = "miniapp:" + userId`。需填占位：`utils/config.js` 的 `BASE_URL`、IDE 的 AppKey、控制台安全域名/出口IP/接口权限。

## 5. 前端原型（`fuling-rag-miniapp/prototype/index.html`）

自包含 HTML（内联 SVG 模拟截图，无网络依赖）：手机框 + 图文回答 + 打字机/骨架 + 赞踩/转人工（原因 chips + 文本框）+ 点按灯箱缩放 + 脚本化示例问答 + 可折叠参考来源。双击即可在浏览器打开（或 `python3 -m http.server --directory fuling-rag-miniapp/prototype`，窗口 ≥480px）。可作为 `answer-bubble` 组件的 1:1 设计参照。

---

## 6. 上线清单（开发者后台 / SAE）

- **同一 SAE 实例**承载 api+bot；镜像不打包 `.env`，注入全部环境变量（含新增 `RAG_SESSION_SIGNING_KEY`、可选 `DINGTALK_MINIAPP_CLIENT_ID/SECRET`）。
- 后台创建：企业内部自主开发 → **应用类型=小程序**；记录 AppKey/AppSecret/AgentId（Secret 仅服务端）。
- 授予 **接口权限** 通讯录个人信息读（`auth_user`）；白名单 **服务器出口IP**；**安全域名** 加入 **API host 与 OSS host**（`<image>` 与 `previewImage` 都需要 OSS host）。
- 设置 `CORS_ALLOWED_ORIGINS`（若有 H5/浏览器后台调用）。
- 版本流程：IDE 上传 → 体验版（试点）→ 灰度 → 线上版。**安全域名变更需重新打包上传**，客户端缓存约 10 分钟。
- **免登只能在真机验证**（IDE 模拟器无法签发有效 authCode）。
- DevTools 版本：**到官方 changelog 确认当前稳定的免登兼容版本**（研究报告中「3.9.22」无法独立证实，勿硬编码）。

## 7. 分阶段交付

| # | 里程碑 | 退出标准 |
|---|---|---|
| M1 | 免登 + 单轮纯文本问答 | 真机上登录员工得到文本答案；`qa_session_log` 记录**服务端解析的** user_id/dept；body 不含 user_dept |
| M2 | 原生图文 + 缩放 | 截图密集 SOP 内联渲染图片（OSS host 已白名单）；点按缩放可左右滑动 |
| M3 | 客户端打字机 + loading + 多轮 | 打字机动画 + 骨架占位；追问带上下文 |
| M4 | 反馈（赞踩/转人工/其他原因） | 赞踩写 `user_feedback`（按 message_id）；其他原因一步提交；转人工建 `escalation_ticket` |
| M5 | 权限加固 + 安全回归 | A 部门用户即使篡改请求也读不到 B 部门 `dept_internal`；无令牌→public-only/401 |
| M6 | 体验版→灰度→线上 | 安全域名先于打包定稿；试点一周；灰度→全量 |

## 8. 风险 / 注意

1. 安全域名变更需重新打包上传（~10 分钟客户端缓存）——把 API host + OSS host 在打包前定稿。
2. 小程序无 HTTP 流式——用缓冲 `/api/ask` + 客户端打字机；真流式需 WSS（延后）。
3. `session_store` 进程内内存——多 worker/多副本会丢上下文；上线前 Redis 化，或单 worker / 粘性会话 / 客户端回传 history。
4. OSS 签名 URL 3600s 过期——历史回看需 `/api/resign-images` 重签（延后，v2）。
5. `setData` ~1MB 上限——图片走 OSS HTTPS URL，**绝不** base64 内联。
6. 模拟器 vs 真机免登差异；AppSecret/签名密钥仅服务端。

## 9. 官方参考

| URL | 用途 |
|---|---|
| https://open.dingtalk.com/document/development/jsapi-get-auth-code | `dd.getAuthCode` 小程序免登（5 分钟单次） |
| https://open.dingtalk.com/document/development/obtain-the-userid-of-a-user-by-using-the-log-free | `topapi/v2/user/getuserinfo` authCode→userid |
| https://open.dingtalk.com/document/orgapp/obtain-orgapp-token | app `access_token`（AppKey/Secret，2h，缓存） |
| https://open.dingtalk.com/document/development/queries-the-detailed-information-of-a-user | `topapi/v2/user/get` 部门解析 |
| https://open.dingtalk.com/document/orgapp/send-network-requests | `dd.httpRequest`（headers/status，仅缓冲） |
| https://open.dingtalk.com/document/client/dd-connectsocket | `dd.connectSocket`（WSS，真流式备选） |
| https://miniprogram.alipay.com/docs-alipayconnect/miniprogram_alipayconnect/mpdev/component_multimedia_image | `<image mode=widthFix>` |
| https://open.dingtalk.com/document/development/dd-previewimage | `dd.previewImage` 缩放 |
| https://open.dingtalk.com/document/development/mini-app-component-lifecycle | `didMount/didUpdate/didUnmount` |
| https://www.npmjs.com/package/dingtalk-design-miniapp | DingUI 组件（无 card/toast） |
| https://github.com/open-dingtalk/eapp-corp-quick-start-fe | 官方企业内部脚手架 + 免登范例 |
| https://open.dingtalk.com/document/orgapp/upload-and-publish-mini-programs | 上传/体验/灰度/线上 + 安全域名 |
