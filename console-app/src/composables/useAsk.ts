import { computed, reactive, ref, watch } from 'vue'
import { apiFetch, apiJson } from '@/lib/api'
import { createSseDecoder, type SseEvent } from '@/lib/sseDecoder'
import { renderMd, stripImg } from '@/lib/markdown'
import { useSession } from '@/stores/session'

// 问答单一事实来源（模块级单例，等同轻量 store）。多会话（Atlas 式）：每条会话独立 messages +
// 服务端 qaSession；新建/切换/删除/搜索；localStorage 持久化（reload 仍在，故有会话历史）。

const NO_RESULT_FALLBACK = '抱歉，当前知识库中未找到相关信息。'

export type Level = 'high' | 'mid' | 'low'

export interface SourceRow { idx: number; title: string; section: string; levelLabel: string; level: Level; score: number; relevance: number; preview: string }

export interface ViewBlock {
  type: 'text' | 'image'
  html?: string
  url?: string
  oss_key?: string
  caption?: string
  alt?: string
  failed?: boolean
  reloading?: boolean
}

export interface ChatMessage {
  id: string
  role: 'user' | 'ai'
  text?: string            // 用户气泡文本
  question?: string        // AI 消息对应的问句（重试用）
  loading?: boolean
  stageText?: string
  raw?: string             // 累积的流式原始文本
  html?: string            // 渲染后的纯文本答案
  viewBlocks?: ViewBlock[] | null   // 图文定稿（content_blocks 帧后）
  copyText?: string
  messageId?: string       // 反馈关联键（来自 session 帧）
  sources?: SourceRow[]
  sourcesOpen?: boolean
  guard?: boolean          // 低置信提示
  noResult?: boolean
  answer?: string          // no_result 文案
  rephrase?: string[]      // no_result 改写建议
  voted?: '' | 'up' | 'down'
  handoffDone?: boolean
  copied?: boolean
  error?: boolean
  errorText?: string
  streaming?: boolean      // 正在流式书写（驱动答案末尾的流式光标）；finish/stop/error 置 false
  reasoning?: string       // 思考过程原始累积（深度思考 + RAG_STREAM_REASONING 开时下发的 reasoning 帧）
  reasoningHtml?: string   // 思考过程已渲染（与答案共用匀速吐字泵平滑显现）
  reasoningOpen?: boolean  // 「思考过程」披露条是否展开（思考中默认展开，答案开始自动收起，可手动切换）
  reasoningMs?: number     // 思考耗时（ms）：首个 reasoning 帧 → 答案开始；收起态如实展示"思考 N.Ns"
  _reasoningT0?: number    // 思考起点时间戳（performance.now）
  _stageTimer?: ReturnType<typeof setTimeout> | null
  _renderRaf?: number | null
  _shownLen?: number       // 答案已"吐字"显现到的字符位置（匀速泵推进，<= raw 长度）
  _lastRenderTs?: number   // 答案上次渲染时间戳（performance.now），用于节流到 ~40fps
  _rRaf?: number | null    // 思考通道 rAF 句柄
  _rShownLen?: number      // 思考过程已显现位置
  _rTs?: number            // 思考通道上次渲染时间戳
  _reasoningDone?: boolean // 思考流结束（答案开始/收尾）→ 停思考泵、定稿全文
  _thinking?: boolean      // 本次是否开了「深度思考」（仅影响有据等待态文案）
}

export interface Conversation {
  id: string
  title: string            // 取首条用户问句；未提问前为「新对话」
  messages: ChatMessage[]
  qaSession: string        // 服务端会话关联（reload 后失效，下次提问重建）
  updatedAt: number
  _server?: boolean        // 仅服务端历史回灌的占位（消息点开再拉）
  _loading?: boolean       // 该会话消息按需加载中
}

// 会话 ID = UUIDv4。优先 crypto.randomUUID（仅安全上下文/https），降级 getRandomValues（http 也可用），
// 再退到时间+随机（避免可预测/自增 ID）。
function uuid(): string {
  try {
    const c = (typeof crypto !== 'undefined' ? crypto : undefined) as Crypto | undefined
    if (c?.randomUUID) return c.randomUUID()
    if (c?.getRandomValues) {
      const b = new Uint8Array(16); c.getRandomValues(b)
      b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80
      const h = Array.from(b, (x) => x.toString(16).padStart(2, '0'))
      return `${h[0]}${h[1]}${h[2]}${h[3]}-${h[4]}${h[5]}-${h[6]}${h[7]}-${h[8]}${h[9]}-${h[10]}${h[11]}${h[12]}${h[13]}${h[14]}${h[15]}`
    }
  } catch { /* noop */ }
  return 'c-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12)
}

// ── 模块级状态 ──
const conversations = ref<Conversation[]>([])
const activeId = ref('')
const asking = ref(false)
const draft = ref('')
const thinking = ref(false)            // 深度思考开关（逐问生效，与小程序对齐；后端 qwen3 思考流式）
const hotQuestions = ref<string[]>([])
let askSeq = 0                       // 竞态锁：停止/新提问/重试递增，作废在途流回调
let abortCtl: AbortController | null = null
let mid = Date.now()                 // 消息 id 计数（Date 种子，避开 load 后旧 id 冲突）

/** 当前激活会话（无则新建一个）。 */
function ensureActive(): Conversation {
  let c = conversations.value.find((x) => x.id === activeId.value)
  if (!c) {
    c = reactive({ id: uuid(), title: '新对话', messages: [], qaSession: '', updatedAt: Date.now() })
    conversations.value.unshift(c)   // 最新在前
    activeId.value = c.id
  }
  return c
}

// 当前会话的 messages（组件读这个；ask/retry/stop 推到激活会话的数组里）。
const messages = computed<ChatMessage[]>(() => conversations.value.find((c) => c.id === activeId.value)?.messages ?? [])

const LV: Record<Level, string> = { high: '高', mid: '中', low: '低' }

function mapSources(sources: any[]): SourceRow[] {
  return (sources || []).map((s, i) => {
    // 优先服务端 level；缺省时按 weighted 融合阈值兜底（rerank 后量纲是 0-1，故只兜底不重算）。
    const level: Level = (s.level === 'high' || s.level === 'mid' || s.level === 'low')
      ? s.level
      : (s.score >= 7.7 ? 'high' : s.score >= 5.8 ? 'mid' : 'low')
    return { idx: i + 1, title: s.title || s.doc_id || '', section: s.section || '', levelLabel: LV[level], level, score: Number(s.score) || 0, relevance: Number(s.relevance) || 0, preview: s.preview || '' }
  })
}

// 流式"匀速吐字"泵：把 bursty 的网络到达解耦成屏幕上的匀速显现——已收到的 raw 入缓冲，rAF 以稳定
// 节奏推进 _shownLen 朝末尾追平（落后越多走越快、有上限），渲染节流到 ~30fps（省一半重排）。追平即停，
// 新 chunk 由 ensureReveal 重启；finishStream/stop 收尾时一次性渲染全文定稿（故尾部一两字的"补齐"无感）。
function _now(): number { return (typeof performance !== 'undefined' && performance.now) ? performance.now() : 0 }

// 通用"匀速吐字"通道：answer（raw→html）与 reasoning（reasoning→reasoningHtml）共用同一套墙钟节奏泵，
// 把 bursty 到达解耦成屏幕匀速显现。两者从不同时流式（思考先、答案后），共用逻辑只是渲染目标/状态键不同。
interface RevealCh {
  text: (ai: ChatMessage) => string                  // 源文本
  html: (ai: ChatMessage) => string | undefined      // 当前已渲染 html（判首帧）
  setHtml: (ai: ChatMessage, h: string) => void       // 写回渲染结果
  alive: (ai: ChatMessage) => boolean                 // 存活条件（停止则不再推进）
  raf: '_renderRaf' | '_rRaf'
  shown: '_shownLen' | '_rShownLen'
  ts: '_lastRenderTs' | '_rTs'
}
const ANSWER_CH: RevealCh = {
  text: (ai) => stripImg(ai.raw || ''),
  html: (ai) => ai.html,
  setHtml: (ai, h) => { ai.html = h },
  alive: (ai) => !ai.viewBlocks && !ai.error && !ai.noResult,
  raf: '_renderRaf', shown: '_shownLen', ts: '_lastRenderTs',
}
const REASON_CH: RevealCh = {
  text: (ai) => ai.reasoning || '',
  html: (ai) => ai.reasoningHtml,
  setHtml: (ai, h) => { ai.reasoningHtml = h },
  alive: (ai) => !ai._reasoningDone,
  raf: '_rRaf', shown: '_rShownLen', ts: '_rTs',
}
function pumpTick(ai: ChatMessage, seq: number, ch: RevealCh): void {
  ;(ai as any)[ch.raf] = null
  if (seq !== askSeq || !ch.alive(ai)) return            // 作废/定稿/错误/无结果 → 停
  const target = ch.text(ai).length
  const shown = ((ai as any)[ch.shown] as number) || 0
  if (shown >= target) return                            // 追平即停；新帧由 ensureReveal 重启
  const now = _now()
  const lastTs = (ai as any)[ch.ts] as number | undefined
  const since = lastTs ? (now - lastTs) : 24
  if (lastTs && since < 24) {                            // 重排节流 ≤~40fps（与刷新率/headless 无关）
    ;(ai as any)[ch.raf] = requestAnimationFrame(() => pumpTick(ai, seq, ch))
    return
  }
  // 按【真实时间】推进：~200ms 时间常数内追平当前积压（比例显现）；dt 封顶 40ms，使长停顿后不会一帧
  // 吐完（"一卡一卡"的根因）；步长有下限/上限 → 任何刷新率下都匀速。
  const dt = Math.min(40, since)
  const remain = target - shown
  const next = shown + Math.max(2, Math.min(remain, Math.ceil(remain * (dt / 200))))
  ;(ai as any)[ch.shown] = next
  ;(ai as any)[ch.ts] = now
  ch.setHtml(ai, renderMd(ch.text(ai).slice(0, next)))
  ;(ai as any)[ch.raf] = requestAnimationFrame(() => pumpTick(ai, seq, ch))
}
// 帧到达即确保泵在跑。首帧立即同步渲染（内容尽快出现 + 保证 html 是字符串），其后增量交给匀速泵；
// 无 rAF 环境（SSR/测试）退化为即时全量渲染（与收尾口径一致）。默认 answer 通道。
function ensureReveal(ai: ChatMessage, seq: number, ch: RevealCh = ANSWER_CH): void {
  if (!ch.alive(ai)) return
  if (ch.html(ai) == null || typeof requestAnimationFrame !== 'function') {
    const full = ch.text(ai)
    ;(ai as any)[ch.shown] = full.length
    ch.setHtml(ai, renderMd(full))
    if (typeof requestAnimationFrame !== 'function') return
  }
  if ((ai as any)[ch.raf] != null) return                // 已在跑
  ;(ai as any)[ch.raf] = requestAnimationFrame(() => pumpTick(ai, seq, ch))
}
// 思考定稿：答案开始或收尾时停思考泵、渲染全文、自动收起披露条。
function finalizeReasoning(ai: ChatMessage, collapse = true): void {
  if (ai._rRaf != null) { cancelAnimationFrame(ai._rRaf); ai._rRaf = null }
  if (!ai.reasoning || ai._reasoningDone) return
  ai._reasoningDone = true
  ai.reasoningHtml = renderMd(ai.reasoning)
  if (ai._reasoningT0 != null && ai.reasoningMs == null) ai.reasoningMs = Math.round(_now() - ai._reasoningT0)
  if (collapse) ai.reasoningOpen = false
}

function onEvent(conv: Conversation, ai: ChatMessage, ev: SseEvent, seq: number): void {
  if (seq !== askSeq) return
  switch (ev.type) {
    case 'session':
      ai.messageId = (ev.message_id as string) || ''
      if (ev.session_id) conv.qaSession = ev.session_id as string
      break
    case 'sources':
      ai.sources = mapSources(ev.sources as any[])
      ai.sourcesOpen = false
      // 有据等待态：检索完成（总在首个答案 token 之前到达）即把"找到了什么"如实显出，
      // 替代盲目计时的「正在生成回答」。深度思考时此窗口更长，预览价值更大。
      if (ai.loading && ai.sources.length) {
        if (ai._stageTimer) { clearTimeout(ai._stageTimer); ai._stageTimer = null }
        ai.stageText = `已找到 ${ai.sources.length} 篇相关资料，正在${ai._thinking ? '深度思考并' : ''}作答…`
      }
      break
    case 'reasoning':
      // 深度思考过程（thinking + RAG_STREAM_REASONING 开；在答案 chunk 之前到达）。披露条接管等待态，
      // 思考中默认展开，文本经思考通道匀速显现。
      if (ai._stageTimer) { clearTimeout(ai._stageTimer); ai._stageTimer = null }
      if (ai._reasoningT0 == null) ai._reasoningT0 = _now()
      ai.loading = false
      ai.reasoning = (ai.reasoning || '') + ((ev.content as string) || '')
      if (ai.reasoningOpen == null) ai.reasoningOpen = true
      ensureReveal(ai, seq, REASON_CH)
      break
    case 'chunk':
      if (ai._stageTimer) { clearTimeout(ai._stageTimer); ai._stageTimer = null }
      ai.loading = false
      if (ai.reasoning && !ai._reasoningDone) finalizeReasoning(ai)   // 答案开始 → 思考定稿并收起
      ai.raw = (ai.raw || '') + ((ev.content as string) || '')
      if (!ai.viewBlocks) ensureReveal(ai, seq)   // 匀速吐字泵（解耦 bursty 到达 → 屏幕匀速显现）
      break
    case 'done':
      ai.guard = !!ev.guard
      if (ev.no_result) {
        ai.noResult = true
        ai.answer = stripImg(ai.raw || '') || NO_RESULT_FALLBACK
        ai.rephrase = (ev.rephrase as string[]) || []
      }
      break
    case 'content_blocks':
      // 图片只能全文定稿后发；位置在 done 之后、[DONE] 之前。原始格式：
      // text {type:'markdown',content} / image {type:'image',title,url,oss_key,caption}
      ai.viewBlocks = ((ev.content_blocks as any[]) || []).map((b) =>
        b.type === 'image'
          ? { type: 'image', url: b.url, oss_key: b.oss_key, caption: b.caption || '', alt: b.caption || '', failed: false, reloading: false } as ViewBlock
          : { type: 'text', html: renderMd(b.content || '') } as ViewBlock,
      )
      ai.copyText = ((ev.content_blocks as any[]) || [])
        .filter((b) => b.type !== 'image').map((b) => b.content || '').join('\n') || stripImg(ai.raw || '')
      break
    case 'error':
      // 流内错误帧（替代 done）：HTTP 200 已发出，错误只能作为帧下发。
      if (ai._stageTimer) { clearTimeout(ai._stageTimer); ai._stageTimer = null }
      ai.loading = false
      ai.streaming = false
      finalizeReasoning(ai, false)
      ai.error = true
      ai.errorText = (ev.message as string) || '回答生成失败，请重试。'
      break
    // '__done' 及未知类型：忽略（finishStream 在 reader 结束时收尾）
  }
}

function finishStream(ai: ChatMessage, seq: number): void {
  if (seq !== askSeq) return
  asking.value = false
  abortCtl = null
  if (ai._stageTimer) { clearTimeout(ai._stageTimer); ai._stageTimer = null }
  if (ai._renderRaf != null) { cancelAnimationFrame(ai._renderRaf); ai._renderRaf = null }
  finalizeReasoning(ai, false)   // 思考定稿（若有）：停泵 + 渲染全文，保留展开态
  ai.loading = false
  ai.streaming = false
  if (ai.noResult || ai.error) return
  if (!ai.raw && !ai.viewBlocks) { ai.error = true; ai.errorText = '回答为空，请重试。'; return }
  if (!ai.viewBlocks) { ai.html = renderMd(stripImg(ai.raw)); ai.copyText = stripImg(ai.raw) }
}

async function ask(preset?: string, skipUser = false): Promise<void> {
  const text = ((preset != null ? preset : draft.value) || '').trim()
  if (!text || asking.value) return
  if (preset == null) draft.value = ''
  const conv = ensureActive()
  if (!skipUser) conv.messages.push({ id: 'u' + (++mid), role: 'user', text })
  if (conv.title === '新对话' && text) conv.title = text.slice(0, 24)   // 标题取首问
  conv.updatedAt = Date.now()

  const ai: ChatMessage = reactive({
    id: 'a' + (++mid), role: 'ai', loading: true, stageText: '正在检索知识库…',
    question: text, sourcesOpen: false, voted: '', viewBlocks: null,
    streaming: true, _thinking: thinking.value,
  })
  conv.messages.push(ai)
  asking.value = true

  const seq = ++askSeq
  // 等待态文案由真实流帧驱动（sources 帧 → 有据态；chunk 帧 → 收起）——不再盲目计时翻页。
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null
  abortCtl = ctl

  const body: Record<string, unknown> = { question: text }
  if (conv.qaSession) body.session_id = conv.qaSession
  body.conversation_id = conv.id   // 客户端会话 ID → 服务端按此归并历史（仅 RAG_CONVERSATION_HISTORY 开时落库）
  if (thinking.value) body.thinking = true   // 深度思考（仅 true 时带，避免覆盖服务端默认）

  try {
    // apiFetch：自动 Bearer（部门过滤需要）+ 首帧 401 自动重登重试一次（流未消费，可干净重发）。
    const res = await apiFetch('/api/ask/stream', { method: 'POST', body: JSON.stringify(body), signal: ctl?.signal })
    if (!res.ok) {
      const t = await res.text().catch(() => '')
      const e: any = new Error(t || `HTTP ${res.status}`); e.status = res.status; throw e
    }
    if (!res.body || !res.body.getReader) throw new Error('浏览器不支持流式读取')

    const reader = res.body.getReader()
    const dec = createSseDecoder()
    for (;;) {
      const { value, done } = await reader.read()
      if (seq !== askSeq) { try { reader.cancel() } catch { /* noop */ } if (ai._stageTimer) clearTimeout(ai._stageTimer); return }
      if (done) {
        for (const ev of dec.flush()) onEvent(conv, ai, ev, seq)
        finishStream(ai, seq)
        break
      }
      for (const ev of dec.push(value!)) onEvent(conv, ai, ev, seq)
    }
  } catch (e: any) {
    if (seq !== askSeq) return   // 已被停止/新提问接管
    asking.value = false
    abortCtl = null
    if (ai._stageTimer) { clearTimeout(ai._stageTimer); ai._stageTimer = null }
    finalizeReasoning(ai, false)
    ai.loading = false
    ai.streaming = false
    ai.error = true
    ai.errorText = e && e.name === 'AbortError' ? '已取消本次提问。' : '回答失败，请检查网络后重试。'
  }
}

function stop(): void {
  askSeq++   // 作废在途流回调
  if (abortCtl) { try { abortCtl.abort() } catch { /* noop */ } abortCtl = null }
  asking.value = false
  const ai = messages.value[messages.value.length - 1]
  if (ai && ai.role === 'ai') {
    if (ai._stageTimer) { clearTimeout(ai._stageTimer); ai._stageTimer = null }
    if (ai._renderRaf != null) { cancelAnimationFrame(ai._renderRaf); ai._renderRaf = null }   // 停吐字泵
    finalizeReasoning(ai, false)
    ai.loading = false
    ai.streaming = false
    if (ai.raw && !ai.viewBlocks) ai.html = renderMd(stripImg(ai.raw))   // 保留已生成部分（一次性定稿）
    else if (!ai.raw && !ai.viewBlocks) { ai.error = true; ai.errorText = '已取消本次提问。' }
  }
}

function retry(m: ChatMessage): void {
  const idx = messages.value.indexOf(m)
  if (idx >= 0) messages.value.splice(idx, 1)   // 原位移除错误卡，保留用户问句重发
  void ask(m.question, true)
}

/** 新会话：作废在途流，新建并切换到一条空会话（下次提问重建服务端会话）。 */
function newConversation(): void {
  if (asking.value) stop()
  draft.value = ''
  const c: Conversation = reactive({ id: uuid(), title: '新对话', messages: [], qaSession: '', updatedAt: Date.now() })
  conversations.value.unshift(c)
  activeId.value = c.id
}
const resetThread = newConversation   // 旧名兼容

/** 切到某条历史会话；服务端回灌的占位会按需拉取消息。 */
function switchTo(id: string): void {
  if (id === activeId.value) return
  if (asking.value) stop()
  draft.value = ''
  const c = conversations.value.find((x) => x.id === id)
  if (!c) return
  activeId.value = id
  if (c.messages.length === 0) void loadConversationMessages(c)   // 空（含服务端占位）→ 按需拉取
}

/** 删除某条会话；若删的是当前会话则切到最近一条（无则留空，下次提问自建）。
 *  同时 best-effort 服务端软删除（端点未启用则忽略，本地照常移除）。 */
function removeConversation(id: string): void {
  const i = conversations.value.findIndex((c) => c.id === id)
  if (i < 0) return
  if (id === activeId.value && asking.value) stop()
  conversations.value.splice(i, 1)
  if (activeId.value === id) activeId.value = conversations.value[0]?.id || ''
  void apiJson(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE', auth: true }).catch(() => {})
}

/** 按标题/消息文本搜索会话（用于侧栏搜索框）。空会话（未提问）不进列表，避免噪声。 */
function searchConversations(q: string): Conversation[] {
  const k = q.trim().toLowerCase()
  const list = [...conversations.value]
    .filter((c) => c.messages.length > 0 || c._server)   // 服务端占位（标题先到、消息未拉）也展示
    .sort((a, b) => b.updatedAt - a.updatedAt)
  if (!k) return list
  return list.filter((c) =>
    c.title.toLowerCase().includes(k) ||
    c.messages.some((m) => (m.text || m.raw || m.answer || '').toLowerCase().includes(k)))
}

async function vote(m: ChatMessage, type: 'upvote' | 'downvote'): Promise<void> {
  if (m.voted || !m.messageId) return
  m.voted = type === 'upvote' ? 'up' : 'down'   // 乐观置态
  try {
    await apiJson('/api/feedback', { method: 'POST', auth: true, body: JSON.stringify({ message_id: m.messageId, feedback_type: type }) })
  } catch { m.voted = '' }   // 回滚
}

async function handoff(m: ChatMessage): Promise<void> {
  if (m.handoffDone || !m.messageId) return
  try {
    await apiJson('/api/feedback', { method: 'POST', auth: true, body: JSON.stringify({ message_id: m.messageId, feedback_type: 'handoff' }) })
    m.handoffDone = true
  } catch { /* 失败保持可重试 */ }
}

function copyAns(m: ChatMessage): void {
  const txt = m.copyText || m.answer || ''
  const done = () => { m.copied = true; setTimeout(() => { m.copied = false }, 1500) }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(done, done)
  } else {
    try {
      const ta = document.createElement('textarea')
      ta.value = txt; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta)
    } catch { /* noop */ }
    done()
  }
}

async function resignImage(m: ChatMessage, bi: number): Promise<void> {
  const b = m.viewBlocks?.[bi]
  if (!b || b.reloading) return
  if (!b.oss_key) { b.failed = false; return }
  b.reloading = true
  try {
    const r = await apiJson<{ urls: Record<string, string> }>('/api/resign-images', {
      method: 'POST', auth: true, body: JSON.stringify({ oss_keys: [b.oss_key] }),
    })
    const u = (r.urls || {})[b.oss_key]
    b.reloading = false
    if (u) { b.url = u; b.failed = false }
  } catch { b.reloading = false }
}

function imgFailed(m: ChatMessage, bi: number): void {
  const b = m.viewBlocks?.[bi]
  if (b) b.failed = true
}

function preview(b: ViewBlock): void {
  if (b && b.url) { try { window.open(b.url, '_blank', 'noopener') } catch { /* noop */ } }
}

function fillInput(t: string): void { draft.value = t }

async function loadHotQuestions(): Promise<void> {
  const fb = ['U8+ 如何登录？', '请假流程是什么？', '访客 WiFi 密码是多少？']
  try {
    const r = await apiJson<{ questions: string[] }>('/api/hot-questions', { auth: false })
    hotQuestions.value = (r && r.questions && r.questions.length) ? r.questions : fb
  } catch { hotQuestions.value = fb }
}

// ── localStorage 持久化（防御式：失败不影响功能；debounce 防流式期间狂写）──
const LS_KEY = 'fl-conversations'

function persist(): void {
  try {
    let uid = ''
    try { uid = useSession().identity?.userId || '' } catch { /* pinia 未就绪 */ }
    const data = conversations.value.filter((c) => c.messages.length > 0).slice(0, 30).map((c) => ({
      id: c.id, title: c.title, updatedAt: c.updatedAt,
      // 丢 _stageTimer（计时器句柄）、loading（reload 后无在途流）。
      messages: c.messages.map((m) => { const { _stageTimer, _renderRaf, _shownLen, _lastRenderTs, _rRaf, _rShownLen, _rTs, _reasoningDone, _reasoningT0, loading, streaming, _thinking, ...rest } = m as any; return rest }),
    }))
    // uid 戳：登录后 syncHistoryForUser 据此判断本地缓存是否属于当前用户（共享设备防残留）。
    localStorage.setItem(LS_KEY, JSON.stringify({ uid, activeId: activeId.value, conversations: data }))
  } catch { /* 隐私模式/超额忽略 */ }
}

/** 拿到权威身份后调用：若本地缓存属于【其他】用户（或旧版无 uid 戳），清空本地会话历史。
 *  共享钉钉 PC / kiosk 上 token 仅在内存、localStorage 却跨用户残留——上一个人的部门内部
 *  答案与来源摘录会被下一个人 loadPersisted 还原。无条件清，再 ensureActive 起一个空会话。 */
export function syncHistoryForUser(uid: string): void {
  if (typeof window === 'undefined') return
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return
    let storedUid = ''
    try { storedUid = (JSON.parse(raw) || {}).uid || '' } catch { storedUid = '' }
    if (storedUid === (uid || '')) return   // 同一用户：保留本地历史
    localStorage.removeItem(LS_KEY)
    conversations.value = []
    activeId.value = ''
    ensureActive()
  } catch { /* 失败不影响功能 */ }
}

function loadPersisted(): void {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return
    const d = JSON.parse(raw)
    if (!d || !Array.isArray(d.conversations)) return
    conversations.value = d.conversations.map((c: any) => reactive({
      id: c.id || uuid(),
      title: c.title || '新对话',
      qaSession: '',   // 服务端会话已失效，下次提问重建
      updatedAt: c.updatedAt || Date.now(),
      messages: (c.messages || []).map((m: any) => reactive({ ...m, loading: false, _stageTimer: null })),
    }))
    activeId.value = (d.activeId && conversations.value.some((c) => c.id === d.activeId))
      ? d.activeId : (conversations.value[0]?.id || '')
  } catch { /* 损坏数据忽略 */ }
}

let _persistTimer: ReturnType<typeof setTimeout> | null = null
function schedulePersist(): void {
  if (_persistTimer) clearTimeout(_persistTimer)
  _persistTimer = setTimeout(persist, 400)
}

// 模块初始化：从 localStorage 恢复 + 建立持久化 watch（仅浏览器环境）。
if (typeof window !== 'undefined') {
  loadPersisted()
  watch([conversations, activeId], schedulePersist, { deep: true })
}

// ── 服务端会话历史（Phase 2/3）：端点 gate 在 RAG_CONVERSATION_HISTORY，关时返回空 → 全部退回 localStorage ──
interface ServerConv { conversation_id: string; title: string; updated_at: string }
interface ServerMsg { message_id: string; question: string; answer: string; blocks: ViewBlock[]; created_at: string; status: string }

// 服务端一条问答 → [用户气泡, AI 消息]（与 onEvent/finishStream 的渲染口径一致）。
function serverItemToMessages(it: ServerMsg): ChatMessage[] {
  const u: ChatMessage = { id: 'u' + (++mid), role: 'user', text: it.question }
  const a: ChatMessage = reactive({
    id: 'a' + (++mid), role: 'ai', question: it.question, messageId: it.message_id,
    sourcesOpen: false, voted: '', viewBlocks: null,
  })
  if (it.status === 'NO_RESULT') {
    a.noResult = true
    a.answer = it.answer || NO_RESULT_FALLBACK
  } else if (it.blocks && it.blocks.length) {
    a.viewBlocks = (it.blocks as any[]).map((b) =>
      b.type === 'image'
        ? { type: 'image', url: b.url, oss_key: b.oss_key, caption: b.caption || '', alt: b.caption || '', failed: false, reloading: false } as ViewBlock
        : { type: 'text', html: renderMd(b.content || '') } as ViewBlock)
    a.copyText = it.answer || ''
  } else {
    a.html = renderMd(stripImg(it.answer || ''))
    a.copyText = it.answer || ''
  }
  return [u, a]
}

/** 登录后拉服务端会话列表，把本地没有的并进侧栏（占位：标题先到，消息点开再拉）。best-effort。 */
async function hydrateConversations(): Promise<void> {
  try {
    const r = await apiJson<{ items: ServerConv[] }>('/api/conversations', { auth: true })
    for (const sc of (r.items || [])) {
      if (!sc.conversation_id || conversations.value.some((c) => c.id === sc.conversation_id)) continue
      conversations.value.push(reactive({
        id: sc.conversation_id, title: sc.title || '历史会话', messages: [],
        qaSession: '', updatedAt: Date.parse(sc.updated_at) || Date.now(), _server: true,
      }))
    }
  } catch { /* 端点未启用/失败 → 仅 localStorage */ }
}

/** 点开某会话时按需拉其消息（仅当本地为空）。best-effort。 */
async function loadConversationMessages(c: Conversation): Promise<void> {
  if (c._loading || c.messages.length > 0) return
  c._loading = true
  try {
    const r = await apiJson<{ items: ServerMsg[] }>(`/api/conversations/${encodeURIComponent(c.id)}`, { auth: true })
    if (c.messages.length === 0 && r.items && r.items.length) {
      const msgs: ChatMessage[] = []
      for (const it of r.items) msgs.push(...serverItemToMessages(it))
      c.messages = msgs
    }
  } catch { /* noop */ } finally { c._loading = false }
}

export function useAsk() {
  return {
    messages, asking, draft, thinking, hotQuestions,
    conversations, activeId,
    ask, stop, retry, resetThread, newConversation, switchTo, removeConversation, searchConversations,
    vote, handoff, copyAns, resignImage, imgFailed, preview, fillInput, loadHotQuestions, hydrateConversations,
  }
}

/** 仅供测试：重置单例状态。 */
export function __resetAsk(): void {
  conversations.value = []
  activeId.value = ''
  asking.value = false
  draft.value = ''
  thinking.value = false
  hotQuestions.value = []
  askSeq = 0
  abortCtl = null
  if (_persistTimer) { clearTimeout(_persistTimer); _persistTimer = null }
}
