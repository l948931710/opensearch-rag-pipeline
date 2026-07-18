// Chat page: the core Q&A screen (藕粉纸 Lotus-Paper v2).
//
// /api/ask 契约消费：blocks[]（空时降级 answer）、no_result（空结果卡）、
// guard（低匹配提示条）、sources[].level（相关度高/中/低徽章 —— rerank 开启后
// score 是 0-1 量纲，禁止本地按融合阈值重算，仅在 level 缺省时兜底）。
//
// 多会话（v2）：与控制台同构 —— 每个会话一个客户端 conversation_id（随 ask 上送）
// + 一个服务端 session_id（LLM 多轮记忆，首答时由服务端下发并记回）。消息本地
// 落盘（utils/conversations.js），打开小程序恢复最近会话；抽屉列表合并服务端
// /api/conversations（RAG_CONVERSATION_HISTORY 开启时），点开云端会话懒加载。

import { ensureLogin } from '../../utils/auth';
import {
  ask, feedback as postFeedback, getHotQuestions,
  getConversations, getConversationMessages, deleteConversation,
} from '../../utils/api';
import {
  genConvId, deriveTitle, relativeTimeLabel, mergeServerList, leanMessages,
  ensureOwner, loadIndex, loadMessages, saveMessages, touchConversation,
  removeConversation, splitNrLines,
} from '../../utils/conversations';

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

function nowLabel() {
  const now = new Date();
  return '今天 ' + pad2(now.getHours()) + ':' + pad2(now.getMinutes());
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

// 落盘精简消息 → 页面消息（恢复渲染一律 instant，绝不重播打字机）
function reviveMessage(m) {
  if (m.role === 'user') {
    return { id: nextId(), role: 'user', text: m.text || '' };
  }
  if (m.noResult) {
    return {
      id: nextId(), role: 'ai', noResult: true,
      answer: m.answer || '', nrLines: splitNrLines(m.answer),
      rephrase: m.rephrase || [], messageId: m.messageId || '',
      question: m.question || '', handoffDone: !!m.handoffDone,
    };
  }
  return {
    id: nextId(), role: 'ai',
    blocks: m.blocks || [], guard: !!m.guard, general: !!m.general,
    sources: m.sources || [], sourcesOpen: false,
    messageId: m.messageId || '', copyText: m.copyText || '',
    question: m.question || '', instant: true,
  };
}

// /api/conversations/{id} 的 items（与 /api/history 同构）→ 页面消息对。
// 服务端不存 sources/guard，恢复视图只还原问答本体；messageId 在则反馈条可用。
function reviveServerItems(items) {
  const msgs = [];
  (items || []).forEach((it) => {
    if (!it || !it.question) {
      return;
    }
    msgs.push({ id: nextId(), role: 'user', text: it.question });
    if (it.status === 'NO_RESULT') {
      msgs.push({
        id: nextId(), role: 'ai', noResult: true, rephrase: [],
        // 服务端历史里引导式拒答话术落在 answer_text —— 恢复时同样可见
        answer: it.answer || '', nrLines: splitNrLines(it.answer),
        messageId: it.message_id || '', question: it.question,
      });
      return;
    }
    let blocks = (it.blocks && it.blocks.length) ? it.blocks : [];
    if (!blocks.length) {
      blocks = [{ type: 'text', format: 'plain', text: it.answer || '（无回答内容）' }];
    }
    msgs.push({
      id: nextId(), role: 'ai', blocks, guard: false,
      sources: [], sourcesOpen: false,
      messageId: it.message_id || '', copyText: it.answer || '',
      question: it.question, instant: true,
    });
  });
  return msgs;
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
    scrollAnim: false, // 首屏恢复瞬时定位；之后打开平滑滚动
    showToBottom: false,
    // ── 多会话 ──
    convId: '',        // 当前会话（客户端 conversation_id）
    sessionId: '',     // 当前会话的服务端 session_id（首答后由服务端下发记回）
    drawerMounted: false, // 两段式挂载：mounted 控 a:if，open 控滑入动画类
    drawerOpen: false,
    convList: [],      // 抽屉展示列表（本地索引 × 服务端合并的装饰行）
    lastResetAt: 0,    // tracks the 清空会话 marker from the settings page
  },

  onLoad() {
    let sbh = 0;
    try {
      sbh = dd.getSystemInfoSync().statusBarHeight || 0;
    } catch (e) {
      sbh = 0;
    }
    this.setData({ statusBarHeight: sbh });
    this._draft = '';           // 非受控草稿（真值；data.draft 只做程序化覆盖）
    this._pinned = true;        // 钉住才跟滚：用户上翻重读时绝不抢滚动条
    this._listHeight = 0;
    this._lastGrowScroll = 0;
    this._skipOnArrive = false; // 骨架阶段点「停止」→ 响应到达后整段直出（abort 不可用时的保底）
    this._stageTimers = {};     // aiId → 骨架阶段文案定时器（检索→生成）
    this._reqTask = null;       // 当前在途请求的 RequestTask（abort 真取消）
    this._pendingAiId = '';     // 在途请求对应的 AI 消息 id
    this._abortedAiId = '';     // 用户主动取消的消息 id（catch 里区分取消与失败）
    this._serverConvs = [];     // 最近一次 /api/conversations 结果（仅内存，抽屉合并用）
    this._loadingConvId = '';   // 正在懒加载的云端会话（防重复点击）

    // 批次8（ultra chat.js:166）：本地恢复移到 ensureOwner **之后**——此前先渲染再校验
    // 归属，共享设备上第二个账号在登录往返期间会看到（并可能续写进）前一个账号的本地
    // 会话缓存。现在登录成功→ensureOwner（换号即清空）→再恢复；登录失败不恢复任何
    // 缓存（页面保持全新会话可用，重试发送时 _ask 路径会补登录+归属校验）。
    this.setData({ convId: genConvId(), startTimeLabel: nowLabel() });

    ensureLogin()
      .then((g) => {
        ensureOwner(g.userId || 'anon');
        this._restoreLast();
      })
      .catch(() => {
        // ensureLogin already toasted; leave the page usable so a retry on send works.
        // 刻意不 ensureOwner('anon')：网络抖动的登录失败绝不能把单人设备的本地缓存清掉。
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

    // 首屏定位完成后再启用平滑滚动
    setTimeout(() => {
      this.setData({ scrollAnim: true });
    }, 600);
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

  onHide() {
    // 切到别的 tab 时收起抽屉（native tabBar 不被 fixed 层覆盖，可随时切走）
    if (this.data.drawerMounted) {
      this.setData({ drawerMounted: false, drawerOpen: false });
    }
  },

  onShow() {
    // Honor 清空会话 from the settings page: if the marker advanced, storage 已被
    // settings 清空，这里重置可见 UI 并开启全新会话。
    let resetAt = 0;
    try {
      const r = dd.getStorageSync({ key: 'session_reset_at' });
      resetAt = (r && r.data) || 0;
    } catch (e) {
      resetAt = 0;
    }
    if (resetAt && resetAt !== this.data.lastResetAt) {
      this._serverConvs = [];
      this._freshConversation();
      this.setData({ lastResetAt: resetAt });
      // 批次8（ultra chat.js:232）：处理完即移除 marker——lastResetAt 每次冷启动重置为 0，
      // marker 不清则 resetAt!==0 永真，「清空会话」之后每次冷启动都会把刚恢复的会话
      // 再静默清一遍。settings 只写不读本键，移除安全；移除失败最坏=下次冷启动多清一次。
      try {
        dd.removeStorageSync({ key: 'session_reset_at' });
      } catch (e) {
        // 忽略：见上
      }
    }
  },

  // ── 多会话：恢复 / 新建 / 抽屉 ──────────────────────────────────

  // 打开小程序恢复最近会话（索引首行）；无则开新会话（不落盘，首问才落）。
  _restoreLast() {
    const idx = loadIndex();
    const current = idx.length ? idx[0] : null;
    const lean = current ? loadMessages(current.id) : [];
    if (current && lean.length) {
      this.setData({
        convId: current.id,
        sessionId: current.sessionId || '',
        messages: lean.map(reviveMessage),
        startTimeLabel: relativeTimeLabel(current.updatedAt) || nowLabel(),
      });
      this._scrollToBottom(true);
    } else {
      this.setData({ convId: genConvId(), startTimeLabel: nowLabel() });
    }
  },

  // 重置为全新会话（不动已落盘的其他会话）
  _freshConversation() {
    this._draft = '';
    this.setData({
      convId: genConvId(),
      sessionId: '',
      messages: [],
      draft: '',
      hasDraft: false,
      sending: false,
      typing: false,
      scrollIntoId: '',
      showToBottom: false,
      startTimeLabel: nowLabel(),
    });
    this._pinned = true;
  },

  // 当前会话落盘：索引置顶（首问定标题）+ 消息精简缓存。逐答调用，无需 flush。
  _persistCurrent() {
    const convId = this.data.convId;
    if (!convId) {
      return;
    }
    const lean = leanMessages(this.data.messages);
    if (!lean.length) {
      return; // 空会话不进索引
    }
    const firstUser = lean.find((m) => m.role === 'user');
    touchConversation(convId, {
      title: firstUser ? deriveTitle(firstUser.text) : '',
      sessionId: this.data.sessionId,
    });
    saveMessages(convId, lean);
  },

  _buildConvList() {
    const merged = mergeServerList(loadIndex(), this._serverConvs);
    return merged.slice(0, 50).map((c) => ({
      id: c.id,
      title: c.title || '新会话',
      timeLabel: relativeTimeLabel(c.updatedAt),
      active: c.id === this.data.convId,
      remote: !!c.remote,
    }));
  },

  onOpenDrawer() {
    this.setData({ drawerMounted: true, convList: this._buildConvList() });
    setTimeout(() => {
      this.setData({ drawerOpen: true });
    }, 30);
    // 服务端会话列表合并（RAG_CONVERSATION_HISTORY 开启时才有数据；匿名/失败静默）
    ensureLogin()
      .then((g) => {
        // 批次8：owner 复查——换号后本地索引即清，抽屉只按新身份的本地+服务端列表构建
        if (g && g.userId) {
          ensureOwner(g.userId);
        }
        return getConversations();
      })
      .then((resp) => {
        this._serverConvs = (resp && resp.items) || [];
        if (this.data.drawerMounted) {
          this.setData({ convList: this._buildConvList() });
        }
      })
      .catch(() => {});
  },

  onCloseDrawer() {
    this.setData({ drawerOpen: false });
    setTimeout(() => {
      this.setData({ drawerMounted: false });
    }, 260);
  },

  onNewConversation() {
    if (this.data.sending || this.data.typing) {
      this._toast('正在回答中，请稍候再新建会话');
      return;
    }
    if (this.data.drawerMounted) {
      this.onCloseDrawer();
    }
    if (!this.data.messages.length) {
      return; // 已是空会话
    }
    this._freshConversation();
  },

  onSelectConv(e) {
    if (this.data.sending || this.data.typing) {
      this._toast('正在回答中，请稍候再切换会话');
      return;
    }
    const id = e.currentTarget.dataset.id;
    if (!id || id === this.data.convId) {
      this.onCloseDrawer();
      return;
    }
    const row = this._buildConvList().find((c) => c.id === id);
    const idxRow = loadIndex().find((c) => c.id === id);
    const lean = loadMessages(id);
    if (lean.length) {
      // 本地缓存直开
      this.setData({
        convId: id,
        sessionId: (idxRow && idxRow.sessionId) || '',
        messages: lean.map(reviveMessage),
        startTimeLabel: relativeTimeLabel((idxRow && idxRow.updatedAt) || 0) || nowLabel(),
        sending: false,
        typing: false,
      });
      this._pinned = true;
      this.onCloseDrawer();
      this._scrollToBottom(true);
      return;
    }
    // 云端会话懒加载（保持当前会话可见，加载成功才切换）
    if (this._loadingConvId === id) {
      return;
    }
    this._loadingConvId = id;
    dd.showLoading({ content: '加载会话…' });
    getConversationMessages(id)
      .then((resp) => {
        this._loadingConvId = '';
        dd.hideLoading();
        const msgs = reviveServerItems(resp && resp.items);
        if (!msgs.length) {
          this._toast('该会话暂无可显示的消息');
          return;
        }
        this.setData({
          convId: id,
          sessionId: '', // 云端会话无本地 LLM session；下一问由服务端新发
          messages: msgs,
          startTimeLabel: (row && row.timeLabel) || nowLabel(),
          sending: false,
          typing: false,
        });
        this._pinned = true;
        // 转正落盘：下次直开；标题沿用服务端/首问
        this._persistCurrent();
        this.onCloseDrawer();
        this._scrollToBottom(true);
      })
      .catch(() => {
        this._loadingConvId = '';
        dd.hideLoading();
        this._toast('会话加载失败，请稍后重试');
      });
  },

  onDeleteConv(e) {
    const id = e.currentTarget.dataset.id;
    if (!id) {
      return;
    }
    if (id === this.data.convId && (this.data.sending || this.data.typing)) {
      this._toast('正在回答中，请稍候再删除当前会话');
      return;
    }
    dd.confirm({
      title: '删除会话',
      content: '删除后本机与云端列表将不再显示该会话，历史问答记录不受影响。',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      success: (res) => {
        if (!res.confirm) {
          return;
        }
        removeConversation(id);
        this._serverConvs = this._serverConvs.filter((s) => s.conversation_id !== id);
        // 服务端软隐藏 best-effort（flag 关闭时 404，一并忽略）
        deleteConversation(id).catch(() => {});
        if (id === this.data.convId) {
          this._freshConversation();
        }
        this.setData({ convList: this._buildConvList() });
      },
    });
  },

  // ── 输入 / 滚动（v1 原样保留，真机踩坑修复勿动） ─────────────────

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
        this._persistCurrent();
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
    const convId = this.data.convId;       // 定格会话（响应回来时校验未被切走）
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
      .then((g) => {
        // 批次8：owner 复查——冷启动登录失败后经此路径首次拿到身份时，换号即清他人缓存
        //（此时屏上是全新未落盘会话，清空不影响可见状态）。
        if (g && g.userId) {
          ensureOwner(g.userId);
        }
        return ask(question, this.data.sessionId, {
        thinking,
        conversationId: convId,
        onTask: (t) => {
          this._reqTask = t;
        },
        });
      })
      .then((resp) => {
        this._clearStageTimer(aiId);
        this._reqTask = null;
        this._abortedAiId = '';
        // 会话在途中被删除/重置（messages 已清）：整个响应静默丢弃，
        // 绝不把旧会话的 typing/sessionId 状态泼到新会话上。
        if (convId !== this.data.convId) {
          return;
        }
        resp = resp || {};
        // 记回服务端 session_id（首答下发；后续应答原样回显）。
        if (resp.session_id && resp.session_id !== this.data.sessionId) {
          this.setData({ sessionId: resp.session_id });
        }

        // 知识库未命中（检索为空或 LLM 拒答）：空结果卡，隐藏来源/赞踩；
        // rephrase = 服务端「换个说法」建议（相似 SUCCESS 问题优先，可答性有保证）。
        // answer = 引导式拒答话术（RAG_GUIDED_REFUSAL：换说法/相近文档标题/知识贡献
        // 入口，多行文本）；nrLines 预拆行供 axml 逐行渲染，空则回退硬编码旧文案
        // —— flag 关时 answer 即旧固定文案，视觉等价。
        if (resp.no_result) {
          const nrAnswer = String(resp.answer || '').trim();
          this._updateMessage(aiId, {
            loading: false,
            noResult: true,
            answer: nrAnswer,
            nrLines: splitNrLines(nrAnswer),
            rephrase: resp.rephrase || [],
            messageId: resp.message_id || '',
          });
          this.setData({ sending: false, serviceOk: true });
          this._persistCurrent();
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
          // 通用回答徽标（resp.source ∈ general|smalltalk）：与正文免责尾注双保险
          general: resp.source === 'general' || resp.source === 'smalltalk',
          sources: mapSources(resp.sources),
          sourcesOpen: false,
          messageId: resp.message_id || '',
          copyText: resp.answer || '',
          question,
          instant: !!this._skipOnArrive,
        });
        this._skipOnArrive = false;
        this.setData({ sending: false, typing: true, serviceOk: true });
        this._persistCurrent();
        this._scrollToBottom(true);
      })
      .catch((err) => {
        this._clearStageTimer(aiId);
        this._reqTask = null;
        if (convId !== this.data.convId) {
          return; // 会话已被删除/重置，同上静默丢弃
        }
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
