// Settings / 我的: read-only profile + 清除会话.

import { ensureLogin } from '../../utils/auth';

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
    // Sessions are keyed by user on the backend; clearing locally simply asks
    // the chat page to start fresh. We bump a marker the chat page can read,
    // and clear any in-app draft state via storage.
    dd.confirm({
      title: '清除会话',
      content: '确定要开始新的会话吗？历史问答记录不受影响。',
      confirmButtonText: '清除',
      cancelButtonText: '取消',
      success: (res) => {
        if (res.confirm) {
          // Signal a session reset; the chat page reads this on next onShow/onLoad.
          dd.setStorageSync({ key: 'session_reset_at', data: Date.now() });
          const sb = this.selectComponent('#snackbar');
          if (sb && typeof sb.show === 'function') {
            sb.show({ content: '已清除会话', type: 'success', duration: 2000 });
          } else {
            dd.showToast({ type: 'success', content: '已清除会话', duration: 2000 });
          }
        }
      },
    });
  },
});
