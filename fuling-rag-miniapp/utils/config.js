// Single source of truth for the backend API host.
//
// TODO(developer): replace with your real API host BEFORE building.
// This exact host (scheme + domain) MUST also be added to the DingTalk
// developer console under 应用能力 -> 安全设置 -> 服务器安全域名 (安全域名),
// otherwise dd.httpRequest will be blocked at runtime.
// Changing 安全域名 later requires re-package + re-upload of the mini-program.
export const BASE_URL = 'https://YOUR_API_HOST';

// Default network timeout (ms). RAG answers can be slow (LLM + retrieval),
// so keep this generous.
export const REQUEST_TIMEOUT = 30000;
