// Single source of truth for the backend API host.
//
// ── 本地联调（IDE 模拟器）──
// 1. 本机起后端：RAG_ENV=test RAG_RERANK_ENABLE=true uvicorn opensearch_pipeline.api:app --port 8000
// 2. 钉钉 IDE → 详情/设置 勾选「不校验安全域名」
// 3. 把下面 DEV 置 true（提交/构建前改回 false）
const DEV = false;
const DEV_BASE_URL = 'http://127.0.0.1:8000';

// ── 生产 ──
// TODO(developer): SSL 重做完成后替换为真实 HTTPS 域名。
// 该域名（协议+主机）必须同时配进钉钉开发者后台
// 应用能力 → 安全设置 → 服务器安全域名（仅接受 https://），
// 否则 dd.httpRequest 运行时直接被拦截。改安全域名后需重新打包上传小程序。
const PROD_BASE_URL = 'https://YOUR_API_HOST';

export const BASE_URL = DEV ? DEV_BASE_URL : PROD_BASE_URL;

// Default network timeout (ms). RAG answers can be slow (LLM + retrieval),
// so keep this generous.
export const REQUEST_TIMEOUT = 30000;
