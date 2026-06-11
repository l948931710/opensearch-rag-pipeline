# 环境硬事实（端口/命令/选择器/取证模板）

> 凭证规则：所有生产访问经 `opensearch_pipeline.prod_access`（脚本内已封装）。
> 本文件不含任何密钥。

## 后端（SAE 同构形态，Bash 后台运行）

```bash
RAG_ENV=prod_ro RAG_RERANK_ENABLE=true RAG_LOW_CONFIDENCE_GUARD=true \
RAG_MAX_CONTEXT_CHARS=10000 \
python3 -m uvicorn opensearch_pipeline.api:app --host 127.0.0.1 --port 8001 --log-level warning
```

- 就绪探测：`until curl -s -m 4 http://127.0.0.1:8001/api/health | grep -q ok; do sleep 2; done`
- ⚠️ 改任何 serving 代码后必须 `pkill -f "uvicorn opensearch_pipeline.api:app"` 重启再测。
- `.env.prod_ro` 含完整 HA3 公网端点配置（`…public.ha.aliyuncs.com`，HTTP/80）与
  只读 ack；shell 导出的变量若与 env 文件重名会被文件覆盖（override=True），上面
  四个变量不在 prod_ro 文件里所以 shell 设置有效。
- SAE 形态基线（与生产对齐的依据）：rerank ON、低置信守卫 ON、上下文 10000；
  若生产配置日后变化，以 SAE 实际环境变量为准更新此处。

## 前端（小程序原型 + LIVE 桥）

- 启动：preview 工具 `preview_start`，配置名 **`miniapp-prototype`**
  （`.claude/launch.json`：`python3 -m http.server 4599 --directory fuling-rag-miniapp/prototype`）。
- LIVE 桥：导航 `/?api=http://127.0.0.1:8001`（参数名就叫 `api`）。
  成功标志 = 标题栏出现「LIVE实测」。无此参数时页面跑的是内置 mock 剧本。
- 视口：`preview_resize` preset `mobile`（375×812，小程序为移动端设计）。

## DOM 取证（preview_eval 模板）

选择器：输入框 `#input`（textarea）、发送 `#send`、消息气泡 `.msg`。

最后一条答案的全要素取证：

```js
(() => { const msgs = [...document.querySelectorAll('.msg')]; const last = msgs[msgs.length-1];
  const t = last ? last.textContent.replace(/\s+/g, ' ') : '';
  const imgs = last ? [...last.querySelectorAll('img')] : [];
  return {
    n_msgs: msgs.length,                       // ⚠️ 与已提问次数核对，防读到旧气泡
    steps: (t.match(/第\d+步/g)||[]).length,
    refusal: t.includes('未找到相关信息') || t.includes('未找到相关内容'),
    n_imgs: imgs.length,
    loaded: imgs.filter(i => i.complete && i.naturalWidth > 0).length,
    alts: imgs.map(i => (i.alt||'').slice(0, 40)),
    img_docs: imgs.map(i => { const m = decodeURIComponent(i.src||'').match(/DOC_[A-Z_]+_\d+_[0-9A-F]{6}/); return m ? m[0].slice(-6) : '?'; }),
    srcs_panel: (t.match(/参考来源[\s\S]{0,160}/)||[''])[0].slice(0, 160),
  }; })()
```

- `img_docs` 用图片 URL 里的 doc_id 判图片归属（孪生/串图问题一查便知）。
- 验证"正文无来源泄漏"：检查正文不含「来源依据」「参考来源：」等段（注意排除
  结构化面板自身的「参考来源 N 条」字样）。
- 网络层验图：`preview_network` 看签名 OSS URL 是否 200；失败项用 `filter:"failed"`。
- 截图：先 `scrollIntoView` 目标图（按 alt 关键词找），再 `preview_screenshot`。

## 时序

- 生成 6-18s（rerank+LLM）+ 打字机渲染数秒。等待用 until 循环 gate（直接
  `sleep N` 会被环境拦截）：
  `i=0; until [ $i -ge 5 ]; do curl -s -m 4 http://127.0.0.1:8001/api/health >/dev/null; sleep 4; i=$((i+1)); done`
- 取证前先核对 `n_msgs` 是否等于 问候(1)+已问×2+1；打字机进行中 `len` 很小或
  内容只有头像——再等一轮。

## API 直连（绕过 UI 的对照/批量手段）

```bash
curl -s -m 120 -X POST http://127.0.0.1:8001/api/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "…", "user_id": "live-eval"}'
```
响应关键字段：`answer`/`no_result`/`guard`/`sources[].{title,section,score,level}`/
`blocks[]`（type=image 项含 url/oss_key/caption）。UI 偶发异常时先 API 直连复测定性。

## 产物归档惯例

- 批量采集 json → `scratch/`（命名带日期，如 `prod_retest_R2_20260611.json`）
- 结论报告 → `eval_harness/reports/*_findings.md` 或 `*_20260611.md`
- 探针脚本一次性变体 → `scratch/`；可复用的沉淀回本 skill `scripts/`

## 关键既有题集与基线

- 25 题图文题集（含期望文档/要点/期图标志）：`scratch/local_e2e_answers.json`
- 251 题金集：`eval_harness/goldset/golden_full.json`（`expected_docs` 为标题字符串，
  匹配须先归一化——见 pitfalls §标题匹配假阴）
- 历史轮次答案：`scratch/prod_retest_*_2026*.json`
