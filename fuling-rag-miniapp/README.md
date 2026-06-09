# 富岭知识助手 · 钉钉小程序 (DingTalk mini-program client)

A DingTalk **corp-internal mini-program** (小程序, Alipay engine) front-end for the
浙江富岭塑胶 (Fuling Plastics) enterprise RAG knowledge base. Employees ask a
question and get an answer with **interleaved screenshots** (SOP / U8 ERP docs),
plus 赞踩 / 转人工 feedback and a client-side typewriter effect.

> This is the **Alipay engine**, not WeChat: markup is **AXML**, styles are
> **ACSS**, APIs are **`dd.*`**, events bind via **`onTap`**, lists use **`a:for`**,
> and components use the **`didMount` / `didUpdate` / `didUnmount`** lifecycle.

---

## 1. Prerequisites

- **钉钉开发者工具 (DingTalk DevTools IDE)** — install the **current version that
  supports 免登 (corp SSO)**. Do **not** hardcode a version here; check the official
  changelog and download the latest免登-compatible IDE build:
  <https://open.dingtalk.com/document/resourcedownload/download-the-development-tool>
- A **corp-internal mini-program** created in the DingTalk developer console
  (开发者后台 → 应用开发 → 小程序), which gives you an **AppKey / MiniAppId**.
- Node.js + npm (for installing the component library).

This scaffold follows the official corp mini-program quick-start:
**`open-dingtalk/eapp-corp-quick-start-fe`**
<https://github.com/open-dingtalk/eapp-corp-quick-start-fe>

---

## 2. Install & configure

```bash
cd fuling-rag-miniapp
npm install
```

Then set the backend host (single source of truth):

- Edit **`utils/config.js`** → `BASE_URL` → your real API host
  (e.g. `https://rag.fuling.example.com`). **TODO for the developer.**

Open the project in the **钉钉开发者工具** and fill in your **AppKey / MiniAppId**
in the IDE project settings (**TODO** — not stored in this repo).

---

## 3. DingTalk developer console steps (required for免登 + network)

In 开发者后台 → 你的应用 → 应用能力 / 应用信息:

1. **服务器安全域名 (安全域名)** — add the **exact** `BASE_URL` host
   (scheme + domain, no path). `dd.httpRequest` is **blocked** for any host not on
   this list. ⚠️ **Changing 安全域名 later requires re-package + re-upload** of the
   mini-program — it is not a hot change.
2. **服务器出口 IP 白名单** — if your backend validates免登码 by calling DingTalk's
   server API (`gettoken` / `user/getuserinfo`), whitelist your **backend's egress
   IP** under the app's 服务器出口IP. (This is on the server side, not the client.)
3. **接口权限 (API scopes)** — grant the member-read scope so the backend can
   resolve the免登码 to a user: **通讯录个人信息读权限 (`Contact.User.Read` /
   `qyapi_get_member` — "成员信息读权限" / `auth_user`)**. Without it, the
   `/api/auth/dingtalk` exchange cannot resolve `user_id` / `display_name` / `dept`.

---

## 4. Testing免登 — REAL DEVICE only

`dd.getAuthCode` mints a **5-minute, single-use**免登码 and only works inside the
**real DingTalk client on a real device**. The **IDE simulator cannot produce a
valid authCode** — login will appear to fail there. To test the full login + ask
flow:

- In the IDE, use **预览 (Preview)** → scan the QR code with DingTalk on your phone,
  **or** push a **体验版 (trial build)** and open it from the DingTalk workbench.

---

## 5. Release flow

Standard DingTalk mini-program rollout:

```
本地开发 (IDE)  →  上传 (Upload)  →  体验版 (Trial)  →  灰度 (Gray release)  →  线上版 (Production)
```

1. **上传** the build from the IDE to the developer console.
2. Promote the uploaded version to **体验版** and verify on real devices.
3. Optionally roll out a **灰度** (percentage / whitelist) release.
4. Promote to **线上版 (Production)** for all employees.

> Reminder: any change to **安全域名** (or other package-baked config) needs a
> **re-package + re-upload**, then re-promotion through体验版 → 灰度 → 线上.

---

## 6. Backend API contract (consumed by this client)

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `POST /api/auth/dingtalk` | none | exchange `{auth_code}` →`{token,user_id,display_name,dept}` |
| `POST /api/ask` | `Bearer` | `{question,session_id}` → `{message_id,blocks[],sources[],...}` |
| `POST /api/feedback` | `Bearer` | `{message_id,feedback_type,feedback_reason?,feedback_comment?}` |

`blocks[]` interleaves `{type:"text",...}` and `{type:"image",url,caption,alt,...}`;
the client renders text via a typewriter and images via `<image mode="widthFix">`
with tap-to-`dd.previewImage`.

> Streaming note: `dd.httpRequest` is **buffered only** (no SSE). This client uses
> the **non-streaming `/api/ask`**, then animates the answer client-side. (The
> backend's `/api/ask/stream` is intentionally not used here.)

---

## 7. Project structure

```
fuling-rag-miniapp/
  app.json / app.js / app.acss        小程序入口、全局数据、全局样式
  package.json / .gitignore
  utils/
    config.js        BASE_URL (单一配置源)
    auth.js          免登: dd.getAuthCode → /api/auth/dingtalk → 缓存 token
    api.js           dd.httpRequest 封装 + 401 自动重登拦截器; ask()/feedback()
    typewriter.js    客户端打字机 (~30ms/字, 可取消)
  pages/
    chat/            核心问答页 (消息列表 + 输入栏)
    settings/        我的: 资料只读 + 清除会话 + 版本
  components/
    answer-bubble/   图文交错渲染 + 打字机 + 点击预览大图
    feedback-bar/    👍 / 👎(原因面板) / 转人工
```

---

## 8. Developer TODO checklist

- [ ] `utils/config.js` → set real `BASE_URL`.
- [ ] DingTalk IDE → fill in **AppKey / MiniAppId**.
- [ ] 开发者后台 → **安全域名** add the `BASE_URL` host.
- [ ] 开发者后台 → **服务器出口IP** whitelist the backend egress IP.
- [ ] 开发者后台 → **接口权限** grant member-read (`auth_user`) scope.
- [ ] Test免登 on a **real device** (not the simulator).
