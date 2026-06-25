import { computed, reactive, ref, watch } from 'vue'
import { apiFetch, apiJson } from '@/lib/api'
import { createSseDecoder, type SseEvent } from '@/lib/sseDecoder'
import { renderMd, stripImg } from '@/lib/markdown'

// 问答单一事实来源（模块级单例，等同轻量 store）。多会话（Atlas 式）：每条会话独立 messages +
// 服务端 qaSession；新建/切换/删除/搜索；localStorage 持久化（reload 仍在，故有会话历史）。

const NO_RESULT_FALLBACK = '抱歉，当前知识库中未找到相关信息。'

export type Level = 'high' | 'mid' | 'low'

export interface SourceRow { idx: number; title: string; section: string; levelLabel: string; level: Level }

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
  _stageTimer?: ReturnType<typeof setTimeout> | null
}

export interface Conversation {
  id: string
  title: string            // 取首条用户问句；未提问前为「新对话」
  messages: ChatMessage[]
  qaSession: string        // 服务端会话关联（reload 后失效，下次提问重建）
  updatedAt: number
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
let cid = Date.now()                 // 会话 id 计数

/** 当前激活会话（无则新建一个）。 */
function ensureActive(): Conversation {
  let c = conversations.value.find((x) => x.id === activeId.value)
  if (!c) {
    c = reactive({ id: 'c' + (++cid), title: '新对话', messages: [], qaSession: '', updatedAt: Date.now() })
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
    return { idx: i + 1, title: s.title || s.doc_id || '', section: s.section || '', levelLabel: LV[level], level }
  })
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
      break
    case 'chunk':
      if (ai._stageTimer) { clearTimeout(ai._stageTimer); ai._stageTimer = null }
      ai.loading = false
      ai.raw = (ai.raw || '') + ((ev.content as string) || '')
      if (!ai.viewBlocks) ai.html = renderMd(stripImg(ai.raw))   // 逐 token 实时打字
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
  ai.loading = false
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
  })
  conv.messages.push(ai)
  asking.value = true

  const seq = ++askSeq
  ai._stageTimer = setTimeout(() => { if (ai.loading) ai.stageText = '正在生成回答…' }, 2200)
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null
  abortCtl = ctl

  const body: Record<string, unknown> = { question: text }
  if (conv.qaSession) body.session_id = conv.qaSession
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
    ai.loading = false
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
    ai.loading = false
    if (ai.raw && !ai.viewBlocks) ai.html = renderMd(stripImg(ai.raw))   // 保留已生成部分（不算错误）
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
  const c: Conversation = reactive({ id: 'c' + (++cid), title: '新对话', messages: [], qaSession: '', updatedAt: Date.now() })
  conversations.value.unshift(c)
  activeId.value = c.id
}
const resetThread = newConversation   // 旧名兼容

/** 切到某条历史会话。 */
function switchTo(id: string): void {
  if (id === activeId.value) return
  if (asking.value) stop()
  draft.value = ''
  if (conversations.value.some((c) => c.id === id)) activeId.value = id
}

/** 删除某条会话；若删的是当前会话则切到最近一条（无则留空，下次提问自建）。 */
function removeConversation(id: string): void {
  const i = conversations.value.findIndex((c) => c.id === id)
  if (i < 0) return
  if (id === activeId.value && asking.value) stop()
  conversations.value.splice(i, 1)
  if (activeId.value === id) activeId.value = conversations.value[0]?.id || ''
}

/** 按标题/消息文本搜索会话（用于侧栏搜索框）。空会话（未提问）不进列表，避免噪声。 */
function searchConversations(q: string): Conversation[] {
  const k = q.trim().toLowerCase()
  const list = [...conversations.value]
    .filter((c) => c.messages.length > 0)
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
    const data = conversations.value.slice(0, 30).map((c) => ({
      id: c.id, title: c.title, updatedAt: c.updatedAt,
      // 丢 _stageTimer（计时器句柄）、loading（reload 后无在途流）。
      messages: c.messages.map((m) => { const { _stageTimer, loading, ...rest } = m as any; return rest }),
    }))
    localStorage.setItem(LS_KEY, JSON.stringify({ activeId: activeId.value, conversations: data }))
  } catch { /* 隐私模式/超额忽略 */ }
}

function loadPersisted(): void {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return
    const d = JSON.parse(raw)
    if (!d || !Array.isArray(d.conversations)) return
    conversations.value = d.conversations.map((c: any) => reactive({
      id: c.id || 'c' + (++cid),
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

export function useAsk() {
  return {
    messages, asking, draft, thinking, hotQuestions,
    conversations, activeId,
    ask, stop, retry, resetThread, newConversation, switchTo, removeConversation, searchConversations,
    vote, handoff, copyAns, resignImage, imgFailed, preview, fillInput, loadHotQuestions,
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
