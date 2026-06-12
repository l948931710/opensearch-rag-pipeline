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
 * opts.thinking: 深度思考（默认关闭，逐问生效）。思考显著更慢（30-90s+），
 * 默认 30s 超时必先爆 —— 该请求的超时放宽到 120s。
 * opts.onTask:   接收 RequestTask（骨架阶段「停止」按它 abort 真取消）。
 */
export function ask(question, sessionId, opts) {
  const thinking = !!(opts && opts.thinking);
  const data = { question, session_id: sessionId };
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
