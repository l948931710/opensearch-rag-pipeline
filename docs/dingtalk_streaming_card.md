# 钉钉流式 AI 卡片（打字机）配置与排障

这份文档是 2026-06-08 那次"流式卡片一直 500 / 按钮不显示"排查的结论沉淀，配 `dingtalk_bot.py`
+ `dingtalk_card.py` + `card_templates/streaming_rag_feedback_card.json` 一起看。

## 一、能跑的必要条件（缺一不可）

| 项 | 正确值 | 说明 |
|---|---|---|
| 消息接收模式 | **HTTP** | 本服务是 HTTP webhook（`/dingtalk/webhook`），不是 Stream 模式 SDK。选 Stream 收不到消息 |
| 流式变量名 | **`content`** | 钉钉 AI 流式卡片的约定变量；用 `answer` 等自定义名 → `PUT /card/streaming` 500 unknownError |
| 变量类型 | **富文本 / Markdown**（`varType=markdown`） | 流式接口只能写 markdown 类型变量 |
| 流式组件 | **MarkdownBlock**，`isStreaming=true`（输出中）/`false`（完成态），两态都绑 `content` | 普通 Markdown 不会逐字动；isStreaming=false 不流 |
| 推流 key | `content`（代码默认；env `DINGTALK_STREAM_CARD_KEY` 可覆盖） | **推流 key 必须 == 流式组件绑定的变量名**，否则 500 |
| guid | 每帧新的标准 UUID（`str(uuid.uuid4())`） | 复用/无连字符 → 500（代码已修） |
| 应用权限 | `Card.Streaming.Write` + `Card.Instance.Write` | 没开 → 500（本租户已开） |
| isFull | `true` | 推累计全文，覆盖式（代码已对） |

## 二、环境变量（SAE）

```
RAG_DINGTALK_STREAMING=true
DINGTALK_STREAM_CARD_TEMPLATE_ID=<流式模板 id>
# DINGTALK_STREAM_CARD_KEY=content        # 默认就是 content，一般不用设
DINGTALK_CARD_CALLBACK_URL=https://<公网域名>/dingtalk/card/callback   # 注意是 card/callback 不是 webhook
RAG_PURE_TEXT=true                          # 钉钉只出纯文本
```

## 三、反馈按钮 spec（后端按这些 action/reason 识别，必须一字不差）

按钮组显示条件（完成态）：`feedback_status` 为空 **且** `show_other_feedback_form` 为空。

| 按钮 | 类型 | actionId | 参数 |
|---|---|---|---|
| 喜欢 | 普通按钮 FixedSingleButton | `btn_upvote` | action=`upvote`, message_id |
| 不喜欢 | 下拉菜单 DropdownButton | `feedback_downvote` | 见下方菜单 |
| 转人工 | 普通按钮 FixedSingleButton | `handoff` | action=`handoff`, message_id |

不喜欢下拉菜单项（每项 action=`downvote` + reason + message_id）：
`答案不准确`→inaccurate · `答非所问`→irrelevant · `回答不完整`→incomplete · `内容已过时`→outdated · `未找到答案`→not_found ·
**`其他原因`** → actionId=`feedback_downvote_other_start`，action=`downvote_other_start`，reason=`other`（触发"其他原因"输入表单）。

⚠️ **导入 JSON 经常把交互按钮（尤其下拉菜单按钮）搞坏**：表现为只剩第一个"喜欢"、后面的不喜欢+转人工都不显示。
若发生，用一个"按钮确实能用"的模板（如 `图文版本1`）把按钮组**整组移植**过来，或在 console 里**原生重建**这三个按钮。

## 四、测试方式：部署前先验（diag）

`scratch/diag_streaming.py`：用应用凭证对**任意模板 id** 实测 `createAndDeliver` + `PUT /card/streaming`
（STREAM/HTTP 都测），当场看 200/500。**改完模板先跑它确认流式 200，再部署。**

```
DINGTALK_CLIENT_ID=... DINGTALK_CLIENT_SECRET=... \
DINGTALK_STREAM_CARD_TEMPLATE_ID=<新模板id> DINGTALK_STAFF_ID=<你的staffId> DINGTALK_STREAM_KEY=content \
python scratch/diag_streaming.py
```

对照基准：官方公开测试模板 `8aebdfb9-28f4-4a98-98f5-396c3dde41a0.schema`（实测 200），
用来区分"模板问题"还是"账号/代码问题"。

**注意范围**：diag 只能验"流式接口 200/500"；按钮**渲染出没出来**是视觉的，只能在钉钉客户端里目测
（diag 发的测试卡也能看打字机）。组件配置层面的问题用脚本解析模板 JSON（`editorData` → `componentsTree`）排查。

## 五、根因链（速查）

消息接收用 HTTP → 开 `RAG_DINGTALK_STREAMING` + 配流式模板 id → 流式变量必须叫 `content`（非 answer）
→ 流式组件 `isStreaming=true`+`varType=markdown`+两态都 MarkdownBlock → 开 `Card.Streaming.Write` 权限
→ 这些**手动在 console 改容易改错，导入结构正确的 JSON 更稳**；但导入又可能搞坏交互按钮 → 按钮从能用的模板移植。
