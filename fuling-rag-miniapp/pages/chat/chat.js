// Chat page: the core Q&A screen.

import { ensureLogin } from '../../utils/auth';
import { ask } from '../../utils/api';

let msgSeq = 0;
function nextId() {
  msgSeq += 1;
  return 'm' + msgSeq + '_' + Date.now();
}

Page({
  data: {
    messages: [],      // { id, role:'user'|'ai', text?, blocks?, messageId?, loading?, error?, errorText?, instant? }
    draft: '',
    sending: false,
    scrollIntoId: '',  // id used by scroll-into-view
    sessionId: '',
    lastResetAt: 0, // tracks the 清除会话 marker from the settings page
  },

  onLoad() {
    ensureLogin()
      .then((g) => {
        // Stable per-user session id for the conversation memory on the backend.
        if (g.userId) {
          this.setData({ sessionId: 'miniapp:' + g.userId });
        }
      })
      .catch(() => {
        // ensureLogin already toasted; leave the page usable so a retry on send works.
      });
  },

  onShow() {
    // Honor 清除会话 from the settings page: if the marker advanced, wipe the
    // visible conversation and let the backend memory start fresh.
    let resetAt = 0;
    try {
      const r = dd.getStorageSync({ key: 'session_reset_at' });
      resetAt = (r && r.data) || 0;
    } catch (e) {
      resetAt = 0;
    }
    if (resetAt && resetAt !== this.data.lastResetAt) {
      this.setData({
        messages: [],
        draft: '',
        sending: false,
        scrollIntoId: '',
        lastResetAt: resetAt,
      });
    }
  },

  onInput(e) {
    this.setData({ draft: e.detail.value });
  },

  _scrollToBottom() {
    const msgs = this.data.messages;
    if (!msgs.length) {
      return;
    }
    const lastId = msgs[msgs.length - 1].id;
    // Toggle to force scroll-into-view to re-fire even for the same target.
    this.setData({ scrollIntoId: '' });
    setTimeout(() => {
      this.setData({ scrollIntoId: 'msg-' + lastId });
    }, 30);
  },

  // Called by <answer-bubble onGrow> as the typewriter reveals more text.
  onAnswerGrow() {
    this._scrollToBottom();
  },

  _toast(content, type) {
    const sb = this.selectComponent('#snackbar');
    if (sb && typeof sb.show === 'function') {
      sb.show({ content, type: type || 'info', duration: 2500 });
    } else {
      dd.showToast({ content, type: type === 'error' ? 'fail' : 'none', duration: 2500 });
    }
  },

  _updateMessage(id, patch) {
    const messages = this.data.messages.map((m) =>
      m.id === id ? Object.assign({}, m, patch) : m
    );
    this.setData({ messages });
  },

  onSend() {
    const question = (this.data.draft || '').trim();
    if (!question || this.data.sending) {
      return;
    }

    const userMsg = { id: nextId(), role: 'user', text: question };
    const aiId = nextId();
    const aiMsg = { id: aiId, role: 'ai', loading: true };

    this.setData({
      messages: this.data.messages.concat([userMsg, aiMsg]),
      draft: '',
      sending: true,
    });
    this._scrollToBottom();

    // ensureLogin() is idempotent and cached; it guarantees a token before ask().
    ensureLogin()
      .then(() => ask(question, this.data.sessionId))
      .then((resp) => {
        let blocks = (resp && resp.blocks) || [];
        // 纯文字答案/未引用图片时后端约定返回 blocks=[]，需降级渲染 answer 文本，
        // 否则气泡为空白（NO_RESULT 道歉语与 RAG_PURE_TEXT 模式下 100% 触发）。
        if (!blocks.length && resp && resp.answer) {
          blocks = [{ type: 'text', format: 'plain', text: resp.answer }];
        }
        this._updateMessage(aiId, {
          loading: false,
          error: false,
          blocks,
          messageId: resp && resp.message_id,
          instant: false,
        });
        // Adopt a server-issued session id if we did not have one yet.
        if (resp && resp.session_id && !this.data.sessionId) {
          this.setData({ sessionId: resp.session_id });
        }
        this.setData({ sending: false });
        this._scrollToBottom();
      })
      .catch((err) => {
        console.error('[chat.onSend]', err);
        this._updateMessage(aiId, {
          loading: false,
          error: true,
          errorText: '回答失败，请稍后重试',
        });
        this.setData({ sending: false });
        this._toast('回答失败，请稍后重试', 'error');
        this._scrollToBottom();
      });
  },
});
