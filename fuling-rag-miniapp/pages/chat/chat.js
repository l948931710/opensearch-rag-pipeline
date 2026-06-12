// Chat page: the core Q&A screen (Aurora-Forest v1.1 port).
//
// /api/ask 契约消费：blocks[]（空时降级 answer）、no_result（空结果卡）、
// guard（低匹配提示条）、sources[].level（相关度高/中/低徽章 —— rerank 开启后
// score 是 0-1 量纲，禁止本地按融合阈值重算，仅在 level 缺省时兜底）。

import { ensureLogin } from '../../utils/auth';
import { ask, feedback as postFeedback, getHotQuestions } from '../../utils/api';

// 静态兜底（/api/hot-questions 不可达时显示；与服务端 _HOT_QUESTIONS_FALLBACK 同源）
const QUICK = ['U8+ 如何登录？', '请假流程是什么？', '访客 WiFi 密码是多少？', '注塑模具多久保养一次？'];

let msgSeq = 0;
function nextId() {
  msgSeq += 1;
  return 'm' + msgSeq + '_' + Date.now();
}

function pad2(n) {
  return n < 10 ? '0' + n : '' + n;
}

// level 以服务端下发为准；缺省按加权融合分 7.7/5.8 标定兜底（与原型同款）
function mapSources(sources) {
  return (sources || []).map((s, i) => {
    let lvl = s.level;
    if (lvl !== 'high' && lvl !== 'mid' && lvl !== 'low') {
      const sc = s.score || 0;
      lvl = sc >= 7.7 ? 'high' : (sc >= 5.8 ? 'mid' : 'low');
    }
    return {
      idx: i + 1,
      title: s.title || '',
      section: s.section || '',
      levelLabel: lvl === 'high' ? '高' : (lvl === 'mid' ? '中' : '低'),
      levelClass: lvl === 'high' ? '' : lvl,
    };
  });
}

Page({
  data: {
    // { id, role:'user'|'ai', text?, blocks?, guard?, sources?, sourcesOpen?,
    //   messageId?, copyText?, question?, loading?, error?, errorText?,
    //   noResult?, handoffDone?, instant? }
    messages: [],
    draft: '',        // 仅程序化写值（发送清空/chip 回填）；输入过程非受控，见 onInput
    hasDraft: false,  // 发送按钮可用态（空↔非空翻转才 setData）
    sending: false,   // 等待 /api/ask 响应（骨架屏阶段）
    typing: false,    // 打字机揭示中（按钮显示「停止」）
    thinking: false,  // 深度思考开关：默认关、不持久化（每次进页都是关），逐问生效
    serviceOk: true,  // 健康态仅由最近一次请求结果驱动，不做无依据承诺
    statusBarHeight: 0,
    startTimeLabel: '',
    quick: QUICK,
    scrollIntoId: '',
    showToBottom: false,
    sessionId: '',
    lastResetAt: 0, // tracks the 清除会话 marker from the settings page
  },

  onLoad() {
    let sbh = 0;
    try {
      sbh = dd.getSystemInfoSync().statusBarHeight || 0;
    } catch (e) {
      sbh = 0;
    }
    const now = new Date();
    this.setData({
      statusBarHeight: sbh,
      startTimeLabel: '今天 ' + pad2(now.getHours()) + ':' + pad2(now.getMinutes()),
    });
    this._draft = '';           // 非受控草稿（真值；data.draft 只做程序化覆盖）
    this._pinned = true;        // 钉住才跟滚：用户上翻重读时绝不抢滚动条
    this._listHeight = 0;
    this._lastGrowScroll = 0;
    this._skipOnArrive = false; // 骨架阶段点「停止」→ 响应到达后整段直出（abort 不可用时的保底）
    this._stageTimers = {};     // aiId → 骨架阶段文案定时器（检索→生成）
    this._reqTask = null;       // 当前在途请求的 RequestTask（abort 真取消）
    this._pendingAiId = '';     // 在途请求对应的 AI 消息 id
    this._abortedAiId = '';     // 用户主动取消的消息 id（catch 里区分取消与失败）

    ensureLogin()
      .then((g) => {
        if (g.userId) {
          this.setData({ sessionId: 'miniapp:' + g.userId });
        }
      })
      .catch(() => {
        // ensureLogin already toasted; leave the page usable so a retry on send works.
      });

    // 「猜你想问」：服务端近 30 天高频问题；失败保持静态兜底（fail open）
    getHotQuestions()
      .then((resp) => {
        const qs = (resp && resp.questions) || [];
        if (qs.length) {
          this.setData({ quick: qs });
        }
      })
      .catch(() => {});
  },

  onReady() {
    // 量取列表视口高，供钉住判定（onScroll 事件不带 clientHeight）
    try {
      dd.createSelectorQuery()
        .select('.msg-list')
        .boundingClientRect()
        .exec((res) => {
          if (res && res[0] && res[0].height) {
            this._listHeight = res[0].height;
          }
        });
    } catch (e) {
      // 量取失败则跟滚退化为始终跟随（_listHeight=0 时 onListScroll 不更新 pinned）
    }
  },

  onUnload() {
    // 清掉所有骨架阶段定时器，防止迟到回调在已卸载页面上 setData
    Object.keys(this._stageTimers || {}).forEach((k) => clearTimeout(this._stageTimers[k]));
    this._stageTimers = {};
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
      this._draft = '';
      this.setData({
        messages: [],
        draft: '',
        hasDraft: false,
        sending: false,
        typing: false,
        scrollIntoId: '',
        showToBottom: false,
        lastResetAt: resetAt,
      });
      this._pinned = true;
    }
  },

  // 非受控输入：按键绝不 setData 回推 value —— 受控回声在真机上与连续删除赛跑，
  // 旧值把刚删的字"恢复"回来（删除要按好几遍的根因）。草稿存 this._draft，
  // data.draft 只在程序化写值（发送清空 / chip 回填）时变更；按钮态用 hasDraft
  // 布尔（仅空↔非空翻转时 setData，每键零开销）。
  onInput(e) {
    this._draft = e.detail.value;
    const hasDraft = !!(this._draft || '').trim();
    if (hasDraft !== this.data.hasDraft) {
      this.setData({ hasDraft });
    }
  },

  // 真机键盘附件栏避让：聚焦时给输入栏垫底（kb-pad），失焦还原。
  // 引擎只按键盘高度顶页面，不算钉钉语音入口那条附件栏的高度。
  onInputFocus() {
    this.setData({ kbOpen: true });
  },

  onInputBlur() {
    this.setData({ kbOpen: false });
  },

  // 深度思考逐问开关（影响下一次发送；骨架阶段/打字中也可切，只对后续提问生效）
  onToggleThinking() {
    const next = !this.data.thinking;
    this.setData({ thinking: next });
    if (next) {
      dd.showToast({ type: 'none', content: '深度思考已开启：回答更慢但更仔细', duration: 2000 });
    }
  },

  // ---------- 滚动：钉住才跟滚 ----------
  onListScroll(e) {
    const d = e.detail || {};
    if (!this._listHeight || !d.scrollHeight) {
      return;
    }
    const dist = d.scrollHeight - d.scrollTop - this._listHeight;
    this._pinned = dist < 60;
    const show = dist > 120;
    if (show !== this.data.showToBottom) {
      this.setData({ showToBottom: show });
    }
  },

  _scrollToBottom(force) {
    if (!force && !this._pinned) {
      return;
    }
    if (force) {
      this._pinned = true;
      if (this.data.showToBottom) {
        this.setData({ showToBottom: false });
      }
    }
    // Toggle to force scroll-into-view to re-fire even for the same target.
    this.setData({ scrollIntoId: '' });
    setTimeout(() => {
      this.setData({ scrollIntoId: 'bottom-anchor' });
    }, 30);
  },

  onToBottom() {
    this._scrollToBottom(true);
  },

  // Called by <answer-bubble onGrow> as the typewriter reveals more text.
  // 打字机 30ms/字高频回调，跟滚节流到 ~240ms。
  onAnswerGrow() {
    const now = Date.now();
    if (now - this._lastGrowScroll < 240) {
      return;
    }
    this._lastGrowScroll = now;
    this._scrollToBottom(false);
  },

  onTypingEnd() {
    // 打完即标 instant：此后该消息任何重渲染（如整列表重建）都直出，绝不重播打字机
    const msgs = this.data.messages;
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m.role === 'ai' && m.blocks && !m.instant) {
        this._updateMessage(m.id, { instant: true });
        break;
      }
    }
    this.setData({ typing: false });
    this._scrollToBottom(false);
  },

  _toast(content) {
    dd.showToast({ content, type: 'none', duration: 1700 });
  },

  // 索引路径 setData：只过桥变更字段。整数组 setData 会把所有消息克隆出新身份
  // （旧答案 blocks 引用一变，answer-bubble 误判为新答案重启打字机 ——「旧答案
  // 自己重打一遍」bug 的根因），且序列化成本随会话长度线性上涨。
  _updateMessage(id, patch) {
    const idx = this.data.messages.findIndex((m) => m.id === id);
    if (idx < 0) {
      return;
    }
    const out = {};
    Object.keys(patch).forEach((k) => {
      out['messages[' + idx + '].' + k] = patch[k];
    });
    this.setData(out);
  },

  onToggleSources(e) {
    const id = e.currentTarget.dataset.id;
    const msg = this.data.messages.find((m) => m.id === id);
    if (!msg) {
      return;
    }
    this._updateMessage(id, { sourcesOpen: !msg.sourcesOpen });
    setTimeout(() => this._scrollToBottom(false), 280);
  },

  // 发送/停止。打字中「停止」= 跳过打字机整段直出（内容已在客户端，丢弃毫无意义）；
  // 骨架阶段「停止」= RequestTask.abort 真取消（深思 120s 等待必须有出口），
  // 老基础库无 abort 时保底「到达后直出」。
  onSend() {
    if (this.data.typing) {
      const msgs = this.data.messages;
      for (let i = msgs.length - 1; i >= 0; i--) {
        const m = msgs[i];
        if (m.role === 'ai' && m.blocks && !m.instant) {
          this._updateMessage(m.id, { instant: true });
          break;
        }
      }
      return;
    }
    if (this.data.sending) {
      const t = this._reqTask;
      if (t && typeof t.abort === 'function') {
        this._abortedAiId = this._pendingAiId;
        try {
          t.abort();
        } catch (e) {
          // 已完成/重复点按的竞态：忽略，then/catch 自会收尾
        }
      } else {
        this._skipOnArrive = true;
      }
      return;
    }
    const question = (this._draft || '').trim();
    if (!question) {
      return;
    }
    // 两段式清空：data.draft 可能仍是 ''（非受控期间没跟踪），直接置 '' 无 diff
    // 不会触发原生输入框清空 —— 先同步成当前文本，再异步置空（必有 diff）。
    this._draft = '';
    this.setData({ draft: question, hasDraft: false });
    setTimeout(() => {
      this.setData({ draft: '' });
    }, 0);
    this._ask(question, true);
  },

  onQuickTap(e) {
    if (this.data.sending || this.data.typing) {
      return;
    }
    const q = e.currentTarget.dataset.q;
    if (q) {
      this._ask(q, true);
    }
  },

  // 错误卡重试：原位移除错误卡 → 新骨架重答（用户气泡保留，不重复添加）
  onRetry(e) {
    if (this.data.sending || this.data.typing) {
      return;
    }
    const id = e.currentTarget.dataset.id;
    const msg = this.data.messages.find((m) => m.id === id);
    if (!msg || !msg.question) {
      return;
    }
    this.setData({ messages: this.data.messages.filter((m) => m.id !== id) });
    this._ask(msg.question, false);
  },

  // NO_RESULT「换个说法」chip：回填输入框让用户确认/微调后再发（原型同语义，不自动发送）
  onNrChipTap(e) {
    const q = e.currentTarget.dataset.q;
    if (q && !this.data.sending && !this.data.typing) {
      this._draft = q;
      this.setData({ draft: q, hasDraft: true });
    }
  },

  // NO_RESULT 卡内转人工：死胡同必须留出口（无来源、无赞踩，只有这一条路）
  onNrHandoff(e) {
    const id = e.currentTarget.dataset.id;
    const msg = this.data.messages.find((m) => m.id === id);
    if (!msg || msg.handoffDone || !msg.messageId) {
      return;
    }
    postFeedback({ message_id: msg.messageId, feedback_type: 'handoff' })
      .then(() => {
        this._updateMessage(id, { handoffDone: true });
        this._toast('已转交管理员跟进，请留意钉钉消息');
      })
      .catch((err) => {
        console.error('[chat.onNrHandoff]', err);
        this._toast('提交失败，请重试');
      });
  },

  // 骨架阶段文案：先「检索」，约 2.2s 后切「生成/深思」。定时器在响应/失败/取消时清理；
  // 回调内再查一次 loading 态，杜绝迟到的定时器改写已完成的消息。
  _startStageTimer(aiId, thinking) {
    this._stageTimers[aiId] = setTimeout(() => {
      delete this._stageTimers[aiId];
      const msg = this.data.messages.find((m) => m.id === aiId);
      if (msg && msg.loading) {
        this._updateMessage(aiId, {
          stageText: thinking ? '深度思考中，可能需要约 1 分钟…' : '正在生成回答…',
        });
      }
    }, 2200);
  },

  _clearStageTimer(aiId) {
    if (this._stageTimers[aiId]) {
      clearTimeout(this._stageTimers[aiId]);
      delete this._stageTimers[aiId];
    }
  },

  _ask(question, addUserMsg) {
    const aiId = nextId();
    const thinking = !!this.data.thinking; // 发送瞬间定格（骨架文案 + 请求参数同源）
    const newMsgs = [];
    if (addUserMsg) {
      newMsgs.push({ id: nextId(), role: 'user', text: question });
    }
    newMsgs.push({
      id: aiId, role: 'ai', loading: true, question, thinking,
      stageText: '正在检索知识库…',
    });
    this._skipOnArrive = false;
    this._reqTask = null;
    this._pendingAiId = aiId;
    // 索引路径追加：concat 整数组 setData 会重新序列化全部历史消息（身份全换 +
    // 成本随会话线性涨）。'messages[N]' 越界索引赋值 = 数据路径式追加。
    const base = this.data.messages.length;
    const appendPatch = { sending: true };
    newMsgs.forEach((m, i) => {
      appendPatch['messages[' + (base + i) + ']'] = m;
    });
    this.setData(appendPatch);
    this._scrollToBottom(true);
    this._startStageTimer(aiId, thinking);

    // ensureLogin() is idempotent and cached. 登录失败降级匿名继续提问：
    // 后端无 token = 仅 public 语料，免登抖动不应塞死问答（IDE 模拟器中免登必失败）。
    ensureLogin()
      .catch(() => null)
      .then(() => ask(question, this.data.sessionId, {
        thinking,
        onTask: (t) => {
          this._reqTask = t;
        },
      }))
      .then((resp) => {
        this._clearStageTimer(aiId);
        this._reqTask = null;
        this._abortedAiId = '';
        resp = resp || {};
        // Adopt a server-issued session id if we did not have one yet.
        if (resp.session_id && !this.data.sessionId) {
          this.setData({ sessionId: resp.session_id });
        }

        // 知识库未命中（检索为空或 LLM 拒答）：空结果卡，隐藏来源/赞踩；
        // rephrase = 服务端「换个说法」建议（相似 SUCCESS 问题优先，可答性有保证）
        if (resp.no_result) {
          this._updateMessage(aiId, {
            loading: false,
            noResult: true,
            rephrase: resp.rephrase || [],
            messageId: resp.message_id || '',
          });
          this.setData({ sending: false, serviceOk: true });
          this._scrollToBottom(true);
          return;
        }

        let blocks = resp.blocks || [];
        // 纯文字答案/未引用图片时后端约定返回 blocks=[]，需降级渲染 answer 文本，
        // 否则气泡为空白（RAG_PURE_TEXT 模式下 100% 触发）。
        if (!blocks.length && resp.answer) {
          blocks = [{ type: 'text', format: 'plain', text: resp.answer }];
        }
        this._updateMessage(aiId, {
          loading: false,
          blocks,
          guard: !!resp.guard,
          sources: mapSources(resp.sources),
          sourcesOpen: false,
          messageId: resp.message_id || '',
          copyText: resp.answer || '',
          question,
          instant: !!this._skipOnArrive,
        });
        this._skipOnArrive = false;
        this.setData({ sending: false, typing: true, serviceOk: true });
        this._scrollToBottom(true);
      })
      .catch((err) => {
        this._clearStageTimer(aiId);
        this._reqTask = null;
        // 用户主动取消 ≠ 服务故障：不降级健康态、错误卡文案区分（重试按钮共用）
        const cancelled = this._abortedAiId === aiId;
        this._abortedAiId = '';
        if (!cancelled) {
          console.error('[chat._ask]', err);
        }
        this._updateMessage(aiId, {
          loading: false,
          error: true,
          errorText: cancelled ? '已取消本次提问。' : '回答失败，请检查网络后重试。',
          question,
        });
        this.setData({
          sending: false,
          serviceOk: cancelled ? this.data.serviceOk : false,
        });
        this._scrollToBottom(true);
      });
  },
});
