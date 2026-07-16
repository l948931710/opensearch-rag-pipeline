import { computed, reactive, ref } from 'vue'
import { apiFetch, apiJson } from '@/lib/api'
import { createSseDecoder, type SseEvent } from '@/lib/sseDecoder'
import { renderMd, stripImg } from '@/lib/markdown'
import { newPumpState, renderMdIncr, stripImgIncr, type PumpChState } from '@/lib/mdIncr'
import { useSession } from '@/stores/session'
import { __resetIdentityScope, identityFingerprint, registerIdentityScopedStore } from '@/composables/identityScope'

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
  loaded?: boolean    // 图片已完成加载（Perf-6：加载前 figure 撑占位高度，防定稿后弹入抖动）
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
  // 流式双节点拆分（Perf-5）：定稿前缀 / 推进尾段各占一个 v-html 节点——此前每 tick 整段
  // innerHTML 替换，md 解析已增量化但 DOM 仍全量重建。拆分后每帧只有尾段节点变（stable
  // 字符串不变 → Vue 跳过 patch）。仅流式期存在；定稿/停止/图文帧后清空回单节点权威渲染。
  _htmlStable?: string
  _htmlTail?: string
  viewBlocks?: ViewBlock[] | null   // 图文定稿（content_blocks 帧后）
  copyText?: string
  messageId?: string       // 反馈关联键（来自 session 帧）
  sources?: SourceRow[]
  sourcesOpen?: boolean
  guard?: boolean          // 低置信提示
  noResult?: boolean
  answer?: string          // no_result 文案
  rephrase?: string[]      // no_result 改写建议
  suggestTitles?: string[] // no_result「您是不是想问」相近文档标题（done 帧 suggest_titles，
                           // 与 rephrase 后端二选一下发；通用能力分级开放引导式拒答）
  source?: string          // 回答来源 kb|smalltalk|general|guard（done 帧 source；
                           // general/smalltalk 时正常答案区显示「通用回答·非公司口径」徽标）
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
  agent?: AgentMsgMeta     // Agent canary：本条 AI 消息走 /api/agent/ask（useAgentAsk 写入/消费；旧路径恒不置）
}

/** Agent 消息元数据（结构定义放这里避免 useAsk↔useAgentAsk 循环依赖；会随消息持久化，
 *  reload 后挂起卡按 runId 轮询恢复）。所有字段由 useAgentAsk 写入，useAsk 不读不写。 */
export interface AgentMsgMeta {
  runId?: string
  status?: string          // running/suspended/resuming/succeeded/failed/cancelled/expired（流帧+轮询回写）
  stages?: { key: string; label: string; at: number }[]   // 阶段化状态条（只映射真实事件，at=Date.now()）
  tools?: { callId: string; toolName: string; args?: Record<string, unknown> | null; status?: string; elapsedMs?: number }[]
  approval?: { requestId: string; checkpointId?: string; toolName?: string; args?: Record<string, unknown> | null } | null
  messageId?: string       // done 后才提升为 m.messageId（挂起/在途不出反馈条）
  disconnected?: boolean   // 实时流被停止/断开但 run 仍在服务端运行（轮询兜底）
  gotTerminal?: boolean    // 批次2（P0-03f）：收到过终局帧（done/approval/error）——
                           // clean EOF 而无终局帧=传输被切，绝不把 partial 当定稿
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

export function mapSources(sources: any[]): SourceRow[] {
  return (sources || []).map((s, i) => {
    // 优先服务端 level；缺省时按 weighted 融合阈值兜底（rerank 后量纲是 0-1，故只兜底不重算）。
    const level: Level = (s.level === 'high' || s.level === 'mid' || s.level === 'low')
      ? s.level
      : (s.score >= 7.7 ? 'high' : s.score >= 5.8 ? 'mid' : 'low')
    return { idx: i + 1, title: s.title || s.doc_id || '', section: s.section || '', levelLabel: LV[level], level, score: Number(s.score) || 0, relevance: Number(s.relevance) || 0, preview: s.preview || '' }
  })
}

/** content_blocks 帧 → ViewBlock[]（旧 RAG 与 agent transport 共用同一映射，勿分叉）。 */
export function mapViewBlocks(blocks: any[]): ViewBlock[] {
  return (blocks || []).map((b) =>
    b.type === 'image'
      ? { type: 'image', url: b.url, oss_key: b.oss_key, caption: b.caption || '', alt: b.caption || '', failed: false, reloading: false } as ViewBlock
      : { type: 'text', html: renderMd(b.content || '') } as ViewBlock,
  )
}

// 流式"匀速吐字"泵：把 bursty 的网络到达解耦成屏幕上的匀速显现——已收到的 raw 入缓冲，rAF 以稳定
// 节奏推进 _shownLen 朝末尾追平（落后越多走越快、有上限），渲染节流到 ~30fps（省一半重排）。追平即停，
// 新 chunk 由 ensureReveal 重启；finishStream/stop 收尾时一次性渲染全文定稿（故尾部一两字的"补齐"无感）。
function _now(): number { return (typeof performance !== 'undefined' && performance.now) ? performance.now() : 0 }

// 通用"匀速吐字"通道：answer（raw→html）与 reasoning（reasoning→reasoningHtml）共用同一套墙钟节奏泵，
// 把 bursty 到达解耦成屏幕匀速显现。两者从不同时流式（思考先、答案后），共用逻辑只是渲染目标/状态键不同。
interface RevealCh {
  key: 'a' | 'r'                                      // 流式增量缓存槽位（answer / reasoning）
  text: (ai: ChatMessage) => string                  // 源文本
  html: (ai: ChatMessage) => string | undefined      // 当前已渲染 html（判首帧）
  setHtml: (ai: ChatMessage, h: string) => void       // 写回渲染结果
  alive: (ai: ChatMessage) => boolean                 // 存活条件（停止则不再推进）
  raf: '_renderRaf' | '_rRaf'
  shown: '_shownLen' | '_rShownLen'
  ts: '_lastRenderTs' | '_rTs'
}
const ANSWER_CH: RevealCh = {
  key: 'a',
  text: (ai) => stripImg(ai.raw || ''),
  html: (ai) => ai.html,
  setHtml: (ai, h) => { ai.html = h },
  alive: (ai) => !ai.viewBlocks && !ai.error && !ai.noResult,
  raf: '_renderRaf', shown: '_shownLen', ts: '_lastRenderTs',
}
const REASON_CH: RevealCh = {
  key: 'r',
  text: (ai) => ai.reasoning || '',
  html: (ai) => ai.reasoningHtml,
  setHtml: (ai, h) => { ai.reasoningHtml = h },
  alive: (ai) => !ai._reasoningDone,
  raf: '_rRaf', shown: '_rShownLen', ts: '_rTs',
}

// ── 流式渲染增量缓存（H#68）──
// 增量 strip/render 内核已抽到 @/lib/mdIncr（perf 批次 B §6.2：Agent 流复用同一实现，
// 等价性契约不变）。缓存放 WeakMap（不上消息对象 → 绝不进 localStorage）。
const _pumpStates = new WeakMap<ChatMessage, Partial<Record<'a' | 'r', PumpChState>>>()
function _pumpState(ai: ChatMessage, key: 'a' | 'r'): PumpChState {
  let slots = _pumpStates.get(ai)
  if (!slots) { slots = {}; _pumpStates.set(ai, slots) }
  let st = slots[key]
  if (!st) { st = newPumpState(); slots[key] = st }
  return st
}
function _dropPumpState(ai: ChatMessage): void {
  _pumpStates.delete(ai)
  // 双节点拆分随泵一起退场：调用方随后必然写权威 ai.html（定稿/停止/历史加载），
  // 模板据 _htmlTail == null 切回单节点渲染。
  ai._htmlStable = undefined
  ai._htmlTail = undefined
}

function pumpTick(ai: ChatMessage, seq: number, ch: RevealCh): void {
  ;(ai as any)[ch.raf] = null
  if (seq !== askSeq || !ch.alive(ai)) return            // 作废/定稿/错误/无结果 → 停
  const st = _pumpState(ai, ch.key)
  const text = ch.key === 'a' ? stripImgIncr(st, ai.raw || '') : (ai.reasoning || '')
  const target = text.length
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
  const fullHtml = renderMdIncr(st, text.slice(0, next))
  ch.setHtml(ai, fullHtml)                               // ai.html 保持全量（滚动 watch/persist/兜底渲染读它）
  if (ch.key === 'a') {
    // 双节点拆分（Perf-5）：st.mdHtml = 已定稿行前缀（只增不改 → Vue 对 stable 节点跳过 patch），
    // 其余为推进中的尾段——每帧真正被 innerHTML 替换的只有这一小截。
    ai._htmlStable = st.mdHtml
    ai._htmlTail = fullHtml.slice(st.mdHtml.length)
  }
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
      // 通用能力分级开放：done 帧可带 source（回答来源徽标）；旧协议/flag 关缺省不置
      if (typeof ev.source === 'string') ai.source = ev.source
      if (ev.no_result) {
        ai.noResult = true
        ai.answer = stripImg(ai.raw || '') || NO_RESULT_FALLBACK
        ai.rephrase = (ev.rephrase as string[]) || []
        // 「您是不是想问」相近文档标题（与 rephrase 后端二选一下发）
        ai.suggestTitles = (ev.suggest_titles as string[]) || []
      }
      break
    case 'content_blocks':
      // 图片只能全文定稿后发；位置在 done 之后、[DONE] 之前。原始格式：
      // text {type:'markdown',content} / image {type:'image',title,url,oss_key,caption}
      ai.viewBlocks = mapViewBlocks(ev.content_blocks as any[])
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
  schedulePersist()              // 收尾持久化（deep watch 已移除）：定时器 400ms 后读到的是下面全部定稿态
  _dropPumpState(ai)             // 流式增量缓存到此为止（下面是全量权威定稿帧）
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
  schedulePersist()   // 提问即持久化（问句/标题/updatedAt）；流式中间态不逐帧写，收尾再持久化定稿

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
    _dropPumpState(ai)
    if (ai._stageTimer) { clearTimeout(ai._stageTimer); ai._stageTimer = null }
    finalizeReasoning(ai, false)
    ai.loading = false
    ai.streaming = false
    ai.error = true
    ai.errorText = e && e.name === 'AbortError' ? '已取消本次提问。' : '回答失败，请检查网络后重试。'
    schedulePersist()            // 错误态也入本地历史（与旧 deep watch 行为一致）
  }
}

function stop(): void {
  askSeq++   // 作废在途流回调
  if (abortCtl) { try { abortCtl.abort() } catch { /* noop */ } abortCtl = null }
  asking.value = false
  const ai = messages.value[messages.value.length - 1]
  if (ai && ai.role === 'ai') {
    _dropPumpState(ai)
    if (ai._stageTimer) { clearTimeout(ai._stageTimer); ai._stageTimer = null }
    if (ai._renderRaf != null) { cancelAnimationFrame(ai._renderRaf); ai._renderRaf = null }   // 停吐字泵
    finalizeReasoning(ai, false)
    ai.loading = false
    ai.streaming = false
    if (ai.raw && !ai.viewBlocks) ai.html = renderMd(stripImg(ai.raw))   // 保留已生成部分（一次性定稿）
    else if (!ai.raw && !ai.viewBlocks) { ai.error = true; ai.errorText = '已取消本次提问。' }
    schedulePersist()            // 手动停止后的半截答案/取消卡也入本地历史
  }
}

function retry(m: ChatMessage): void {
  const idx = messages.value.indexOf(m)
  if (idx >= 0) { messages.value.splice(idx, 1); schedulePersist() }   // 原位移除错误卡，保留用户问句重发
  void ask(m.question, true)
}

/** 新会话：作废在途流，新建并切换到一条空会话（下次提问重建服务端会话）。 */
function newConversation(): void {
  if (asking.value) stop()
  draft.value = ''
  const c: Conversation = reactive({ id: uuid(), title: '新对话', messages: [], qaSession: '', updatedAt: Date.now() })
  conversations.value.unshift(c)
  activeId.value = c.id
  schedulePersist()
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
  schedulePersist()
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
  schedulePersist()
  void apiJson(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE', auth: true }).catch(() => {})
}

// ── 侧栏会话列表 / 搜索（H#70）──
// 排序列表做成浅依赖 computed：只读 updatedAt / messages.length / _server，不触碰消息内容 →
// 流式 raw 每帧追加不再触发重排。
const sortedConversations = computed(() =>
  conversations.value
    .filter((c) => c.messages.length > 0 || c._server)   // 空会话（未提问）不进列表；服务端占位也展示
    .sort((a, b) => b.updatedAt - a.updatedAt))

// 全文搜索的 lowercase 结果缓存：按（标题+消息数+文本总长）做廉价失效键——raw 只追加、消息只增删，
// 键不变即复用，避免每次输入/每帧对全部会话重做 toLowerCase 大字符串分配。用 NUL(\u0000) 作拼接分隔符
// （搜索框输入不可能含 NUL，不会跨消息误匹配）。
const _searchTextCache = new WeakMap<Conversation, { key: string; text: string }>()
function convSearchText(c: Conversation): string {
  let total = 0
  for (const m of c.messages) total += (m.text || m.raw || m.answer || '').length
  const key = `${c.title}\u0000${c.messages.length}\u0000${total}`
  const hit = _searchTextCache.get(c)
  if (hit && hit.key === key) return hit.text
  const text = (c.title + '\u0000' + c.messages.map((m) => m.text || m.raw || m.answer || '').join('\u0000')).toLowerCase()
  _searchTextCache.set(c, { key, text })
  return text
}

/** 按标题/消息文本搜索会话（用于侧栏搜索框）。空会话（未提问）不进列表，避免噪声。 */
function searchConversations(q: string): Conversation[] {
  const k = q.trim().toLowerCase()
  const list = sortedConversations.value
  if (!k) return list
  return list.filter((c) => convSearchText(c).includes(k))
}

async function vote(m: ChatMessage, type: 'upvote' | 'downvote'): Promise<void> {
  if (m.voted || !m.messageId) return
  m.voted = type === 'upvote' ? 'up' : 'down'   // 乐观置态
  schedulePersist()
  try {
    await apiJson('/api/feedback', { method: 'POST', auth: true, body: JSON.stringify({ message_id: m.messageId, feedback_type: type }) })
  } catch { m.voted = ''; schedulePersist() }   // 回滚
}

async function handoff(m: ChatMessage): Promise<void> {
  if (m.handoffDone || !m.messageId) return
  try {
    await apiJson('/api/feedback', { method: 'POST', auth: true, body: JSON.stringify({ message_id: m.messageId, feedback_type: 'handoff' }) })
    m.handoffDone = true
    schedulePersist()
  } catch { /* 失败保持可重试 */ }
}

function copyAns(m: ChatMessage): void {
  const txt = m.copyText || m.answer || ''
  const done = () => { m.copied = true; schedulePersist(); setTimeout(() => { m.copied = false; schedulePersist() }, 1500) }
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
  schedulePersist()   // viewBlocks 的 url/failed/reloading 是持久化字段
}

function imgFailed(m: ChatMessage, bi: number): void {
  const b = m.viewBlocks?.[bi]
  if (b) { b.failed = true; schedulePersist() }
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
      messages: c.messages.map((m) => { const { _stageTimer, _renderRaf, _shownLen, _lastRenderTs, _rRaf, _rShownLen, _rTs, _reasoningDone, _reasoningT0, _htmlStable, _htmlTail, loading, streaming, _thinking, ...rest } = m as any; return rest }),
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
    schedulePersist()   // 以当前用户 uid 戳重写空态（旧 deep watch 对 conversations 重赋值同样会触发）
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
    // 开机默认空态（欢迎界面）：历史照常回灌进侧栏,但不自动激活上次会话——
    // 关闭再打开 = 新对话起点,想续聊点侧栏历史即可（与 ChatGPT 开窗即新对话的习惯一致）。
    activeId.value = ''
  } catch { /* 损坏数据忽略 */ }
}

let _persistTimer: ReturnType<typeof setTimeout> | null = null
function schedulePersist(): void {
  if (_persistTimer) clearTimeout(_persistTimer)
  _persistTimer = setTimeout(persist, 400)
}

// 模块初始化：从 localStorage 恢复（仅浏览器环境）。
// H#69：持久化不再走 `watch([conversations, activeId], …, { deep: true })`——那会在流式期每 tick
// 深遍历整棵会话树。改为在每个会话变更点手动 schedulePersist()（ask 发起 / finishStream 收尾 /
// ask 异常 / stop / retry / newConversation / switchTo / removeConversation / vote 及回滚 / handoff /
// copyAns / resignImage / imgFailed / loadConversationMessages 回灌 / syncHistoryForUser 清残留）。
// persist 本身延迟 400ms 读【实时】状态，故触发点只需落在同一轮同步变更的任意位置。
if (typeof window !== 'undefined') {
  loadPersisted()
}

// P0-D 身份作用域挂接：与其它 store 不同，问答历史按【uid 戳】处理而非无条件清空——
// 同一用户换 token/角色刷新（401 重登、whoami 复核）绝不打断在途问答、不动本地历史
//（否则 apiFetch 的 401 自动重试会被 askSeq++ 误废，答案静默丢失）；真换了人才作废在途流、
// 清草稿并走 syncHistoryForUser（uid 戳判定，共享设备防残留）。
registerIdentityScopedStore('ask', (sw) => {
  if (sw.prevUserId === sw.nextUserId) return   // 同人（仅 token/角色变）：历史/草稿/在途流一律保留
  askSeq++                                      // 作废上个用户的在途流回调
  if (abortCtl) { try { abortCtl.abort() } catch { /* noop */ } abortCtl = null }
  asking.value = false
  draft.value = ''                              // 未发送草稿不跨身份残留
  syncHistoryForUser(sw.nextUserId)             // 跨用户：清本地会话历史并以新 uid 重新戳记
})

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
  const fp = identityFingerprint()   // P0-D：在途判废——标题含提问摘要，A 的慢响应不得落进 B 的侧栏
  try {
    const r = await apiJson<{ items: ServerConv[] }>('/api/conversations', { auth: true })
    if (fp !== identityFingerprint()) return
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
      schedulePersist()   // 服务端回灌消息后该会话进入持久化范围（messages.length > 0）
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

// ── Agent canary 桥（useAgentAsk 专用，只加不改）────────────────────────────
// Agent transport 复用旧路径的会话创建/消息 id/持久化调度/草稿，避免在另一个模块里复刻一份
// 会漂移的实现。旧 RAG 流程不感知本函数（零行为变化）；agent 侧自持独立 seq/abort，不碰 askSeq。
export function agentChatBridge() {
  return {
    ensureActive,
    schedulePersist,
    nextMsgId: () => ++mid,
    /** 深度思考开关（与旧路径同一状态源）：agent 路径读它请求模型档 light→high。 */
    thinking,
    /** 旧路径回退入口（agent 404 时同问重发；skipUser=true 复用已推的用户气泡）。 */
    askLegacy: (q: string) => ask(q, true),
  }
}

/** 仅供测试：流式增量渲染内部（H#68）——供等价性回归测试对照全量 stripImg/renderMd。
 *  实现已抽 @/lib/mdIncr（perf 批次 B §6.2），testkit 契约原样保留。 */
export function __incrRenderTestkit() {
  return { newState: newPumpState, stripImgIncr, renderMdIncr }
}

/** 仅供测试：重置单例状态（顺带忘掉 identityScope 已观测身份，下次 sync 首见采纳）。 */
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
  __resetIdentityScope()
}
