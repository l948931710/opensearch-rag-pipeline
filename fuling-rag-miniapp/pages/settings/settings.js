// Settings / 我的: read-only profile + 清空会话列表.

import { ensureLogin } from '../../utils/auth';
import { clearSession, deleteConversation } from '../../utils/api';
import { loadIndex, clearAllLocal } from '../../utils/conversations';

const APP_VERSION = '0.3.0';

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
    convCount: 0,
    avatarChar: '富',
    version: APP_VERSION,
    canManageKb: false,
    roleLabel: '',
    roleClass: '',   // kb（实心深绿）/ dept（描边绿）—— 与 kb-docs 工作台同一套徽章
    kbScopeLabel: '',
    kbTip: '',
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
    // 管辖范围标注：与 kb-docs 工作台角色横幅同词表（kb_admin=全库 / dept_admin=本部门）
    const KB_SCOPE = { kb_admin: '全库', dept_admin: '本部门' };
    const KB_TIP = {
      kb_admin: '全库文档管理：上传 / 升版 / 待审批放行。',
      dept_admin: '本部门文档管理：上传 / 升版；新文档由知识库管理员审批上线。',
    };
    this.setData({
      displayName: name,
      deptLabel: friendlyDept(g.dept, g.role),
      userId: g.userId || '',
      convCount: loadIndex().length,
      avatarChar: name ? name.charAt(0) : '富',
      canManageKb: !!g.canManageKb,
      roleLabel: ROLE_LABEL[g.role] || '',
      roleClass: g.role === 'kb_admin' ? 'kb' : (g.role === 'dept_admin' ? 'dept' : ''),
      kbScopeLabel: KB_SCOPE[g.role] || '',
      kbTip: KB_TIP[g.role] || '',
    });
  },

  onOpenHistory() {
    dd.navigateTo({ url: '/pages/history/history' });
  },

  onOpenKbConsole() {
    dd.navigateTo({ url: '/pages/kb-docs/kb-docs' });
  },

  onClearConversations() {
    dd.confirm({
      title: '清空会话列表',
      content: '将删除本机全部会话，并同步隐藏云端会话列表；历史问答记录不受影响。',
      confirmButtonText: '清空',
      cancelButtonText: '取消',
      success: (res) => {
        if (!res.confirm) {
          return;
        }
        // 1. 本地为准：清空索引+消息缓存，落 marker（问答页 onShow 读到即重置 UI）。
        //    离线也必须成立。
        const cleared = clearAllLocal();
        dd.setStorageSync({ key: 'session_reset_at', data: Date.now() });
        this.setData({ convCount: 0 });
        dd.showToast({ type: 'success', content: '已清空会话', duration: 2000 });

        // 2. Best-effort 云端收尾：软隐藏会话列表（flag 关闭时 404）+ 清各会话
        //    LLM 记忆（否则旧上下文陪聊到 30 分钟 TTL）。失败静默 —— 本地已清。
        ensureLogin()
          .then(() => {
            cleared.ids.forEach((id) => {
              deleteConversation(id).catch(() => {});
            });
            cleared.sessionIds.forEach((sid) => {
              clearSession(sid).catch(() => {});
            });
          })
          .catch(() => {});
      },
    });
  },
});
