// Thin wrapper around dd.httpRequest with a token-expiry interceptor.
//
// DingTalk (Alipay engine) specifics:
//   - field name is 'headers' (NOT 'header')
//   - HTTP status is res.status (NOT res.statusCode)
//   - response body is res.data; with dataType:'json' it is already parsed
//   - dd.httpRequest is BUFFERED ONLY — there is no streaming support, so the
//     backend /api/ask (non-stream) endpoint is the one we call.

import { BASE_URL, REQUEST_TIMEOUT } from './config';
import { ensureLogin, getToken } from './auth';

/**
 * Low-level request. Returns a Promise that resolves with the parsed body
 * (res.data) or rejects with an Error carrying { status, data }.
 *
 * @param {string} path           e.g. '/api/ask'
 * @param {object} opts
 * @param {string} [opts.method]  default 'GET'
 * @param {object} [opts.data]    JS object; JSON.stringify'd for non-GET
 * @param {boolean} [opts.auth]   attach Authorization: Bearer <token>
 * @param {boolean} [opts._retried] internal: prevents infinite 401 loops
 */
export function request(path, opts) {
  const options = opts || {};
  const method = (options.method || 'GET').toUpperCase();
  const auth = !!options.auth;

  const headers = { 'Content-Type': 'application/json' };
  if (auth) {
    const token = getToken();
    if (token) {
      headers.Authorization = 'Bearer ' + token;
    }
  }

  // GET sends no body; everything else is JSON-encoded.
  let body;
  if (method !== 'GET' && options.data !== undefined) {
    body = JSON.stringify(options.data);
  }

  return new Promise((resolve, reject) => {
    const task = dd.httpRequest({
      url: BASE_URL + path,
      method,
      headers,
      data: body,
      dataType: 'json',
      timeout: options.timeout || REQUEST_TIMEOUT,
      success(res) {
        const status = res.status;

        // Token-expiry interceptor: on 401, transparently re-login ONCE then retry.
        if (status === 401 && auth && !options._retried) {
          ensureLogin({ force: true })
            .then(() => request(path, Object.assign({}, options, { _retried: true })))
            .then(resolve)
            .catch(reject);
          return;
        }

        if (status >= 200 && status < 300) {
          resolve(res.data);
        } else {
          const err = new Error('请求失败 (HTTP ' + status + ')');
          err.status = status;
          err.data = res.data;
          reject(err);
        }
      },
      fail(err) {
        const e = new Error((err && err.errorMessage) || '网络请求失败');
        e.raw = err;
        reject(e);
      },
    });
    // 把 RequestTask 交给调用方（task.abort() 真取消请求；老基础库可能返回
    // undefined —— 调用方需判空降级）。abort 后走 fail 回调 reject。
    if (typeof options.onTask === 'function') {
      options.onTask(task);
    }
  });
}

/**
 * ask(question, sessionId, opts) -> Promise<answer payload>
 * Calls POST /api/ask with the bearer token.
 *
 * opts.thinking:       深度思考（默认关闭，逐问生效）。思考显著更慢（30-90s+），
 *                      默认 30s 超时必先爆 —— 该请求的超时放宽到 120s。
 * opts.conversationId: 客户端会话 ID（多会话线程归属）。服务端
 *                      RAG_CONVERSATION_HISTORY 开启时按它落 qa_conversation，
 *                      关闭时忽略 —— 无条件上送，向后兼容。
 * opts.onTask:         接收 RequestTask（骨架阶段「停止」按它 abort 真取消）。
 *
 * sessionId 为空时不上送：服务端会新建 UUID 会话并在响应里返回 session_id，
 * 调用方应把它记回当前会话（每个会话独立的 LLM 多轮记忆）。
 */
export function ask(question, sessionId, opts) {
  const thinking = !!(opts && opts.thinking);
  const data = { question };
  if (sessionId) {
    data.session_id = sessionId;
  }
  if (opts && opts.conversationId) {
    data.conversation_id = opts.conversationId;
  }
  if (thinking) {
    data.thinking = true;
  }
  return request('/api/ask', {
    method: 'POST',
    auth: true,
    data,
    timeout: thinking ? 120000 : undefined,
    onTask: opts && opts.onTask,
  });
}

// ── 多会话（与控制台同一组端点；RAG_CONVERSATION_HISTORY 关闭时列表恒空/404，
//    前端以本地存储为准、服务端数据仅做合并增益）──────────────────────────

/** getConversations() -> Promise<{items:[{conversation_id,title,updated_at}], has_more}> */
export function getConversations() {
  return request('/api/conversations?limit=50', { auth: true });
}

/**
 * getConversationMessages(conversationId) -> Promise<{items, has_more}>
 * items 与 /api/history 同构（question/answer/blocks/created_at/status），
 * 图片 URL 服务端已重签。
 */
export function getConversationMessages(conversationId) {
  return request('/api/conversations/' + encodeURIComponent(conversationId), { auth: true });
}

/** deleteConversation(conversationId) —— 服务端软隐藏；flag 关闭时 404（调用方按成功清理本地）。 */
export function deleteConversation(conversationId) {
  return request('/api/conversations/' + encodeURIComponent(conversationId), {
    method: 'DELETE',
    auth: true,
  });
}

/**
 * feedback(payload) -> Promise<{status, message_id}>
 * payload: { message_id, feedback_type, feedback_reason?, feedback_comment? }
 */
export function feedback(payload) {
  return request('/api/feedback', {
    method: 'POST',
    auth: true,
    data: payload,
  });
}

/**
 * clearSession(sessionId) -> Promise<{status, cleared}>
 * Tells the backend to drop the conversation memory for this session.
 * 'miniapp:<userId>' ids are ownership-checked server-side, so auth is required.
 */
export function clearSession(sessionId) {
  return request('/api/session/clear', {
    method: 'POST',
    auth: true,
    data: { session_id: sessionId },
  });
}

/**
 * resignImages(ossKeys) -> Promise<{urls: {oss_key: freshUrl}}>
 * 过期图片重签：blocks[].oss_key 换新签名 URL（空串 = 该 key 被拒/失败）。
 */
export function resignImages(ossKeys) {
  return request('/api/resign-images', {
    method: 'POST',
    data: { oss_keys: ossKeys },
  });
}

/**
 * getHistory(offset) -> Promise<{items, has_more}>
 * 历史问答（仅本人，强制鉴权；401 拦截器会自动重登一次）。
 */
export function getHistory(offset) {
  return request('/api/history?limit=20&offset=' + (offset || 0), {
    auth: true,
  });
}

/**
 * getHotQuestions() -> Promise<{questions: string[]}>
 * 「猜你想问」快捷栏（服务端近 30 天高频问题；失败时调用方用静态兜底）。
 */
export function getHotQuestions() {
  return request('/api/hot-questions', {});
}

// ── 知识库管理（部门管理员）──────────────────────────────────────
// 全部强制鉴权；后端按 token + DB 现查角色裁决（前端入口只是便利）。

/** 权限选择器数据：10 组 + 部门→组映射 + 本人可管理/可授权范围 + org 快照。 */
export function getOrgTree() {
  return request('/api/kb/org-tree', { auth: true });
}

/** 我（kb_admin 全量 / dept_admin 限 managed）可管理的文档列表；q=文档名搜索（可空）。 */
export function getMyDocs(offset, q) {
  let p = '/api/kb/my-docs?limit=20&offset=' + (offset || 0);
  if (q) {
    p += '&q=' + encodeURIComponent(q);
  }
  return request(p, { auth: true });
}

/** 某文档的版本历史（各版本管线状态）。 */
export function getVersionHistory(docId) {
  return request('/api/kb/version-history?doc_id=' + encodeURIComponent(docId), { auth: true });
}

/** 某文档某版本的详细管线状态 + chunk 计数（version 省略取当前版本）。 */
export function getDocStatus(docId, version) {
  let p = '/api/kb/doc-status?doc_id=' + encodeURIComponent(docId);
  if (version) {
    p += '&version=' + encodeURIComponent(version);
  }
  return request(p, { auth: true });
}

/**
 * createUploadUrl(payload) -> Promise<{upload_token, put_url, raw_key, doc_id, expires_in, requires_kb_admin_approval}>
 * payload: { action:'new'|'version', filename, owner_dept, permission_level, title?, category_l1?, category_l2?, doc_id?, share_owner_depts? }
 */
export function createUploadUrl(payload) {
  return request('/api/kb/upload-url', { method: 'POST', auth: true, data: payload });
}

/** registerDoc(uploadToken) -> Promise<{doc_id, version_no, content_process_status, requires_kb_admin_approval, status_badge, idempotent}> */
export function registerDoc(uploadToken) {
  return request('/api/kb/register', { method: 'POST', auth: true, data: { upload_token: uploadToken } });
}

/** kb_admin 审批放行 / 驳回（payload: {doc_id, version_no?, reason?}）。 */
export function approveDoc(payload) {
  return request('/api/kb/approve', { method: 'POST', auth: true, data: payload });
}
export function rejectDoc(payload) {
  return request('/api/kb/reject', { method: 'POST', auth: true, data: payload });
}

/** 管理范围文档聚合（与 my-docs 同一 owner 作用域：dept_admin=本部门 / kb_admin=全库）。
 * 返回 {total, active, retired, chunks, new_this_month, by_badge}。 */
export function getKbStats() {
  return request('/api/kb/stats', { auth: true });
}

/** kb_admin 待审批队列（content_process_status='PENDING_APPROVAL' 的版本）；
 * 非 kb_admin 后端 403 —— 调用方按空列表处理。 */
export function getPendingApprovals() {
  return request('/api/kb/pending-approvals', { auth: true });
}
