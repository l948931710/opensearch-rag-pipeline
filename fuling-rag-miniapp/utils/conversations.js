// 多会话（threaded conversations）本地层：索引 + 每会话消息缓存 + 服务端列表合并。
//
// 设计（与控制台同构，见 opensearch_pipeline/api.py /api/conversations*）：
//   - conversation_id 客户端生成，随 /api/ask 上送；服务端 RAG_CONVERSATION_HISTORY
//     开启时按它归并 qa_session_log / qa_conversation。flag 关闭时服务端列表为空，
//     本地存储独立成立 —— 功能不依赖后端开关（开了则多端同步 + 换机可见）。
//   - 本地索引/消息缓存单账号制：fl_conv_owner 记录归属 userId，登录后发现换号
//     即整体清空（防共用设备串号）。
//   - 纯函数（merge/lean/时间/标题/裁剪）与 dd 存储包装分离；纯函数在
//     tests/units.test.mjs 中无 dd 环境直接单测。
//
// dd storage API 形状（Alipay 引擎）：setStorageSync({key,data}) /
// getStorageSync({key}) -> {data} / removeStorageSync({key})。

const KEY_OWNER = 'fl_conv_owner_v1';
const KEY_INDEX = 'fl_conv_index_v1';
const KEY_MSGS_PREFIX = 'fl_conv_msgs_v1:';

// 本地最多保留 30 个会话（LRU，淘汰行连带清其消息缓存）；每会话最多缓存 40 条
// 消息（20 轮）。上限护 dd 单 key ~200KB 限额，也护 setData 首屏成本。
export const MAX_CONVERSATIONS = 30;
export const MAX_MESSAGES = 40;

// ── 纯函数（无 dd 依赖，可单测） ──────────────────────────────────────

let _idSeq = 0;

/** 客户端会话 ID：'c' + 时间基 36 + 随机段（无 crypto API，Math.random 足够 ——
 * 仅需本人维度唯一；服务端按 (user_id, conversation_id) 归并）。≤128 字符契约内。 */
export function genConvId() {
  _idSeq = (_idSeq + 1) % 1296;
  const rand = Math.floor(Math.random() * 0xffffff).toString(36);
  return 'c' + Date.now().toString(36) + _idSeq.toString(36) + rand;
}

/** 首问 → 会话标题：压掉换行/连续空白，截 60 字（列表行再按容器省略）。 */
export function deriveTitle(question) {
  const t = String(question || '').replace(/\s+/g, ' ').trim();
  return t.slice(0, 60);
}

/** 服务端时间字符串 → ms。'2026-07-02 10:11:12(.123)' / 'T' 分隔均可；
 * 不走 new Date(string)（iOS JSC 对空格分隔格式返回 Invalid Date）。失败 0。 */
export function parseServerTime(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(String(s || ''));
  if (!m) {
    return 0;
  }
  return new Date(
    Number(m[1]), Number(m[2]) - 1, Number(m[3]),
    Number(m[4]), Number(m[5]), Number(m[6] || 0)
  ).getTime();
}

function two(n) {
  return n < 10 ? '0' + n : '' + n;
}

/** 会话列表时间标签：刚刚 / N分钟前 / 今天 HH:MM / 昨天 / N天前 / MM-DD（跨年带年份）。 */
export function relativeTimeLabel(ms, nowMs) {
  if (!ms) {
    return '';
  }
  const now = nowMs || Date.now();
  const diff = now - ms;
  if (diff < 60 * 1000) {
    return '刚刚';
  }
  if (diff < 60 * 60 * 1000) {
    return Math.floor(diff / 60000) + '分钟前';
  }
  const d = new Date(ms);
  const n = new Date(now);
  const sameDay = d.getFullYear() === n.getFullYear()
    && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  if (sameDay) {
    return two(d.getHours()) + ':' + two(d.getMinutes());
  }
  const yest = new Date(now - 24 * 60 * 60 * 1000);
  const isYest = d.getFullYear() === yest.getFullYear()
    && d.getMonth() === yest.getMonth() && d.getDate() === yest.getDate();
  if (isYest) {
    return '昨天';
  }
  if (diff < 7 * 24 * 60 * 60 * 1000) {
    return Math.floor(diff / (24 * 60 * 60 * 1000)) + '天前';
  }
  if (d.getFullYear() === n.getFullYear()) {
    return two(d.getMonth() + 1) + '-' + two(d.getDate());
  }
  return d.getFullYear() + '-' + two(d.getMonth() + 1) + '-' + two(d.getDate());
}

/**
 * 本地索引 × 服务端 /api/conversations 合并（仅用于抽屉展示，不直接落盘）：
 *   - 同 id：本地行为准（持 sessionId/消息缓存），标题本地缺失时补服务端的，
 *     updatedAt 取两者较新。
 *   - 服务端独有：以 remote:true 占位（消息点开时懒加载，落盘转正）。
 * 结果按 updatedAt 倒序。server 行时间解析失败按 0 排尾，不臆造。
 */
export function mergeServerList(localList, serverItems) {
  const out = (localList || []).map((c) => Object.assign({}, c));
  const byId = {};
  out.forEach((c) => {
    byId[c.id] = c;
  });
  (serverItems || []).forEach((s) => {
    const id = s && s.conversation_id;
    if (!id) {
      return;
    }
    const ts = parseServerTime(s.updated_at);
    const local = byId[id];
    if (local) {
      if (!local.title && s.title) {
        local.title = s.title;
      }
      if (ts > (local.updatedAt || 0)) {
        local.updatedAt = ts;
      }
    } else {
      out.push({ id, title: s.title || '', updatedAt: ts, sessionId: '', remote: true });
    }
  });
  out.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  return out;
}

/**
 * NO_RESULT 卡正文拆行（通用能力分级开放：后端引导式拒答话术为多行文本，
 * axml 无法运行时拆分 —— 存消息前/恢复时预计算 nrLines）。
 * 空/非法入参返回 []（axml 据此回退硬编码旧文案，兼容老后端）。
 */
export function splitNrLines(answer) {
  return String(answer || '')
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean);
}

/**
 * 页面消息数组 → 可落盘精简形态：丢弃 loading/error 瞬态，保留最终回答与
 * NO_RESULT 卡；AI 行统一带 instant:true（恢复渲染绝不重播打字机）。
 * 截尾保留最近 MAX_MESSAGES 条。
 */
export function leanMessages(messages) {
  const out = [];
  (messages || []).forEach((m) => {
    if (!m || m.loading || m.error) {
      return;
    }
    if (m.role === 'user') {
      out.push({ role: 'user', text: m.text || '' });
      return;
    }
    if (m.noResult) {
      out.push({
        // answer = 后端引导式拒答话术原文（nrLines 恢复时经 splitNrLines 重算，
        // 不落盘 —— 省 200KB 本地存储配额）
        role: 'ai', noResult: true, answer: m.answer || '',
        rephrase: m.rephrase || [], messageId: m.messageId || '',
        question: m.question || '', handoffDone: !!m.handoffDone,
      });
      return;
    }
    if (m.blocks && m.blocks.length) {
      out.push({
        role: 'ai', blocks: m.blocks, guard: !!m.guard, general: !!m.general,
        sources: m.sources || [], messageId: m.messageId || '',
        copyText: m.copyText || '', question: m.question || '',
        instant: true,
      });
    }
  });
  return out.slice(-MAX_MESSAGES);
}

/** 索引裁剪到 cap：返回 {list, evictedIds}（淘汰尾部 = updatedAt 最旧）。 */
export function capIndex(list, cap) {
  const sorted = (list || []).slice()
    .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  const max = cap || MAX_CONVERSATIONS;
  return {
    list: sorted.slice(0, max),
    evictedIds: sorted.slice(max).map((c) => c.id),
  };
}

// ── dd 存储包装（页面用；node 单测环境无 dd，以上纯函数不经过这里） ──────

function _get(key) {
  try {
    const r = dd.getStorageSync({ key });
    return r ? r.data : null;
  } catch (e) {
    return null;
  }
}

function _set(key, data) {
  try {
    dd.setStorageSync({ key, data });
    return true;
  } catch (e) {
    return false; // 配额/序列化失败：静默降级（会话缓存是增益不是命脉）
  }
}

function _remove(key) {
  try {
    dd.removeStorageSync({ key });
  } catch (e) {
    // ignore
  }
}

/** 登录归属校验：换号即整体清空，防共用设备看到他人本地会话。 */
export function ensureOwner(userId) {
  const uid = userId || 'anon';
  const owner = _get(KEY_OWNER);
  if (owner && owner !== uid) {
    clearAllLocal();
  }
  _set(KEY_OWNER, uid);
}

export function loadIndex() {
  const list = _get(KEY_INDEX);
  return Array.isArray(list) ? list : [];
}

function _saveIndex(list) {
  const capped = capIndex(list, MAX_CONVERSATIONS);
  capped.evictedIds.forEach((id) => _remove(KEY_MSGS_PREFIX + id));
  _set(KEY_INDEX, capped.list);
  return capped.list;
}

/** 会话置顶更新（新建首问 / 每轮问答后调用）。patch: {title?, sessionId?} */
export function touchConversation(convId, patch) {
  if (!convId) {
    return;
  }
  const list = loadIndex();
  let row = null;
  const rest = [];
  list.forEach((c) => {
    if (c.id === convId) {
      row = c;
    } else {
      rest.push(c);
    }
  });
  if (!row) {
    row = { id: convId, title: '', updatedAt: 0, sessionId: '' };
  }
  if (patch && patch.title && !row.title) {
    row.title = patch.title;
  }
  if (patch && patch.sessionId) {
    row.sessionId = patch.sessionId;
  }
  delete row.remote;
  row.updatedAt = Date.now();
  rest.unshift(row);
  _saveIndex(rest);
}

export function loadMessages(convId) {
  const msgs = _get(KEY_MSGS_PREFIX + convId);
  return Array.isArray(msgs) ? msgs : [];
}

/** 落盘该会话消息（已 lean 化）。超配额时对半截尾重试一次，再失败放弃。 */
export function saveMessages(convId, lean) {
  if (!convId) {
    return;
  }
  if (!_set(KEY_MSGS_PREFIX + convId, lean)) {
    _set(KEY_MSGS_PREFIX + convId, lean.slice(-Math.ceil(MAX_MESSAGES / 2)));
  }
}

export function removeConversation(convId) {
  _remove(KEY_MSGS_PREFIX + convId);
  _saveIndex(loadIndex().filter((c) => c.id !== convId));
}

/** 清空全部本地会话（索引 + 消息缓存）。返回被清的 {ids, sessionIds}，
 * 供调用方 best-effort 同步服务端（DELETE /api/conversations + session clear）。 */
export function clearAllLocal() {
  const list = loadIndex();
  list.forEach((c) => _remove(KEY_MSGS_PREFIX + c.id));
  _remove(KEY_INDEX);
  return {
    ids: list.map((c) => c.id),
    sessionIds: list.map((c) => c.sessionId).filter(Boolean),
  };
}
