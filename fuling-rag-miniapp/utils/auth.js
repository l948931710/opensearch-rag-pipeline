// 钉钉免登 (corp single-sign-on) helper.
//
// Flow:
//   1. dd.getAuthCode()  -> a 5-minute, single-use免登码 (authCode)
//   2. POST {BASE}/api/auth/dingtalk {auth_code}  -> {token, user_id, display_name, dept}
//   3. cache token + user into getApp().globalData
//
// NOTE: use dd.getAuthCode (mini-program API), NOT
//       dd.runtime.permission.requestAuthCode (that is the H5 / jsapi flavour).
//       免登 only works on a REAL DEVICE inside the DingTalk client — the IDE
//       simulator cannot mint a valid authCode.

import { BASE_URL, REQUEST_TIMEOUT } from './config';

// In-flight login promise, so concurrent callers share one round-trip.
let loginPromise = null;

function getAuthCode() {
  return new Promise((resolve, reject) => {
    dd.getAuthCode({
      success(res) {
        // res.authCode is the免登码.
        if (res && res.authCode) {
          resolve(res.authCode);
        } else {
          reject(new Error('未获取到免登码 (authCode 为空)'));
        }
      },
      fail(err) {
        reject(err || new Error('dd.getAuthCode 调用失败'));
      },
    });
  });
}

function exchangeToken(authCode) {
  return new Promise((resolve, reject) => {
    dd.httpRequest({
      url: BASE_URL + '/api/auth/dingtalk',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ auth_code: authCode }),
      dataType: 'json',
      timeout: REQUEST_TIMEOUT,
      success(res) {
        if (res.status >= 200 && res.status < 300 && res.data && res.data.token) {
          resolve(res.data);
        } else {
          reject(new Error('登录失败 (HTTP ' + res.status + ')'));
        }
      },
      fail(err) {
        reject(err || new Error('登录请求失败'));
      },
    });
  });
}

/**
 * ensureLogin({ force }) -> Promise<globalData>
 * Resolves once a valid token is present in globalData. Caches the result;
 * pass { force: true } to bypass the cache (used by the 401 interceptor).
 */
export function ensureLogin(opts) {
  const force = !!(opts && opts.force);
  const app = getApp();

  if (!force && app.globalData.token) {
    return Promise.resolve(app.globalData);
  }
  if (loginPromise) {
    return loginPromise;
  }

  loginPromise = getAuthCode()
    .then(exchangeToken)
    .then((data) => {
      app.globalData.token = data.token || '';
      app.globalData.userId = data.user_id || '';
      app.globalData.displayName = data.display_name || '';
      app.globalData.dept = data.dept || '';
      // 知识库写授权角色（入口可见性用；后端每个写接口仍会现查 DB 鉴权）
      app.globalData.role = data.role || 'employee';
      app.globalData.canManageKb = !!data.can_manage_kb;
      loginPromise = null;
      return app.globalData;
    })
    .catch((err) => {
      loginPromise = null;
      dd.showToast({
        type: 'fail',
        content: '登录失败，请在钉钉中重试',
        duration: 2500,
      });
      console.error('[auth.ensureLogin]', err);
      throw err;
    });

  return loginPromise;
}

/** Synchronous read of the cached token (may be empty before first login). */
export function getToken() {
  return getApp().globalData.token || '';
}

/** Clears the cached token (e.g. on explicit logout / session reset). */
export function clearToken() {
  const app = getApp();
  app.globalData.token = '';
}
