// feedback-bar: 👍 / 👎 / 复制 under each AI answer (v1.1 semantics).
//
// 反馈语义：赞/踩一次性互斥锁定（已选中的那一票保持饱和 —— 否则"已记录"看起来
// 像"坏了"）；复制独立可用。后端 user_feedback 按 (message_id, user_id) 覆盖更新。
// toast 文案与原型文案基准表逐字一致。（转人工已下线 2026-07：由知识贡献系统兜底。）

import { feedback as postFeedback } from '../../utils/api';

// 不满意原因（多选）：含两大真实失败模式（检索未命中 / 答案截断），
// 否则都被误归入"内容不准确"，污染反馈挖掘数据。code 拼接为 feedback_reason。
const REASONS = [
  { code: 'inaccurate', label: '内容不准确' },
  { code: 'irrelevant', label: '答非所问' },
  { code: 'incomplete', label: '答案不完整' },
  { code: 'not_found', label: '没找到我要的文档' },
  { code: 'wrong_image', label: '图片不对' },
  { code: 'outdated', label: '信息过时' },
];

Component({
  props: {
    messageId: '',
    // 复制回答用的纯文本（chat 页传 resp.answer，已剥 <<IMG:N>> 标记）
    copyText: '',
  },

  data: {
    reasons: REASONS.map((r) => ({ code: r.code, label: r.label, sel: false })),
    voted: '',        // '' | 'up' | 'down'
    voteLocked: false, // 赞踩已提交（不影响复制）
    panelOpen: false,
    hasReason: false,
    comment: '',
  },

  methods: {
    _ready() {
      if (!this.props.messageId) {
        dd.showToast({ type: 'none', content: '消息尚未就绪，请稍候', duration: 1700 });
        return false;
      }
      return true;
    },

    _toast(content) {
      dd.showToast({ content, type: 'none', duration: 1700 });
    },

    _send(payload) {
      return postFeedback(Object.assign({ message_id: this.props.messageId }, payload));
    },

    onUpvote() {
      if (this.data.voteLocked || !this._ready()) {
        return;
      }
      this.setData({ voted: 'up', voteLocked: true, panelOpen: false });
      this._send({ feedback_type: 'upvote' })
        .then(() => {
          this._toast('感谢反馈 👍');
        })
        .catch((err) => {
          console.error('[feedback-bar.upvote]', err);
          this.setData({ voted: '', voteLocked: false });
          this._toast('提交失败，请重试');
        });
    },

    // 👎 toggles the inline reason panel; submission happens on 提交.
    onDownvote() {
      if (this.data.voteLocked || !this._ready()) {
        return;
      }
      this.setData({ panelOpen: !this.data.panelOpen });
    },

    // 原因 chips 多选（再点取消选择）
    onPickReason(e) {
      const code = e.currentTarget.dataset.code;
      const reasons = this.data.reasons.map((r) =>
        r.code === code ? { code: r.code, label: r.label, sel: !r.sel } : r
      );
      this.setData({ reasons, hasReason: reasons.some((r) => r.sel) });
    },

    onCommentInput(e) {
      this.setData({ comment: e.detail.value });
    },

    onCancelPanel() {
      this.setData({ panelOpen: false });
    },

    onSubmitDownvote() {
      if (this.data.voteLocked || !this._ready()) {
        return;
      }
      const codes = this.data.reasons.filter((r) => r.sel).map((r) => r.code);
      const comment = (this.data.comment || '').trim();
      if (!codes.length && !comment) {
        this._toast('请选择原因或填写说明');
        return;
      }
      const payload = { feedback_type: 'downvote' };
      if (codes.length) {
        payload.feedback_reason = codes.join(',');
      }
      if (comment) {
        payload.feedback_comment = comment;
      }
      this.setData({ voted: 'down', voteLocked: true, panelOpen: false });
      this._send(payload)
        .then(() => {
          this._toast('感谢反馈，我们会持续改进');
        })
        .catch((err) => {
          console.error('[feedback-bar.downvote]', err);
          this.setData({ voted: '', voteLocked: false, panelOpen: true });
          this._toast('提交失败，请重试');
        });
    },

    // 复制回答：独立、可重复
    onCopy() {
      const text = this.props.copyText || '';
      if (!text) {
        return;
      }
      const self = this;
      dd.setClipboard({
        text,
        success() {
          self._toast('已复制回答');
        },
      });
    },
  },
});
