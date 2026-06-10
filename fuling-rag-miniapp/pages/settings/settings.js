// Settings / 我的: read-only profile + 清除会话.

import { ensureLogin } from '../../utils/auth';
import { clearSession } from '../../utils/api';

const APP_VERSION = '0.1.0';

Page({
  data: {
    displayName: '',
    dept: '',
    userId: '',
    sessionId: '',
    avatarChar: '富',
    version: APP_VERSION,
  },

  onShow() {
    this._refreshFromGlobal();
  },

  onLoad() {
    // If the user lands here first (cold launch into the settings tab),
    // trigger login so the profile is populated.
    if (!getApp().globalData.token) {
      ensureLogin()
        .then(() => this._refreshFromGlobal())
        .catch(() => {});
    } else {
      this._refreshFromGlobal();
    }
  },

  _refreshFromGlobal() {
    const g = getApp().globalData;
    const name = g.displayName || '';
    this.setData({
      displayName: name,
      dept: g.dept || '',
      userId: g.userId || '',
      sessionId: g.userId ? 'miniapp:' + g.userId : '',
      avatarChar: name ? name.charAt(0) : '富',
    });
  },

  onClearSession() {
    dd.confirm({
      title: '清除会话',
      content: '确定要开始新的会话吗？历史问答记录不受影响。',
      confirmButtonText: '清除',
      cancelButtonText: '取消',
      success: (res) => {
        if (res.confirm) {
          // 1. Local marker first — the chat page reads it on next onShow and
          //    resets its UI; this must work even when offline.
          dd.setStorageSync({ key: 'session_reset_at', data: Date.now() });

          // 2. Best-effort backend clear: without it the server keeps the old
          //    turns for the 30-min TTL and the "new" conversation inherits
          //    stale context. Failure degrades to local-only with a hint.
          const done = (ok) => {
            const content = ok
              ? '已清除会话'
              : '已在本地清除（服务器同步失败，旧上下文 30 分钟后自动过期）';
            const sb = this.selectComponent('#snackbar');
            if (sb && typeof sb.show === 'function') {
              sb.show({ content, type: ok ? 'success' : 'warning', duration: 2500 });
            } else {
              dd.showToast({ type: ok ? 'success' : 'none', content, duration: 2500 });
            }
          };
          const sid = this.data.sessionId;
          if (sid) {
            ensureLogin()
              .then(() => clearSession(sid))
              .then(() => done(true))
              .catch(() => done(false));
          } else {
            done(true);
          }
        }
      },
    });
  },
});
