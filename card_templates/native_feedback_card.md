# 钉钉官方「赞踩」AI 流式卡模版 — 分析与接入

> 原始模版导出 JSON：[`native_feedback_card.json`](./native_feedback_card.json)（`editorData` + `widgetInfo` + `type`/`mode`，原样保存）。
> 用户在钉钉卡片搭建台导出，作为我们流式反馈卡的参考实现。

## 一句话结论

**它根本没用原生 `Feedback` 组件**（那个组件在本模版里 `enableLikeDislike=false / enableCopy=false`，等于禁用）。
赞踩完全靠**自定义 `SingleButton`**实现，并且**「踩 → 内联填原因」靠客户端本地态 `setLocalState`**，
不发回调、不更新卡片 → **不会白屏**。这正解决了我们之前以为做不到的事。

## 交互拆解（全部来自导出文件，已用脚本核对）

| 控件 | node id | actionType | 行为 | 回调参数 |
|---|---|---|---|---|
| 👍 赞 | `node_ocm9i4q4z8d` | `eventChain` | ① `request` 回调 ② 本地 `bad=false` | `feedback="good"`, `content`, `query` |
| 👎 踩 | `node_ocm9i4q4z8m` | `setLocalState` | 本地 `bad=true` → 露出输入框（**无回调**） | — |
| 原因输入框 | `node_ocm9jj2tf22` | — | `visible: bad==true`，写入本地变量 `comment` | — |
| 提交 | `node_ocm9jj2tf2b` | `request` | 发回调 | `feedback="bad"`, `comment`, `content`, `query` |
| 取消 | `node_ocm9jj2tf2c` | `setLocalState` | 本地 `bad=false`（收起输入框） | — |
| ~~原生 Feedback~~ | `node_ocm9i4q4z8a` | `request` | **禁用**（`enableLikeDislike=false`） | — |

**关键点**
- 回调参数名是 **`feedback`（good/bad）+ `comment`**，**不是** `action`/`reason`；**不带 `message_id`** → 用回调体里的 `outTrackId` 兜底当 message_id。
- 「踩 → 填原因」是**纯客户端本地态**（`setLocalState bad=true` + `visible: bad==true`），不触发服务端卡片更新 → 不冲掉流式正文。**这是内联自由文本不白屏的正解。**
- 只有「👍」和「提交」是 `request`（真回调）。回调响应仍须 **ACK-only（不返回 `cardData`）**，否则照样白屏（已三次实证）。
- `widgetInfo` 里 status 2 / status 3 都绑到 `@data{data.cardData.content}` —— 正文变量名是 **`content`**，与后端推流 key 一致。

## 接入状态（已完成）

`dingtalk_bot.py::card_callback` 已兼容这套模版：
- `params.feedback == "good"` → `upvote`；`== "bad"` → `downvote`。
- `params.comment` 透传到 `handle_feedback(..., reason="other", comment=...)` → 落 `fuling_operation.user_feedback.feedback_comment`。
- 无 `message_id` 时用 `outTrackId`。
- 一律 ACK-only（返回 `{}`）。
- 回归测试：`tests/test_dingtalk_streaming.py::test_card_callback_official_feedback_template`。

## 若要切到这张模版（可选）

1. 在卡片搭建台保留这套自定义按钮（👍/👎/输入框/提交/取消），把正文 Markdown 绑到 `content`。
2. 把 `DINGTALK_STREAM_CARD_TEMPLATE_ID` 换成这张模版的 schema id，重新发布。
3. 后端**无需再改**（`card_callback` 已认 `feedback`/`comment`）。
4. 想要「转人工」就再加一个 `SingleButton`：`actionType=request`、参数 `action="handoff"`（现有回调已处理）。
   —— 这张官方模版本身**没有转人工**，需要的话单独加一个按钮即可。
