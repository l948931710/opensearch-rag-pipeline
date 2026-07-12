import { reactive, ref, watch } from 'vue'
import { ApiError, apiFetch, apiJson } from '@/lib/api'
import { createSseDecoder, type SseEvent } from '@/lib/sseDecoder'
import { renderMd, stripImg } from '@/lib/markdown'
import { useSession } from '@/stores/session'
import { useDialog } from '@/composables/useDialog'
import { agentChatBridge, mapSources, mapViewBlocks, useAsk, type AgentMsgMeta, type ChatMessage } from '@/composables/useAsk'
import { __resetIdentityScope, identityFingerprint, registerIdentityScopedStore, syncIdentityScope } from '@/composables/identityScope'

// Agent 用户侧 canary（外部审计 P0-F「Agent 尚未形成用户可用链路」/ 报告 §8 P0 user-side）。
// 独立 /api/agent/ask SSE transport + 「Agent 模式」kill switch + 运行中心（我的 runs / 详情 / 轮询恢复）。
// 设计边界：
//  · 旧 RAG 路径（useAsk.ask）一行不动——本模块经 agentChatBridge 复用会话创建/消息 id/持久化，
//    默认关（开关 OFF = 旧路径），任何时刻可关回（kill switch）；
//  · 能力探测（同 useAgentApprovals 的 supported 模式）：GET /api/agent/runs 404/403 → supported=false，
//    UI 不渲染任何 agent 入口（绝不留死入口）；/api/agent/ask 中途 404（flag 被关）→ 本问自动回退旧路径；
//  · 断线恢复（报告 §8②⑥「批准≠成功，用户必须看到真实执行结果」）：挂起/断流的 run 按 run_id
//    每 5s 轮询 GET /api/agent/runs/{id}，终态停；审批通过后不依赖原 SSE 也能看到完成与工具回执。
//  · 身份纪律（P0-D）：模块级状态注册 identityScope；loader 入口 lazy 对账；在途响应按指纹判废；
//    「Agent 模式」localStorage 持久但【换人】清空（同人换 token 只重探测，不清偏好）。

// ── 类型（对齐 routes/agent.py 响应；字段防御性可空）──────────────────────────
export interface AgentRunRow {
  run_id: string
  status: string             // running/suspended/resuming/succeeded/failed/cancelled/expired
  thread_id?: string | null
  conversation_id?: string | null
  agent_profile?: string | null
  model_profile?: string | null   // 模型档（light/high/…）：深度思考开→high
  turns_used?: number | null
  tool_calls_used?: number | null
  tokens_used?: number | null
  started_at?: string | null
  ended_at?: string | null
  user_id?: string | null    // 仅详情 run 携带（列表恒为本人）
  channel?: string | null
}
export interface AgentRunStep {
  step_no: number
  kind: string               // model_call / tool_call / approval / …
  payload?: unknown          // 服务端已脱敏
  tokens_prompt?: number | null
  tokens_completion?: number | null
  created_at?: string | null
}
export interface AgentInvocation {
  invocation_id: string
  run_id?: string
  step_no?: number | null
  tool_name: string
  status: string             // proposed/denied/pending_approval/executing/succeeded/failed/uncertain
  policy_decision?: string | null
  approval_request_id?: string | null
  idempotency_key?: string | null
  args_digest?: string | null
  error_text?: string | null
  started_at?: string | null
  ended_at?: string | null
}
export interface AgentRunApproval {
  request_id: string
  call_id?: string | null
  tool_name?: string | null
  status?: string | null     // pending/approved/rejected/expired/…
  approver_scope?: string | null
  render_summary?: string | null
  proposed_args?: Record<string, unknown> | null
  expires_at?: string | null
  created_at?: string | null
  decided_at?: string | null
}
export interface AgentRunDetail {
  run: AgentRunRow
  steps: AgentRunStep[]
  invocations: AgentInvocation[]
  approval: AgentRunApproval | null
}

// ── 展示辞典（组件共用，避免各处漂移）────────────────────────────────────────
export const AGENT_TOOL_LABEL: Record<string, string> = {
  knowledge_search: '检索知识库',
  ontology_resolve: '本体消解',
  ontology_identity_resolve: '身份消解',
  u8_writeback: 'U8 写回',
}
export function agentToolLabel(name?: string | null): string {
  return AGENT_TOOL_LABEL[name || ''] || name || '工具'
}
/** run 状态 → 中文 + StatusPill 色调键（tone 与 lib/kb 的 st-* 家族同一套）。 */
const RUN_STATUS: Record<string, { label: string; tone: string }> = {
  running: { label: '运行中', tone: 'busy' },
  suspended: { label: '等待审批', tone: 'warn' },
  resuming: { label: '恢复执行中', tone: 'busy' },
  succeeded: { label: '已完成', tone: 'live' },
  failed: { label: '失败', tone: 'fail' },
  cancelled: { label: '已终止', tone: 'muted' },
  expired: { label: '已过期', tone: 'muted' },
}
export function runStatusLabel(s?: string | null): string { return RUN_STATUS[s || '']?.label || s || '—' }
export function runStatusTone(s?: string | null): string { return RUN_STATUS[s || '']?.tone || 'muted' }
/** 工具回执状态 → 中文（tool_result 帧与 invocation.status 同域 + 落库侧扩展态）。 */
const INVOCATION_STATUS: Record<string, { label: string; tone: string }> = {
  proposed: { label: '已提案', tone: 'queue' },
  denied: { label: '被策略拒绝', tone: 'fail' },
  pending_approval: { label: '等待审批', tone: 'warn' },
  executing: { label: '执行中', tone: 'busy' },
  succeeded: { label: '成功', tone: 'live' },
  failed: { label: '失败', tone: 'fail' },
  uncertain: { label: '结果不确定', tone: 'warn' },
}
export function invocationStatusLabel(s?: string | null): string { return INVOCATION_STATUS[s || '']?.label || s || '—' }
export function invocationStatusTone(s?: string | null): string { return INVOCATION_STATUS[s || '']?.tone || 'muted' }
/** 脱敏参数预览（与 AgentApprovalQueue 同口径：k=v · 连接）。 */
export function agentArgsPreview(args?: Record<string, unknown> | null): string {
  if (!args || typeof args !== 'object') return '—'
  const parts = Object.entries(args).map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
  return parts.join(' · ') || '—'
}

export const RUN_TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'expired'])

// ── 模块级状态（identityScope 注册，见文件尾）────────────────────────────────
// null=尚未探测（入口一律不显）；false=404/403（flag 未开/无权）；true=端点在。
const supported = ref<boolean | null>(null)
const agentMode = ref(false)                    // kill switch：OFF=旧 RAG 路径（默认）
const runs = ref<AgentRunRow[]>([])
const runDetails = ref<Record<string, AgentRunDetail>>({})
const runCenterOpen = ref(false)
const runCenterRunId = ref('')                  // 运行中心当前聚焦的 run（''=列表）
const agentStreamActive = ref(false)            // 当前在途流是否 agent（QaView 据此分发 stop）
const inflight = ref<Set<string>>(new Set())    // 撤回等处置的防重

const STALE_MS = 30_000
let _pollMs = 5_000            // 轮询节拍（生产 5s；测试经 __agentAskTestkit 缩短，避免 fake-timers 与 happy-dom 互踩）
const LS_MODE_KEY = 'fl-agent-mode'

let agentSeq = 0                                // 竞态锁：停止/新提问/身份切换递增，作废在途流回调
let abortCtl: AbortController | null = null
let streamConvId = ''                           // 在途 agent 流所属会话（activeId 守卫的比较基准）
let lastLoadedAt = 0
const _pollTimers = new Map<string, ReturnType<typeof setInterval>>()

const bridge = agentChatBridge()
const { asking, draft, messages, conversations, activeId } = useAsk()

function isAgentBusy(key: string): boolean { return inflight.value.has(key) }
async function withInflight<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
  if (inflight.value.has(key)) return undefined
  inflight.value = new Set(inflight.value).add(key)
  try { return await fn() } finally { const n = new Set(inflight.value); n.delete(key); inflight.value = n }
}

// ── kill switch 持久化（uid 戳：跨 reload 保留本人偏好，别人的戳直接作废）──────
function _uid(): string { try { return useSession().identity?.userId || '' } catch { return '' } }
function persistAgentMode(): void {
  try { localStorage.setItem(LS_MODE_KEY, JSON.stringify({ uid: _uid(), on: agentMode.value })) } catch { /* 隐私模式忽略 */ }
}
function restoreAgentMode(): void {
  try {
    const raw = localStorage.getItem(LS_MODE_KEY)
    if (!raw) return
    const d = JSON.parse(raw) || {}
    if (d.uid && d.uid === _uid()) agentMode.value = !!d.on
    else localStorage.removeItem(LS_MODE_KEY)   // 别人的偏好不复活（共享设备）
  } catch { /* 损坏数据忽略 */ }
}
function toggleAgentMode(): void {
  agentMode.value = !agentMode.value
  persistAgentMode()
}

// ── 能力探测 + 我的 runs（同一请求：GET /api/agent/runs 双职）──────────────────
async function loadAgentRuns(force = false): Promise<void> {
  syncIdentityScope()               // P0-D：读取前先对账（变了先同步清空）
  const fp = identityFingerprint()  // 在途判废基准
  const s = useSession()
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    runs.value = [
      { run_id: 'run_demo1', status: 'suspended', thread_id: 't1', conversation_id: 'c1', agent_profile: 'default', turns_used: 2, tool_calls_used: 1, tokens_used: 1830, started_at: '2026-07-11 09:30:00', ended_at: null },
      { run_id: 'run_demo2', status: 'succeeded', thread_id: 't2', conversation_id: 'c2', agent_profile: 'default', turns_used: 3, tool_calls_used: 2, tokens_used: 4210, started_at: '2026-07-10 16:02:00', ended_at: '2026-07-10 16:03:10' },
    ]
    supported.value = true
    restoreAgentMode()
    return
  }
  if (!s.token) return              // 未登录不探测（QaView 挂载在 ready 之后，正常不会走到）
  if (!force && supported.value !== null && Date.now() - lastLoadedAt < STALE_MS) return
  lastLoadedAt = Date.now()
  try {
    const r = await apiJson<{ items: AgentRunRow[] }>('/api/agent/runs?limit=20', { auth: true })
    if (fp !== identityFingerprint()) return   // 身份已切换：旧身份的列表整体丢弃
    runs.value = r.items || []
    supported.value = true
    restoreAgentMode()
  } catch (e) {
    if (fp !== identityFingerprint()) return
    lastLoadedAt = 0
    if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
      supported.value = false                  // RAG_AGENT_ENABLE 未开：静默、不留死入口
      agentMode.value = false
      return
    }
    // 网络/5xx：保持 null（未知），下次探测重试；不置全局错（问答主路径不受影响）
  }
}
/** 能力探测入口（QaView 挂载/身份变化时调；30s 内幂等）。 */
function probeAgent(): Promise<void> { return loadAgentRuns(false) }

// ── 阶段化状态条（只映射真实事件；at=Date.now，本地计时）────────────────────────
function pushStage(ai: ChatMessage, key: string, label: string): void {
  const meta = ai.agent
  if (!meta) return
  meta.stages = meta.stages || []
  const last = meta.stages[meta.stages.length - 1]
  if (last && last.key === key) return          // 连续同阶段去重（chunk 逐帧到达只记一次「回答中」）
  meta.stages.push({ key, label, at: Date.now() })
}

// ── 流式渲染（canary 简化版）：raw 累积 + rAF/80ms 节流全量渲染，收尾权威定稿。
// 不复用 useAsk 的匀速吐字泵（其增量缓存/双节点拆分与 askSeq 深绑）；canary 流量低、
// 答案短，节流全量渲染足够；无 rAF 环境（测试/SSR）退化为即时渲染。
function scheduleAgentRender(ai: ChatMessage, seq: number): void {
  const renderNow = () => { ai.html = renderMd(stripImg(ai.raw || '')) }
  if (typeof requestAnimationFrame !== 'function') { renderNow(); return }
  if (ai._renderRaf != null) return
  ai._renderRaf = requestAnimationFrame(() => {
    ai._renderRaf = null
    if (seq !== agentSeq) return
    const now = Date.now()
    if (ai._lastRenderTs && now - ai._lastRenderTs < 80) { scheduleAgentRender(ai, seq); return }
    ai._lastRenderTs = now
    renderNow()
  })
}

function ensureHtmlString(ai: ChatMessage): void {
  if (ai.html == null) ai.html = ''   // 挂起/纯工具消息也走「正常答案」分支，不落"没有内容"兜底
}

// ── SSE 帧处理 ────────────────────────────────────────────────────────────────
function onAgentEvent(conv: { qaSession: string }, ai: ChatMessage, ev: SseEvent, seq: number): void {
  if (seq !== agentSeq) return
  const meta = ai.agent as AgentMsgMeta
  switch (ev.type) {
    case 'session':
      meta.messageId = (ev.message_id as string) || ''
      meta.runId = (ev.run_id as string) || ''
      if (ev.session_id) conv.qaSession = ev.session_id as string
      pushStage(ai, 'submitted', '已提交')
      // 运行中心列表即时可见（服务端行随后轮询/刷新对齐）
      if (meta.runId && !runs.value.some((r) => r.run_id === meta.runId)) {
        runs.value = [{ run_id: meta.runId, status: 'running', started_at: null, ended_at: null }, ...runs.value]
      }
      break
    case 'chunk':
      ai.loading = false
      ai.raw = (ai.raw || '') + ((ev.content as string) || '')
      pushStage(ai, 'answering', '回答中')
      scheduleAgentRender(ai, seq)
      break
    case 'tool_call': {
      ai.loading = false
      meta.tools = meta.tools || []
      meta.tools.push({
        callId: (ev.call_id as string) || '',
        toolName: (ev.tool_name as string) || '',
        args: (ev.arguments as Record<string, unknown>) ?? null,
        status: 'proposed',
      })
      pushStage(ai, 'tool', '调用工具')
      ensureHtmlString(ai)
      break
    }
    case 'tool_result': {
      // 工具结局收敛（P0-F 阶段化状态）：status ∈ succeeded/failed/denied/pending_approval
      const t = (meta.tools || []).find((x) => x.callId === (ev.call_id as string) && (x.status === 'proposed' || !x.status))
        || (meta.tools || []).find((x) => x.callId === (ev.call_id as string))
      if (t) {
        t.status = (ev.status as string) || ''
        t.elapsedMs = Number(ev.elapsed_ms) || 0
      }
      pushStage(ai, 'tool_done', '工具完成')
      break
    }
    case 'approval': {
      // run 挂起等审批：流就此结束（[DONE] 紧随），后续状态靠 run_id 轮询
      const pc = (ev.pending_call || {}) as Record<string, unknown>
      meta.approval = {
        requestId: (ev.approval_request_id as string) || '',
        checkpointId: (ev.checkpoint_id as string) || undefined,
        toolName: (pc.tool_name as string) || undefined,
        args: (pc.arguments as Record<string, unknown>) ?? null,
      }
      meta.status = 'suspended'
      const t = (meta.tools || []).find((x) => x.callId === (pc.call_id as string))
      if (t && (t.status === 'proposed' || !t.status)) t.status = 'pending_approval'
      pushStage(ai, 'awaiting_approval', '等待审批')
      ai.loading = false
      ensureHtmlString(ai)
      if (meta.runId) ensureRunPolling(meta.runId)
      break
    }
    case 'sources':
      // 答案契约对齐：与旧路径同一映射/同一 SourceList 渲染（服务端已做 SourceInfo 字段收口）。
      // union 递进帧，赋值语义 → 末帧即全集。agent 有阶段条，不复用旧路径的有据等待文案。
      ai.sources = mapSources(ev.sources as any[])
      ai.sourcesOpen = false
      break
    case 'content_blocks':
      // 图文定稿帧（done 之后、[DONE] 之前）：与旧路径同一 ViewBlock 映射，AnswerBlocks 共渲染。
      ai.viewBlocks = mapViewBlocks(ev.content_blocks as any[])
      ai.copyText = ((ev.content_blocks as any[]) || [])
        .filter((b) => b.type !== 'image').map((b) => b.content || '').join('\n') || stripImg(ai.raw || '')
      break
    case 'done':
      meta.status = meta.status === 'suspended' ? meta.status : 'succeeded'
      pushStage(ai, 'done', '完成')
      break
    case 'error':
      ai.loading = false
      ai.streaming = false
      ai.error = true
      ai.errorText = (ev.message as string) || 'Agent 运行失败，请重试。'
      if (meta.status !== 'suspended') meta.status = 'failed'
      pushStage(ai, 'error', '失败')
      break
    // '__done'（[DONE] 哨兵）与未知类型：忽略，finishAgentStream 在 reader 结束时收尾
  }
}

function finishAgentStream(ai: ChatMessage, seq: number): void {
  if (seq !== agentSeq) return
  asking.value = false
  agentStreamActive.value = false
  abortCtl = null
  const meta = ai.agent as AgentMsgMeta
  if (ai._renderRaf != null && typeof cancelAnimationFrame === 'function') { cancelAnimationFrame(ai._renderRaf); ai._renderRaf = null }
  ai.loading = false
  ai.streaming = false
  bridge.schedulePersist()
  if (ai.error) return
  if (ai.raw) {
    ai.html = renderMd(stripImg(ai.raw))
    ai.copyText = stripImg(ai.raw)
  } else if (!meta.approval && meta.status !== 'suspended') {
    ai.error = true
    ai.errorText = 'Agent 未返回内容，请重试。'
    return
  }
  ensureHtmlString(ai)
  if (meta.status === 'succeeded') {
    ai.messageId = meta.messageId || ''     // 定稿后才出反馈条（挂起态不出）
    if (meta.runId) void refreshRunDetail(meta.runId)   // 一次回执拉取：批准≠成功，回执要可见
  }
}

/**
 * Agent 提问（与 useAsk.ask 同签名语义：preset 为空读 draft 并清空）。
 * 帧协议：session → (chunk｜tool_call｜tool_result)… → (approval｜done) → [DONE]；429=并发满。
 * /api/agent/ask 404（flag 中途被关）→ 标记不可用 + 本问自动回退旧 RAG 路径（kill switch 兜底）。
 */
async function askAgent(preset?: string, skipUser = false): Promise<void> {
  const text = ((preset != null ? preset : draft.value) || '').trim()
  if (!text || asking.value) return
  if (preset == null) draft.value = ''
  syncIdentityScope()
  const conv = bridge.ensureActive()
  if (!skipUser) conv.messages.push({ id: 'u' + bridge.nextMsgId(), role: 'user', text })
  if (conv.title === '新对话' && text) conv.title = text.slice(0, 24)
  conv.updatedAt = Date.now()

  const ai: ChatMessage = reactive({
    id: 'a' + bridge.nextMsgId(), role: 'ai', loading: false,
    question: text, sourcesOpen: false, voted: '', viewBlocks: null,
    raw: '', html: '', streaming: true,
    agent: { stages: [], tools: [], approval: null } as AgentMsgMeta,
  })
  pushStage(ai, 'submitted', '已提交')
  conv.messages.push(ai)
  asking.value = true
  agentStreamActive.value = true
  streamConvId = conv.id
  bridge.schedulePersist()

  const seq = ++agentSeq
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null
  abortCtl = ctl

  const body: Record<string, unknown> = { question: text }
  if (conv.qaSession) body.session_id = conv.qaSession
  body.conversation_id = conv.id
  if (bridge.thinking.value) body.thinking = true   // 深度思考→服务端模型档 high（仅 true 时带）

  try {
    const res = await apiFetch('/api/agent/ask', { method: 'POST', body: JSON.stringify(body), signal: ctl?.signal })
    if (res.status === 404) {
      // flag 未开/被关：标记不可用（探测态一并翻转，入口自隐）+ 本问回退旧路径
      supported.value = false
      agentMode.value = false
      persistAgentMode()
      const idx = conv.messages.indexOf(ai)
      if (idx >= 0) conv.messages.splice(idx, 1)   // 移除 agent 占位，旧路径自建 AI 消息
      asking.value = false
      agentStreamActive.value = false
      abortCtl = null
      bridge.schedulePersist()
      await bridge.askLegacy(text)                 // skipUser：用户气泡已在
      return
    }
    if (!res.ok) {
      const t = await res.text().catch(() => '')
      const friendly = res.status === 429 ? 'Agent 并发已满，请稍后再试。' : ''
      const e: any = new Error(friendly || t || `HTTP ${res.status}`)
      e.status = res.status
      e._friendly = !!friendly
      throw e
    }
    if (!res.body || !res.body.getReader) throw new Error('浏览器不支持流式读取')

    const reader = res.body.getReader()
    const dec = createSseDecoder()
    for (;;) {
      const { value, done } = await reader.read()
      if (seq !== agentSeq) { try { void reader.cancel() } catch { /* noop */ } return }
      if (done) {
        for (const ev of dec.flush()) onAgentEvent(conv, ai, ev, seq)
        finishAgentStream(ai, seq)
        break
      }
      for (const ev of dec.push(value!)) onAgentEvent(conv, ai, ev, seq)
    }
  } catch (e: any) {
    if (seq !== agentSeq) return   // 已被停止/新提问/身份切换接管
    asking.value = false
    agentStreamActive.value = false
    abortCtl = null
    if (ai._renderRaf != null && typeof cancelAnimationFrame === 'function') { cancelAnimationFrame(ai._renderRaf); ai._renderRaf = null }
    ai.loading = false
    ai.streaming = false
    ai.error = true
    ai.errorText = e && e.name === 'AbortError'
      ? '已取消本次提问。'
      : (e && e._friendly ? e.message : 'Agent 回答失败，请检查网络后重试。')
    pushStage(ai, 'error', '失败')
    bridge.schedulePersist()
  }
}

/** 停止观看当前 agent 流。⚠️ 只断【视图】不断 run——服务端 run 继续跑（落库在 run 完成侧），
 *  有 run_id 则转轮询兜底（断线恢复语义）；真要终止 run 用挂起卡/运行中心的「撤回」。 */
function stopAgent(): void {
  agentSeq++
  if (abortCtl) { try { abortCtl.abort() } catch { /* noop */ } abortCtl = null }
  asking.value = false
  agentStreamActive.value = false
  const list = messages.value
  const ai = list[list.length - 1]
  if (ai && ai.role === 'ai' && ai.agent) {
    if (ai._renderRaf != null && typeof cancelAnimationFrame === 'function') { cancelAnimationFrame(ai._renderRaf); ai._renderRaf = null }
    ai.loading = false
    ai.streaming = false
    if (ai.raw) { ai.html = renderMd(stripImg(ai.raw)); ai.copyText = stripImg(ai.raw) }
    const meta = ai.agent
    if (meta.runId && !RUN_TERMINAL.has(meta.status || '')) {
      meta.disconnected = true                 // 实时流已断，run 仍在后台
      ensureRunPolling(meta.runId)
    } else if (!ai.raw && !meta.approval && !ai.error) {
      ai.error = true
      ai.errorText = '已取消本次提问。'
    }
    bridge.schedulePersist()
  }
}

/** 错误卡重试（agent 消息专用）：按当前模式重发——agent 可用且开着走 agent，否则回旧路径。 */
function retryAgent(m: ChatMessage): void {
  const idx = messages.value.indexOf(m)
  if (idx >= 0) { messages.value.splice(idx, 1); bridge.schedulePersist() }
  const q = m.question || ''
  if (agentMode.value && supported.value === true) void askAgent(q, true)
  else void bridge.askLegacy(q)
}

// 切换/删除会话（activeId 变化）时在途 agent 流同旧路径语义作废（useAsk.switchTo 内部的
// stop() 只作废旧路径流，感知不到本模块的 fetch——这里补上同一约定）。
// ⚠️ 只在切到【别的】会话时停：首问 ensureActive 新建会话也会改 activeId（属于本流的启动动作，
// watcher 异步 flush 时 streamConvId 已就位），不能误杀刚起步的流。
watch(activeId, (id) => { if (agentStreamActive.value && id !== streamConvId) stopAgent() })

// ── run 轮询（断线恢复核心）───────────────────────────────────────────────────
function stopRunPolling(runId: string): void {
  const t = _pollTimers.get(runId)
  if (t) { clearInterval(t); _pollTimers.delete(runId) }
}
async function _pollRunOnce(runId: string): Promise<void> {
  const fp = identityFingerprint()
  try {
    const d = await apiJson<AgentRunDetail>(`/api/agent/runs/${encodeURIComponent(runId)}`, { auth: true })
    if (fp !== identityFingerprint()) { stopRunPolling(runId); return }
    runDetails.value = { ...runDetails.value, [runId]: d }
    const st = d?.run?.status || ''
    // 列表行对齐
    const i = runs.value.findIndex((r) => r.run_id === runId)
    if (i >= 0) runs.value.splice(i, 1, { ...runs.value[i], ...d.run })
    // 聊天消息回写（挂起卡/断流卡不依赖原 SSE 更新；审批通过→执行→回执全程可见）
    for (const c of conversations.value) {
      for (const m of c.messages) {
        if (m.role === 'ai' && m.agent?.runId === runId) {
          m.agent.status = st
          if (RUN_TERMINAL.has(st)) {
            m.agent.disconnected = false
            if (st === 'succeeded' && !m.messageId && m.agent.messageId) m.messageId = m.agent.messageId
          }
        }
      }
    }
    bridge.schedulePersist()
    if (RUN_TERMINAL.has(st)) stopRunPolling(runId)
  } catch (e) {
    if (fp !== identityFingerprint()) { stopRunPolling(runId); return }
    // 404/403（不可见==不存在，或 flag 被关）→ 停轮询；网络错误保留定时器下轮再试
    if (e instanceof ApiError && (e.status === 404 || e.status === 403)) stopRunPolling(runId)
  }
}
/** 确保某 run 在轮询（5s 间隔，终态自动停）。挂起卡挂载/断流/运行中心聚焦时调用。 */
function ensureRunPolling(runId: string): void {
  if (!runId) return
  const known = runDetails.value[runId]?.run?.status
  if (known && RUN_TERMINAL.has(known)) return
  if (_pollTimers.has(runId)) return
  if (typeof setInterval === 'function') {
    _pollTimers.set(runId, setInterval(() => { void _pollRunOnce(runId) }, _pollMs))
  }
  void _pollRunOnce(runId)
}
/** 单次详情刷新（终态 run 的回执查看；不起定时器）。 */
function refreshRunDetail(runId: string): Promise<void> { return _pollRunOnce(runId) }

// ── 撤回（发起人自撤挂起 run；后端 _authorize_approver 恒放行本人 rejected_terminate）──
async function withdrawRun(runId: string, requestId?: string): Promise<boolean> {
  syncIdentityScope()
  const done = await withInflight(`agent-run:${runId}`, async () => {
    try {
      const res = await apiFetch('/api/agent/approve', {
        method: 'POST', auth: true,
        body: JSON.stringify({
          run_id: runId,
          outcome: { kind: 'rejected_terminate' },
          // 与审批 tab 的幂等口径一致（request_id:kind）；无 request_id 时退化为 run 级键
          idempotency_key: `${requestId || runId}:rejected_terminate`,
        }),
      })
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try { detail = (await res.json())?.detail || detail } catch { /* SSE/非 JSON */ }
        void useDialog().notice({ title: '撤回失败', message: detail, danger: true })
        if (res.status === 409) void refreshRunDetail(runId)   // 已被处置 → 对齐真实状态
        return false
      }
      void res.body?.cancel()                                  // 终止流无需旁观
      await refreshRunDetail(runId)                            // cancelled 立即可见
      return true
    } catch {
      void useDialog().notice({ title: '撤回失败', message: '网络异常，请重试', danger: true })
      return false
    }
  })
  return done === true
}

// ── 运行中心开合 ─────────────────────────────────────────────────────────────
function openRunCenter(runId?: string): void {
  runCenterOpen.value = true
  runCenterRunId.value = runId || ''
  void loadAgentRuns(true)
  if (runId) {
    const st = runDetails.value[runId]?.run?.status
    if (st && RUN_TERMINAL.has(st)) void refreshRunDetail(runId)
    else ensureRunPolling(runId)
  }
}
function closeRunCenter(): void {
  runCenterOpen.value = false
  runCenterRunId.value = ''
}

export function useAgentAsk() {
  return {
    agentSupported: supported,
    agentMode,
    toggleAgentMode,
    probeAgent,
    askAgent,
    stopAgent,
    retryAgent,
    agentStreamActive,
    // 运行中心
    agentRuns: runs,
    agentRunDetails: runDetails,
    runCenterOpen,
    runCenterRunId,
    openRunCenter,
    closeRunCenter,
    loadAgentRuns,
    refreshRunDetail,
    ensureRunPolling,
    withdrawRun,
    isAgentBusy,
  }
}

// ── 身份纪律（P0-D）：换人清偏好+全量；同人换 token/角色只作废数据面并重探测 ────────
function _resetAgentAskState(changedUser: boolean): void {
  agentSeq++                                    // 作废在途流回调
  if (abortCtl) { try { abortCtl.abort() } catch { /* noop */ } abortCtl = null }
  agentStreamActive.value = false
  supported.value = null                        // 重探测（角色/flag 可能已变）
  runs.value = []
  runDetails.value = {}
  runCenterOpen.value = false
  runCenterRunId.value = ''
  inflight.value = new Set()
  lastLoadedAt = 0
  for (const t of _pollTimers.values()) clearInterval(t)
  _pollTimers.clear()
  if (changedUser) {
    agentMode.value = false
    try { localStorage.removeItem(LS_MODE_KEY) } catch { /* noop */ }
  }
}
registerIdentityScopedStore('agentAsk', (sw) => _resetAgentAskState(sw.prevUserId !== sw.nextUserId))

/** 仅供测试：重置单例状态（顺带忘掉 identityScope 已观测身份）。 */
export function __resetAgentAsk(): void {
  _resetAgentAskState(true)
  agentMode.value = false
  _pollMs = 5_000
  __resetIdentityScope()
}

/** 仅供测试：缩短轮询节拍（真实定时器跑轮询用例，绕开 fake-timers 与 happy-dom 互踩）
 *  + 观察在跑的轮询表数量（终态停表断言）。 */
export function __agentAskTestkit() {
  return {
    setPollMs(ms?: number) { _pollMs = ms ?? 5_000 },
    pollTimerCount(): number { return _pollTimers.size },
  }
}
