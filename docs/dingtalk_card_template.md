# 钉钉互动卡片模板配置指南

## 模板变量定义

在 card.dingtalk.com 的"变量管理"中，确保定义以下变量：

| 变量名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `title` | String | "" | 用户问题标题 |
| `question` | String | "" | 用户原始问题 |
| `answer` | String | "" | RAG 回答正文 |
| `content_blocks` | String | "" | **图文穿插 JSON 数组**（核心！） |
| `sources_text` | String | "" | 参考来源文本 |
| `meta` | String | "" | 模型 + 耗时信息 |
| `feedback_status` | String | "" | 反馈状态 |
| `show_other_feedback_form` | String | "" | 其他原因表单显示状态 |

## 模板结构（关键部分）

### 回答区域 — 图文穿插

模板需要用**条件渲染**来切换纯文本和图文两种模式：

**条件判断**：`content_blocks` 是否为空
- 空 → 显示 `answer` 纯文本 markdown
- 非空 → 用 Loop 组件遍历 `content_blocks` 渲染图文

### 在卡片编辑器中的配置步骤

1. **进入 card.dingtalk.com → 打开你的模板**

2. **添加条件渲染组件**：
   - 条件：`${content_blocks}` 不为空
   - If 分支：添加 Loop 组件，数据源设为 `${content_blocks}`
   - Else 分支：添加 Markdown 组件，内容设为 `${answer}`

3. **Loop 组件内部**：
   - 添加条件组件
   - 条件：`${item.type}` == "markdown"
     - 添加 Markdown 文本组件，内容 = `${item.content}`
   - 条件：`${item.type}` == "image"
     - 添加 Image 图片组件，URL = `${item.url}`，标题 = `${item.caption}`

4. **参考来源区域**（只显示一次）：
   - 添加 Markdown 组件
   - 内容：`**参考来源：**\n${sources_text}`
   - 条件：`${sources_text}` 不为空时显示

## content_blocks 数据格式

后端传入的 `content_blocks` 是 JSON 字符串，解析后为数组：

```json
[
  {"type": "markdown", "content": "U8+成品仓库的核心操作流程..."},
  {"type": "image", "title": "系统界面截图", "url": "https://xxx.oss-cn-hangzhou.aliyuncs.com/...", "caption": "来源：《U8+操作手册》"},
  {"type": "markdown", "content": "接下来需要填写单号..."},
  {"type": "image", "title": "填写示例", "url": "https://xxx.oss-cn-hangzhou.aliyuncs.com/...", "caption": "来源：《U8+操作手册》"}
]
```

## ⚠️ 注意事项

1. **图片 URL 必须是 HTTPS** — 代码已确保生成 HTTPS 签名 URL
2. **签名 URL 有效期 1 小时** — 超过后图片将无法加载
3. **钉钉 Image 组件支持的图片格式**：JPEG、PNG、GIF
4. **图片最大尺寸**：钉钉建议宽度不超过 750px

## 简化方案（如果 Loop 组件不可用）

如果你的钉钉版本/权限不支持 Loop 组件，可以用**纯 Markdown 方案**：

后端直接把图片渲染为 Markdown 图片语法：`![描述](https://url)`

这需要修改 `send_interactive_card` 将 content_blocks 转为 Markdown 字符串。
