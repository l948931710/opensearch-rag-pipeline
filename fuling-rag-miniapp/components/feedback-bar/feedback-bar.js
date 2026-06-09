// feedback-bar: 👍 / 👎 / 转人工 under each AI answer.
// All state is local component data — no DingTalk card callbacks involved
// (this is a mini-program, not an interactive card).

import { feedback as postFeedback } from '../../utils/api';

Component({
  props: {
    messageId: '',
  },

  data: {
    // Preset downvote reasons. `code` is sent as feedback_reason.
    reasons: [
      { code: 'inaccurate', label: '内容不准确' },
      { code: 'irrelevant', label: '答非所问' },
      { code: 'wrong_image', label: '图片不对' },
      { code: 'outdated', label: '信息过时' },
    ],
    picked: '', // '', 'upvote', 'downvote', 'handoff'
    panelOpen: false,
    reasonCode: '',
    comment: '',
    submitted: false,
    thanksText: '',
  },

  methods: {
    _guard() {
      if (this.data.submitted) {
        return false;
      }
      if (!this.props.messageId) {
        dd.showToast({ type: 'fail', content: '消息尚未就绪，请稍候', duration: 2000 });
        return false;
      }
      return true;
    },

    _send(payload, thanks) {
      return postFeedback(Object.assign({ message_id: this.props.messageId }, payload))
        .then(() => {
          this.setData({ submitted: true, thanksText: thanks });
        })
        .catch((err) => {
          console.error('[feedback-bar.send]', err);
          dd.showToast({ type: 'fail', content: '提交失败，请重试', duration: 2000 });
          throw err;
        });
    },

    onUpvote() {
      if (!this._guard()) {
        return;
      }
      this.setData({ picked: 'upvote' });
      this._send({ feedback_type: 'upvote' }, '感谢反馈 👍').catch(() => {
        this.setData({ picked: '' });
      });
    },

    // 👎 opens the inline reason panel; submission happens on 提交.
    onDownvote() {
      if (!this._guard()) {
        return;
      }
      this.setData({ panelOpen: !this.data.panelOpen, picked: 'downvote' });
    },

    onPickReason(e) {
      const code = e.target.dataset.code;
      // Tap again to deselect.
      this.setData({ reasonCode: this.data.reasonCode === code ? '' : code });
    },

    onCommentInput(e) {
      this.setData({ comment: e.detail.value });
    },

    onCancelPanel() {
      this.setData({ panelOpen: false, picked: '', reasonCode: '', comment: '' });
    },

    onSubmitDownvote() {
      if (!this._guard()) {
        return;
      }
      const { reasonCode, comment } = this.data;
      if (!reasonCode && !comment) {
        dd.showToast({ type: 'fail', content: '请选择原因或填写说明', duration: 2000 });
        return;
      }
      const payload = { feedback_type: 'downvote' };
      if (reasonCode) {
        payload.feedback_reason = reasonCode;
      }
      if (comment) {
        payload.feedback_comment = comment;
      }
      this._send(payload, '已收到，我们会改进 🙏').then(() => {
        this.setData({ panelOpen: false });
      });
    },

    onHandoff() {
      if (!this._guard()) {
        return;
      }
      this.setData({ picked: 'handoff' });
      this._send({ feedback_type: 'handoff' }, '已为你转接人工客服')
        .then(() => {
          dd.showToast({ type: 'success', content: '已转人工，请留意钉钉消息', duration: 2500 });
        })
        .catch(() => {
          this.setData({ picked: '' });
        });
    },
  },
});
