# DingTalk card templates

Exported card-builder JSON for the DingTalk feedback card(s), kept under version control so the
template ↔ backend contract is documented and reviewable. Upload/import these at
**https://open-dev.dingtalk.com/fe/card** to (re)create the platform template, then wire the resulting
template id into the env (below).

## `streaming_rag_feedback_card.json`

The **streaming AI feedback card** for the DingTalk bot (builder name **流式输出RAG卡**, platform template
id `b2395dc5-…-e58da3743623.schema`). Native **AICardContainer** (AI card) — text-only, typewriter
streaming + a feedback action area.

### Backend ↔ template contract (load-bearing)

| Template variable | Set by | Notes |
|---|---|---|
| `answer` | `streaming_update_card(key="answer")` | The streamed answer. `AICardContent` renders this. **Stream key = `answer`** (code default; `DINGTALK_STREAM_CARD_KEY` overrides). |
| `question` / `sources_text` / `meta` | `create_streaming_card` | set up front |
| `message_id` (private) | `create_streaming_card` (privateData) | feedback join key → `qa_session_log.message_id` |
| `feedback_status` (private) | callback response / `update_card_feedback_status` | "✅ 已反馈…" after a click |
| **`is_answer_done`** | **`update_card_data(…, {"is_answer_done":"true"})` on stream finalize** | **Feedback buttons are gated on `is_answer_done=="true"`.** Empty during streaming → buttons hidden; set `"true"` on finalize → buttons appear. ⚠️ **Must be declared as a public variable in the template** (it's referenced in visibility conditions but was not in the export's variableList — add it in 变量/公有变量 or the update is ignored and buttons never show). |

**Feedback button actions** (what the callback handler `/dingtalk/card/callback` expects in
`cardPrivateData.params`): `action` ∈ `upvote` · `downvote` (+ `reason` ∈ `inaccurate`/`incomplete`/
`irrelevant`/`outdated`) · `handoff` · `downvote_other_start` · `downvote_other_submit` — plus `message_id`.

**⚠️ Native like/dislike is NOT wired.** The native `Feedback` component (`enableLikeDislike`) has no
action/callback config (`actionType: none`) — it only feeds DingTalk's internal feedback and does **not**
reach this backend. Backend-logged feedback comes only from the custom buttons above. Don't remove the
custom buttons in favor of the native 👍👎 unless you wire equivalents.

### Enable it
1. Import this JSON in the card builder; ensure `is_answer_done` is a declared public variable; publish.
2. Env: `RAG_DINGTALK_STREAMING=true`, `DINGTALK_STREAM_CARD_TEMPLATE_ID=<template id>`
   (optional `RAG_DINGTALK_STREAM_INTERVAL_MS=500`; `DINGTALK_STREAM_CARD_KEY` defaults to `answer`).
3. Off by default → the bot falls back to the non-streaming finished card, so enabling is opt-in.

> Other card exports (the older regular-interactive `RAG知识库问答反馈卡片_*` variants, image-text
> version, etc.) currently live outside the repo; add them here if/when they become canonical.
