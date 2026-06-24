// 文档详情 — 版本历史 + 当前版本索引计数（GET /api/kb/version-history、/api/kb/doc-status）。
// 授权：kb_admin 或文档 owner_dept 在调用者 managed 内（后端裁决，403→forbidden）。

import { ensureLogin } from '../../utils/auth';
import { getVersionHistory, getDocStatus } from '../../utils/api';

const BADGE_CLASS = {
  已上线: 'ok', 处理中: 'busy', 排队中: 'queue', 待审核: 'warn',
  处理失败: 'fail', 已退役: 'muted', 内容未变: 'muted',
};
const CPS_LABEL = {
  NOT_STARTED: '待处理', LOADING: '处理中', PROCESSING: '处理中', DONE: '已处理',
  FAILED: '失败', SKIPPED_DUPLICATE: '内容未变', PENDING_APPROVAL: '待审核', REJECTED: '已驳回',
};
const IX_LABEL = {
  NOT_INDEXED: '未上线', PROCESSING: '上线中', INDEXED: '已上线', FAILED: '上线失败', DELETED: '已下线',
};

function decorate(v) {
  return {
    versionNo: v.version_no,
    badge: v.status_badge || '',
    badgeClass: BADGE_CLASS[v.status_badge] || 'muted',
    cps: CPS_LABEL[v.content_process_status] || v.content_process_status || '',
    ix: IX_LABEL[v.index_status] || v.index_status || '',
    err: v.error_message || '',
    createdAt: (v.created_at || '').slice(0, 16),
  };
}

Page({
  data: {
    title: '',
    ownerDept: '',
    docId: '',
    versions: [],
    chunkLine: '',
    loading: true,
    errorKind: '',
  },

  onLoad(q) {
    this._docId = (q && q.doc_id) || '';
    this.setData({ title: decodeURIComponent((q && q.title) || ''), docId: this._docId });
    this._reload();
  },

  onShow() {
    if (this._loadedOnce) {
      this._reload();
    }
  },

  _reload() {
    if (!this._docId) {
      this.setData({ loading: false, errorKind: 'error' });
      return;
    }
    this.setData({ loading: true, errorKind: '' });
    ensureLogin()
      .then(() => getVersionHistory(this._docId))
      .then((resp) => {
        this._loadedOnce = true;
        this.setData({
          loading: false,
          ownerDept: (resp && resp.owner_dept) || '',
          versions: ((resp && resp.versions) || []).map(decorate),
        });
        // 当前（最新）版本的 chunk 计数 —— best-effort，失败不影响版本历史展示
        getDocStatus(this._docId)
          .then((st) => {
            if (st) {
              this.setData({
                chunkLine: '本版索引：' + (st.chunk_indexed || 0) + ' / ' + (st.chunk_total || 0) +
                  ' 段（活跃 ' + (st.chunk_active || 0) + '）',
              });
            }
          })
          .catch(() => {});
      })
      .catch((err) => {
        this._loadedOnce = true;
        const status = err && err.status;
        const kind = status === 401 ? 'login'
          : status === 403 ? 'forbidden'
            : status === 404 ? 'notfound' : 'error';
        this.setData({ loading: false, errorKind: kind });
      });
  },

  onRetry() {
    this._reload();
  },

  onUploadVersion() {
    // 升版走 web-view 上传页（小程序容器选不了 office 文档）。带 doc_id → /console 自动进升版态，
    // 归属/可见范围继承原文档。⚠️ 真机生效仍需业务域名上线（与新建上传同一前置）。
    if (!this._docId) {
      return;
    }
    dd.navigateTo({
      url: '/pages/kb-upload/kb-upload?doc_id=' + encodeURIComponent(this._docId) +
        '&title=' + encodeURIComponent(this.data.title || '') +
        '&owner=' + encodeURIComponent(this.data.ownerDept || ''),
    });
  },
});
