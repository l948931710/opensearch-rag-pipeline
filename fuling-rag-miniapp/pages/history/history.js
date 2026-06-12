// 历史问答：本人 qa_session_log 列表（GET /api/history，强制鉴权）。
// 列表项点按展开答案 —— blocks 服务端已重签图片 URL，answer-bubble instant 直出。

import { ensureLogin } from '../../utils/auth';
import { getHistory } from '../../utils/api';

const PAGE_SIZE = 20;

// "2026-06-12 09:46:23" -> "06-12 09:46"（解析失败原样截断兜底）
function dateLabel(s) {
  const m = /^\d{4}-(\d{2}-\d{2})[ T](\d{2}:\d{2})/.exec(s || '');
  return m ? m[1] + ' ' + m[2] : (s || '').slice(0, 16);
}

const STATUS_LABEL = {
  NO_RESULT: '未找到',
  LLM_ERROR: '失败',
};

function decorate(raw) {
  const blocks = (raw.blocks && raw.blocks.length)
    ? raw.blocks
    : (raw.answer ? [{ type: 'text', format: 'plain', text: raw.answer }] : []);
  return {
    messageId: raw.message_id,
    question: raw.question,
    blocks,
    dateLabel: dateLabel(raw.created_at),
    statusLabel: STATUS_LABEL[raw.status] || '',
    expanded: false,
  };
}

Page({
  data: {
    items: [],
    loading: true,
    loadingMore: false,
    hasMore: false,
    // '' = 正常；'login' = 未登录；'error' = 加载失败
    errorKind: '',
  },

  onLoad() {
    this._reload();
  },

  _reload() {
    this.setData({ loading: true, errorKind: '', items: [] });
    ensureLogin()
      .then(() => getHistory(0))
      .then((resp) => {
        this.setData({
          loading: false,
          items: ((resp && resp.items) || []).map(decorate),
          hasMore: !!(resp && resp.has_more),
        });
      })
      .catch((err) => {
        // 401（含重登失败）按未登录展示；其它按网络/服务异常
        const kind = err && err.status === 401 ? 'login' : 'error';
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
    getHistory(this.data.items.length)
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

  onToggle(e) {
    const idx = Number(e.currentTarget.dataset.idx);
    const item = this.data.items[idx];
    if (!item) {
      return;
    }
    this.setData({ ['items[' + idx + '].expanded']: !item.expanded });
  },
});
