// Settings / 我的: read-only profile + 清除会话.

import { ensureLogin } from '../../utils/auth';
import { clearSession } from '../../utils/api';

const APP_VERSION = '0.2.0';

// 权限组代码 → 中文（"部门"栏友好显示，避免把 marketing,production… 原样铺给用户）
const GROUP_LABEL = {
  finance: '财务', it: '信息技术', marketing: '营销', production: '生产', pmc: '计划PMC',
  admin: '行政', hr: '人力资源', rd: '研发', quality: '品质技术', supply: '资材供应',
};

// dept（其实是 ACL 权限组 CSV）→ 友好文案。kb_admin/多组 → 「全部门可见」，否则中文组名顿号拼接。
function friendlyDept(deptCsv, role) {
  const codes = (deptCsv || '').split(',').map((s) => s.trim()).filter(Boolean);
  if (role === 'kb_admin' || codes.length >= 8) {
    return '全部门可见';
  }
  if (!codes.length) {
    return '部门未知';
  }
  return codes.map((c) => GROUP_LABEL[c] || c).join('、');
}

Page({
  data: {
    displayName: '',
    deptLabel: '',
    userId: '',
    sessionId: '',
    avatarChar: '富',
    version: APP_VERSION,
    canManageKb: false,
    roleLabel: '',
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
    const ROLE_LABEL = { kb_admin: '知识库管理员', dept_admin: '部门管理员' };
    this.setData({
      displayName: name,
      deptLabel: friendlyDept(g.dept, g.role),
      userId: g.userId || '',
      sessionId: g.userId ? 'miniapp:' + g.userId : '',
      avatarChar: name ? name.charAt(0) : '富',
      canManageKb: !!g.canManageKb,
      roleLabel: ROLE_LABEL[g.role] || '',
    });
  },

  onOpenHistory() {
    dd.navigateTo({ url: '/pages/history/history' });
  },

  onOpenKbConsole() {
    dd.navigateTo({ url: '/pages/kb-docs/kb-docs' });
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
            dd.showToast({ type: ok ? 'success' : 'none', content, duration: 2500 });
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
