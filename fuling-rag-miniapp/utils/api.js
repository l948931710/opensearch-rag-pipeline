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
    dd.httpRequest({
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
  });
}

/**
 * ask(question, sessionId) -> Promise<answer payload>
 * Calls POST /api/ask with the bearer token.
 */
export function ask(question, sessionId) {
  return request('/api/ask', {
    method: 'POST',
    auth: true,
    data: { question, session_id: sessionId },
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
