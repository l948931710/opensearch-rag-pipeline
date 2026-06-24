// 知识库管理 — 我可管理的文档列表（GET /api/kb/my-docs，强制鉴权 + 角色现查）。
// kb_admin 看全部；dept_admin 仅看其 managed owner_dept。点按进详情（版本历史 + 状态）。

import { ensureLogin } from '../../utils/auth';
import { getMyDocs } from '../../utils/api';

// 状态徽章 → 颜色类（与后端 _kb_status_badge 文案对齐）
const BADGE_CLASS = {
  已上线: 'ok',
  处理中: 'busy',
  排队中: 'queue',
  待审核: 'warn',
  处理失败: 'fail',
  已退役: 'muted',
  内容未变: 'muted',
};

const PERM_LABEL = { public: '公开', dept_internal: '部门内', restricted: '受限' };

function decorate(raw) {
  return {
    docId: raw.doc_id,
    title: raw.title || raw.original_filename || raw.doc_id,
    ownerDept: raw.owner_dept || '',
    permLabel: PERM_LABEL[raw.permission_level] || raw.permission_level || '',
    versionNo: raw.current_version_no || 1,
    badge: raw.status_badge || '',
    badgeClass: BADGE_CLASS[raw.status_badge] || 'muted',
    updatedAt: (raw.updated_at || '').slice(0, 16),
  };
}

Page({
  data: {
    items: [],
    loading: true,
    loadingMore: false,
    hasMore: false,
    // '' 正常 / 'login' 未登录 / 'forbidden' 非管理员 / 'error' 加载失败
    errorKind: '',
  },

  onLoad() {
    this._loadedOnce = false;
    this._reload();
  },

  onShow() {
    // 从详情/上传返回时刷新（升版、审批会改状态）；首次 onLoad 已加载，避免重复。
    if (this._loadedOnce) {
      this._reload();
    }
  },

  _reload() {
    this.setData({ loading: true, errorKind: '', items: [] });
    ensureLogin()
      .then(() => getMyDocs(0))
      .then((resp) => {
        this._loadedOnce = true;
        this.setData({
          loading: false,
          items: ((resp && resp.items) || []).map(decorate),
          hasMore: !!(resp && resp.has_more),
        });
      })
      .catch((err) => {
        this._loadedOnce = true;
        const status = err && err.status;
        const kind = status === 401 ? 'login' : status === 403 ? 'forbidden' : 'error';
        this.setData({ loading: false, errorKind: kind });
      });
  },

  onRetry() {
    this._reload();
  },

  onLoadMore() {
    if (this.data.loadingMore || !this.data.hasMore) {
      return;
    }
    this.setData({ loadingMore: true });
    getMyDocs(this.data.items.length)
      .then((resp) => {
        this.setData({
          loadingMore: false,
          items: this.data.items.concat(((resp && resp.items) || []).map(decorate)),
          hasMore: !!(resp && resp.has_more),
        });
      })
      .catch(() => {
        this.setData({ loadingMore: false });
        dd.showToast({ type: 'none', content: '加载失败，请稍后重试', duration: 2000 });
      });
  },

  onOpenDoc(e) {
    const ds = e.currentTarget.dataset;
    if (ds.docId) {
      dd.navigateTo({
        url: '/pages/kb-doc-detail/kb-doc-detail?doc_id=' + encodeURIComponent(ds.docId) +
          '&title=' + encodeURIComponent(ds.title || ''),
      });
    }
  },

  onUpload() {
    // 进 web-view 上传页（小程序容器选不了 office 文档 → web-view 浏览器上下文 input[type=file]）。
    // ⚠️ web-view 域名须登记为「业务域名」(HTTPS)；裸 IP HTTP 仅 IDE 关闭校验可测，
    //    线上等 rag.fulingplastics.com.cn 备案+证书+业务域名登记后生效。
    dd.navigateTo({ url: '/pages/kb-upload/kb-upload' });
  },
});
