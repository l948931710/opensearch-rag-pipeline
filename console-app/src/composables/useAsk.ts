import { reactive, ref } from 'vue'
import { apiFetch, apiJson } from '@/lib/api'
import { createSseDecoder, type SseEvent } from '@/lib/sseDecoder'
import { renderMd, stripImg } from '@/lib/markdown'

// 问答线程的单一事实来源（模块级单例，等同轻量 store）。一条线程、进程内、内存态——
// 切到管理页再回来不丢；与服务端会话历史由 qaSession 关联。

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

// ── 模块级状态 ──
const messages = ref<ChatMessage[]>([])
const asking = ref(false)
const draft = ref('')
const hotQuestions = ref<string[]>([])
let qaSession = ''
let askSeq = 0                       // 竞态锁：停止/新提问/重试递增，作废在途流回调
let abortCtl: AbortController | null = null
let mid = 0

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

function onEvent(ai: ChatMessage, ev: SseEvent, seq: number): void {
  if (seq !== askSeq) return
  switch (ev.type) {
    case 'session':
      ai.messageId = (ev.message_id as string) || ''
      if (ev.session_id) qaSession = ev.session_id as string
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
  if (!skipUser) messages.value.push({ id: 'u' + (++mid), role: 'user', text })

  const ai: ChatMessage = reactive({
    id: 'a' + (++mid), role: 'ai', loading: true, stageText: '正在检索知识库…',
    question: text, sourcesOpen: false, voted: '', viewBlocks: null,
  })
  messages.value.push(ai)
  asking.value = true

  const seq = ++askSeq
  ai._stageTimer = setTimeout(() => { if (ai.loading) ai.stageText = '正在生成回答…' }, 2200)
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null
  abortCtl = ctl

  const body: Record<string, unknown> = { question: text }
  if (qaSession) body.session_id = qaSession

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
        for (const ev of dec.flush()) onEvent(ai, ev, seq)
        finishStream(ai, seq)
        break
      }
      for (const ev of dec.push(value!)) onEvent(ai, ev, seq)
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

/** 新会话：作废在途流、清空线程与服务端会话关联（下次提问重新建会话）。 */
function resetThread(): void {
  if (asking.value) stop()
  messages.value = []
  draft.value = ''
  qaSession = ''
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

export function useAsk() {
  return {
    messages, asking, draft, hotQuestions,
    ask, stop, retry, resetThread, vote, handoff, copyAns, resignImage, imgFailed, preview, fillInput, loadHotQuestions,
  }
}

/** 仅供测试：重置线程单例状态。 */
export function __resetAsk(): void {
  messages.value = []
  asking.value = false
  draft.value = ''
  hotQuestions.value = []
  qaSession = ''
  askSeq = 0
  abortCtl = null
  mid = 0
}
