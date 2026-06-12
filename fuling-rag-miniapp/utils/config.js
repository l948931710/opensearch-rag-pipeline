// Single source of truth for the backend API host.
//
// ── 本地联调（IDE 模拟器）──
// 1. 本机起后端：RAG_ENV=test RAG_RERANK_ENABLE=true uvicorn opensearch_pipeline.api:app --port 8000
// 2. 钉钉 IDE → 详情/设置 勾选「不校验安全域名」
// 3. 把下面 DEV 置 true（提交/构建前改回 false）
const DEV = false;
const DEV_BASE_URL = 'http://10.0.0.87:8000'; // 电脑局域网 IP（手机需同 WiFi）

// ── 生产 ──
// 当前 = SAE 弹性公网 IP（HTTP 明文，测试期折衷；地址须在钉钉后台
// 安全设置 → HTTP 可信域名 白名单内，实测该字段收 IP）。
// TODO(developer): 接入备案完成后切 https://rag.fulingplastics.com.cn
// 并同步更新 HTTP 可信域名，发新版本即可无感切换。
const PROD_BASE_URL = 'http://120.55.69.9:8000';

export const BASE_URL = DEV ? DEV_BASE_URL : PROD_BASE_URL;

// Default network timeout (ms). RAG answers can be slow (LLM + retrieval),
// so keep this generous.
export const REQUEST_TIMEOUT = 30000;
