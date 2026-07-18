import { reactive, ref, watch } from 'vue'
import { ApiError, apiFetch, apiJson } from '@/lib/api'
import { createSseDecoder, type SseEvent } from '@/lib/sseDecoder'
import { renderMd, stripImg } from '@/lib/markdown'
import { newPumpState, renderMdIncr, stripImgIncr, type PumpChState } from '@/lib/mdIncr'
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
export interface AgentRunFinal {
  message_id: string
  answer_text: string
  answered_at?: string | null
}
export interface AgentRunDetail {
  run: AgentRunRow
  steps: AgentRunStep[]
  invocations: AgentInvocation[]
  approval: AgentRunApproval | null
  /** U1（schema/036）：succeeded run 经 agent_run.message_id 从 qa_session_log 取回的最终答案；
   *  历史行/留存期外/后端旧版 → null/undefined（前端引导去会话历史）。 */
  final?: AgentRunFinal | null
  /** perf 批次 B §4.3：服务端状态变更指纹（与 /status 探针同口径；不透明字符串，仅比较）。
   *  后端旧版无此字段 → 前端退回每拍全量 detail（行为同批次 B 之前）。 */
  state_key?: string | null
}
/** GET /api/agent/runs/{id}/status 轻量探针响应（perf 批次 B §4.3）。 */
interface AgentRunStatusProbe {
  run_id: string
  status: string
  state_key: string
  started_at?: string | null
  ended_at?: string | null
  turns_used?: number | null
  tool_calls_used?: number | null
  tokens_used?: number | null
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
// 详情拉取失败态（批次β F3）：此前非 404/403 错误被静默吞掉 → 详情面永久停在「正在加载…」
// 且无任何反馈（死胡同）。key=run_id；成功清除。
const runDetailErrors = ref<Record<string, string>>({})
const runCenterOpen = ref(false)
const runCenterRunId = ref('')                  // 运行中心当前聚焦的 run（''=列表）
const agentStreamActive = ref(false)            // 当前在途流是否 agent（QaView 据此分发 stop）
const inflight = ref<Set<string>>(new Set())    // 撤回等处置的防重

const STALE_MS = 30_000
let _pollMs = 5_000            // 轮询节拍（生产 5s；测试经 __agentAskTestkit 缩短，避免 fake-timers 与 happy-dom 互踩）
// perf 批次 A §4.3/§6.3：suspended 走慢拍（=_pollMs 的倍数，非绝对常量——测试 setPollMs 后仍成比例
// 缩短）；网络/5xx 指数退避（上限 BACKOFF_MAX_MULT）；隐藏页整拍跳过（守卫在 _tick 内，镜像 ManageView）。
const SUSPENDED_MULT = 9       // suspended：5s×9≈45s（挂起 run 不需 5s 抢拍，省一截读放大）
const BACKOFF_MAX_MULT = 8     // 退避倍率上限（避免故障时无限拉长）
const LS_MODE_KEY = 'fl-agent-mode'

let agentSeq = 0                                // 竞态锁：停止/新提问/身份切换递增，作废在途流回调
let abortCtl: AbortController | null = null
let streamConvId = ''                           // 在途 agent 流所属会话（activeId 守卫的比较基准）
let lastLoadedAt = 0
const _pollTimers = new Map<string, ReturnType<typeof setTimeout>>()   // 自续 setTimeout 句柄（每 run ≤1）
const _pollActive = new Set<string>()            // 应继续轮询的 run（与 timer 句柄解耦，避免重复起表/孤儿表）
const _pollBackoff = new Map<string, number>()   // 每 run 连续网络/5xx 失败次数（成功清零，驱动指数退避）
// perf 批次 B §6.3：run→聊天消息索引（O(1) 定位，取代每拍全会话×全消息扫描）。session 帧
// 学到 runId 即注册；LS 恢复等索引缺失场景由 _runMessage 懒回扫回填；reset 全清。
const _runMsgIndex = new Map<string, ChatMessage>()
// perf 批次 B §4.3：上次观察到的服务端 state_key（两段式轮询的比较基准；detail 成功时回填，
// stopRunPolling 清除——手动 refreshRunDetail 恒走全量）。
const _runStateKeys = new Map<string, string>()

const bridge = agentChatBridge()
// 批次8（ultra useAsk:433）：把本模块的 stopAgent 注入 legacy stop()——会话切换/新建/删除
// 同步调的 legacy stop 不再把在跑 agent run 涂成取消错误卡（function 声明提升，此处可引用）。
// 可选调用：组件测试对 agentChatBridge 的 mock 只实现最小面（useAsk mock 集成缝，见 memory），
// 缺该方法时静默跳过（等价旧行为，绝不让 mock 面扩张成硬契约）。
bridge.registerAgentStop?.(stopAgent)
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

// ── 流式渲染（perf 批次 B §6.2）：复用 @/lib/mdIncr 增量内核——每拍只 strip/render 新增
// 片段（旧「canary 简化版」每 80ms 对全文 renderMd(stripImg)，长答案 O(n²)）。等价性由
// useAskIncrRender.spec 的「任意前缀 incremental===全量」契约背书；不接 useAsk 的匀速
// 吐字泵/双节点拆分（与 askSeq 深绑），保持单 ai.html 渲染；收尾仍全量权威定稿。
// rAF/80ms 节流与无 rAF 环境（测试/SSR）即时渲染的调度行为不变。
const _agentPumpStates = new WeakMap<ChatMessage, PumpChState>()
// 轻量渲染埋点（§6.2 要求 render duration / answer length 可观测）：模块内累计，
// __agentAskTestkit 只读暴露；不上报、不进生产日志。
const _renderStats = { frames: 0, totalMs: 0, maxMs: 0, lastLen: 0 }

function _agentRenderNow(ai: ChatMessage): void {
  const t0 = Date.now()
  let st = _agentPumpStates.get(ai)
  if (!st) { st = newPumpState(); _agentPumpStates.set(ai, st) }
  ai.html = renderMdIncr(st, stripImgIncr(st, ai.raw || ''))
  const d = Date.now() - t0
  _renderStats.frames += 1
  _renderStats.totalMs += d
  if (d > _renderStats.maxMs) _renderStats.maxMs = d
  _renderStats.lastLen = (ai.raw || '').length
}

function scheduleAgentRender(ai: ChatMessage, seq: number): void {
  if (typeof requestAnimationFrame !== 'function') { _agentRenderNow(ai); return }
  if (ai._renderRaf != null) return
  ai._renderRaf = requestAnimationFrame(() => {
    ai._renderRaf = null
    if (seq !== agentSeq) return
    const now = Date.now()
    if (ai._lastRenderTs && now - ai._lastRenderTs < 80) { scheduleAgentRender(ai, seq); return }
    ai._lastRenderTs = now
    _agentRenderNow(ai)
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
      if (meta.runId) _runMsgIndex.set(meta.runId, ai)   // §6.3：学到 runId 即注册索引
      if (ev.session_id) conv.qaSession = ev.session_id as string
      pushStage(ai, 'submitted', '已提交')
      // 运行中心列表即时可见（服务端行随后轮询/刷新对齐）
      if (meta.runId && !runs.value.some((r) => r.run_id === meta.runId)) {
        runs.value = [{ run_id: meta.runId, status: 'running', started_at: null, ended_at: null }, ...runs.value]
      }
      break
    case 'chunk': {
      // A7（复核批次4）：纯 reasoning 轮可能出空 content 帧——空帧不该熄 loading /
      // 推进「回答中」阶段 / 触发渲染（后端双端点已加 guard，这里是第三道防线）。
      const content = (ev.content as string) || ''
      if (!content) break
      ai.loading = false
      ai.raw = (ai.raw || '') + content
      pushStage(ai, 'answering', '回答中')
      scheduleAgentRender(ai, seq)
      break
    }
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
      meta.gotTerminal = true   // 批次2（P0-03f）：approval 是合法的流终局
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
      meta.gotTerminal = true
      pushStage(ai, 'done', '完成')
      break
    case 'error':
      ai.loading = false
      ai.streaming = false
      ai.error = true
      ai.errorText = (ev.message as string) || 'Agent 运行失败，请重试。'
      if (meta.status !== 'suspended') meta.status = 'failed'
      meta.gotTerminal = true
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
  _agentPumpStates.delete(ai)   // 增量状态随定稿退场（下面的全量渲染是权威兜底）
  ai.loading = false
  ai.streaming = false
  bridge.schedulePersist()
  if (ai.error) return
  // 批次2（P0-03f）：clean EOF 而没收到任何终局帧（done/approval/error）= 传输被切
  // （代理空闲超时/网关重启的干净断连）——绝不把 partial 增量当定稿呈现：已有片段先
  // 渲染出来，转断线恢复态，轮询兜底（succeeded 时 _pollRunOnce 会用 durable final 水合）。
  if (!meta.gotTerminal && meta.runId && meta.status !== 'suspended'
      && !RUN_TERMINAL.has(meta.status || '')) {
    if (ai.raw) {
      ai.html = renderMd(stripImg(ai.raw))
      ai.copyText = stripImg(ai.raw)
    }
    ensureHtmlString(ai)
    meta.disconnected = true
    pushStage(ai, 'reconnecting', '连接中断，转后台跟踪')
    ensureRunPolling(meta.runId)
    bridge.schedulePersist()
    return
  }
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
    const meta = ai.agent as AgentMsgMeta
    // 批次2（P0-03e）：中途断网但 run 已建（收到过 session 帧的 runId）→ 断线恢复而非
    // 报错卡——服务端 run 照跑、答案在完成侧 durable 落库，报「回答失败」是假话且
    // 丢掉可恢复的答案。AbortError（用户主动取消）不在此列。镜像 stopAgent 的现成形态。
    if (e?.name !== 'AbortError' && meta?.runId && !RUN_TERMINAL.has(meta.status || '')
        && meta.status !== 'suspended') {
      if (ai.raw) {
        ai.html = renderMd(stripImg(ai.raw))
        ai.copyText = stripImg(ai.raw)
      }
      ensureHtmlString(ai)
      meta.disconnected = true
      pushStage(ai, 'reconnecting', '连接中断，转后台跟踪')
      ensureRunPolling(meta.runId)
      bridge.schedulePersist()
      return
    }
    ai.error = true
    ai.errorText = e && e.name === 'AbortError'
      ? '已取消本次提问。'
      : (e && e._friendly ? e.message : 'Agent 回答失败，请检查网络后重试。')
    pushStage(ai, 'error', '失败')
    bridge.schedulePersist()
  }
}

/** A5：请求服务端协作取消（轮边界生效）。尽力而为——409/501/网络错都吞（视图已断，
 *  轮询会呈现最终真实状态；取消失败最坏=run 跑完，落库在 run 完成侧不丢答案）。 */
async function cancelRunServerSide(runId: string): Promise<void> {
  try {
    await apiJson(`/api/agent/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST', auth: true })
  } catch { /* 尽力而为：终态 409 / 跨实例 501 / 网络错，均由轮询兜底 */ }
}

/** 停止观看当前 agent 流。默认只断【视图】不断 run——服务端 run 继续跑（落库在 run
 *  完成侧），有 run_id 则转轮询兜底（断线恢复语义）；会话切换 watcher 走的就是这个形态。
 *  cancelRun=true（用户点停止按钮）额外请求服务端协作取消（批次2 cancel 端点，轮边界生效）。 */
function stopAgent(opts?: { cancelRun?: boolean }): void {
  agentSeq++
  if (abortCtl) { try { abortCtl.abort() } catch { /* noop */ } abortCtl = null }
  asking.value = false
  agentStreamActive.value = false
  // A7（复核批次4）：按 streamConvId 反查流所属会话——流中切会话时 activeId 已指向新会话，
  // messages.value 派生自 activeId：取错列表会把旧会话在途消息冻在 loading，且可能把
  // 新会话末尾的无关消息误标「已取消本次提问。」。
  const conv = conversations.value.find((c) => c.id === streamConvId)
  const list = conv ? conv.messages : messages.value
  const ai = list[list.length - 1]
  if (ai && ai.role === 'ai' && ai.agent) {
    const wasInflight = !!(ai.loading || ai.streaming)
    if (ai._renderRaf != null && typeof cancelAnimationFrame === 'function') { cancelAnimationFrame(ai._renderRaf); ai._renderRaf = null }
    ai.loading = false
    ai.streaming = false
    if (ai.raw) { ai.html = renderMd(stripImg(ai.raw)); ai.copyText = stripImg(ai.raw) }
    const meta = ai.agent
    if (meta.runId && !RUN_TERMINAL.has(meta.status || '')) {
      meta.disconnected = true                 // 实时流已断，run 仍在后台
      if (opts?.cancelRun) void cancelRunServerSide(meta.runId)
      ensureRunPolling(meta.runId)
    } else if (wasInflight && !ai.raw && !meta.approval && !ai.error && !RUN_TERMINAL.has(meta.status || '')) {
      // 只有「确实在途且一无所有」的消息才标取消——已完成/已挂起/已终态的消息不误伤
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
  _pollActive.delete(runId)
  _pollBackoff.delete(runId)
  _runStateKeys.delete(runId)   // 停表即弃比较基准：手动 refreshRunDetail 恒走全量 detail
  const t = _pollTimers.get(runId)
  if (t) { clearTimeout(t); _pollTimers.delete(runId) }
}
/** run → 聊天消息 O(1) 定位（perf 批次 B §6.3）：索引命中且 runId 仍对得上直接用；
 *  缺失/失效（LS 恢复重建了消息对象）→ 一次全扫回填；找不到 → 清索引项。 */
function _runMessage(runId: string): ChatMessage | null {
  const hit = _runMsgIndex.get(runId)
  if (hit && hit.role === 'ai' && hit.agent?.runId === runId) return hit
  for (const c of conversations.value) {
    for (const m of c.messages) {
      if (m.role === 'ai' && m.agent?.runId === runId) { _runMsgIndex.set(runId, m); return m }
    }
  }
  _runMsgIndex.delete(runId)
  return null
}
async function _pollRunOnce(runId: string): Promise<void> {
  const fp = identityFingerprint()
  // 预览 mock（批次β F3）：此前 dev-preview 只 mock 了列表、没 mock 详情——?preview 下点开
  // 详情必打真实 fetch（无后端时 502 被吞）→ 永久转圈，设计演示直接断链。
  {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') {
      runDetails.value = { ...runDetails.value, [runId]: _previewRunDetail(runId) }
      delete runDetailErrors.value[runId]
      stopRunPolling(runId)
      return
    }
  }
  try {
    // 两段式（perf 批次 B §4.3）：已有比较基准 → 先打轻量 /status；state_key 未变且非终态
    // 即止（服务端 1 次读、前端零写零 persist）。探针任何失败（后端旧版 404 / 网络）→ 照走
    // detail 全量，错误处理统一归 detail 路径（探针绝不新增失败面）。
    const cachedKey = _runStateKeys.get(runId)
    if (cachedKey) {
      let probe: AgentRunStatusProbe | null = null
      try {
        probe = await apiJson<AgentRunStatusProbe>(
          `/api/agent/runs/${encodeURIComponent(runId)}/status`, { auth: true })
      } catch { probe = null }
      if (fp !== identityFingerprint()) { stopRunPolling(runId); return }
      if (probe && probe.state_key === cachedKey && !RUN_TERMINAL.has(probe.status || '')) {
        _pollBackoff.delete(runId)   // 探针成功=链路通（清退避/清错误横幅），状态未变本拍结束
        if (runDetailErrors.value[runId]) { const n = { ...runDetailErrors.value }; delete n[runId]; runDetailErrors.value = n }
        return
      }
    }
    const d = await apiJson<AgentRunDetail>(`/api/agent/runs/${encodeURIComponent(runId)}`, { auth: true })
    if (fp !== identityFingerprint()) { stopRunPolling(runId); return }
    if (runDetailErrors.value[runId]) { const n = { ...runDetailErrors.value }; delete n[runId]; runDetailErrors.value = n }
    _pollBackoff.delete(runId)   // 成功一次即清退避（下拍恢复正常节拍）
    if (typeof d.state_key === 'string' && d.state_key) _runStateKeys.set(runId, d.state_key)
    runDetails.value = { ...runDetails.value, [runId]: d }
    const st = d?.run?.status || ''
    // 列表行对齐
    const i = runs.value.findIndex((r) => r.run_id === runId)
    if (i >= 0) runs.value.splice(i, 1, { ...runs.value[i], ...d.run })
    // 聊天消息回写（挂起卡/断流卡不依赖原 SSE 更新；审批通过→执行→回执全程可见）。
    // §6.3：O(1) 索引定位 + 变更才 persist（此前每拍无条件 schedulePersist → 全量写 LS）。
    const msg = _runMessage(runId)
    let msgChanged = false
    if (msg && msg.agent) {
      if (msg.agent.status !== st) { msg.agent.status = st; msgChanged = true }
      if (RUN_TERMINAL.has(st)) {
        if (msg.agent.disconnected) { msg.agent.disconnected = false; msgChanged = true }
        if (st === 'succeeded' && !msg.messageId && msg.agent.messageId) {
          msg.messageId = msg.agent.messageId
          msgChanged = true
        }
        // 批次2（P0-03d）：succeeded → 气泡水合 durable 最终答案（qa_session_log 读回，
        // detail.final 权威）。审批续跑/断流恢复的答案此前只在运行中心，聊天气泡永远
        // 停在审批前残稿/空白。final 覆盖增量残稿；无 final（历史行/留存过期）不动。
        const finalText = st === 'succeeded' ? ((d?.final?.answer_text as string) || '') : ''
        if (finalText && finalText !== msg.raw) {
          msg.raw = finalText
          msg.html = renderMd(stripImg(finalText))
          msg.copyText = stripImg(finalText)
          msg.loading = false
          msg.error = false
          msg.errorText = ''
          if (!msg.messageId && d?.final?.message_id) msg.messageId = d.final.message_id as string
          msgChanged = true
        }
      }
    }
    if (msgChanged) bridge.schedulePersist()
    if (RUN_TERMINAL.has(st)) stopRunPolling(runId)
  } catch (e) {
    if (fp !== identityFingerprint()) { stopRunPolling(runId); return }
    // 404/403（不可见==不存在，或 flag 被关）→ 停轮询并给终态提示；
    // 其它（网络/5xx）保留定时器下轮再试，但失败要让用户看见（此前静默吞 → 永久转圈死胡同）。
    if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
      stopRunPolling(runId)
      runDetailErrors.value = { ...runDetailErrors.value, [runId]: '该运行不可见：可能由他人发起、已被清理，或 Agent 功能未开启。' }
    } else {
      // 网络/5xx：保留轮询，指数退避（下拍延时经 _cadenceFor 自增），但失败要让用户看见
      _pollBackoff.set(runId, (_pollBackoff.get(runId) || 0) + 1)
      runDetailErrors.value = { ...runDetailErrors.value, [runId]: '运行详情拉取失败，将自动重试；也可点右上角刷新。' }
    }
  }
}

/** ?preview=kb 的详情 mock：与预览列表(run_demo1/2)呼应——挂起单带审批指向，成功单带步骤/回执/最终答案。 */
function _previewRunDetail(runId: string): AgentRunDetail {
  const row = runs.value.find((r) => r.run_id === runId)
  const base: AgentRunRow = row || { run_id: runId, status: 'succeeded', conversation_id: null, agent_profile: 'default' }
  if (base.status === 'suspended') {
    return {
      run: { ...base, user_id: 'preview' },
      steps: [
        { step_no: 1, kind: 'model_call', payload: { text: '用户要求把 PP 刀叉 8寸 的补货量写回 U8。先检索库存与写回规范。' }, tokens_prompt: 812, tokens_completion: 96, created_at: '2026-07-11 09:30:05' },
        { step_no: 2, kind: 'tool_call', payload: { tool_name: 'knowledge_search', query: 'U8 写回 补货 规范' }, created_at: '2026-07-11 09:30:09' },
        { step_no: 3, kind: 'approval', payload: { tool_name: 'u8_writeback', reason: '高风险写操作需审批' }, created_at: '2026-07-11 09:30:14' },
      ],
      invocations: [
        { invocation_id: 'pv_inv1', run_id: runId, step_no: 2, tool_name: 'knowledge_search', status: 'succeeded', started_at: '2026-07-11 09:30:09', ended_at: '2026-07-11 09:30:11' },
      ],
      approval: { request_id: 'ap1', tool_name: 'u8_writeback', status: 'pending', approver_scope: 'production', render_summary: 'u8_writeback(item=PP 刀叉 8寸, qty=120)', expires_at: '2026-07-15 20:00', created_at: '2026-07-11 09:30:14', decided_at: null },
      final: null,
    }
  }
  return {
    run: { ...base, user_id: 'preview' },
    steps: [
      { step_no: 1, kind: 'model_call', payload: { text: '查询 12oz 纸杯库存并按规范写回补货单。' }, tokens_prompt: 903, tokens_completion: 122, created_at: '2026-07-10 16:02:04' },
      { step_no: 2, kind: 'tool_call', payload: { tool_name: 'knowledge_search', query: '12oz 纸杯 库存 补货规范' }, created_at: '2026-07-10 16:02:08' },
      { step_no: 3, kind: 'tool_call', payload: { tool_name: 'u8_writeback', item: '纸杯 12oz', qty: 40 }, created_at: '2026-07-10 16:02:41' },
    ],
    invocations: [
      { invocation_id: 'pv_inv2', run_id: runId, step_no: 2, tool_name: 'knowledge_search', status: 'succeeded', started_at: '2026-07-10 16:02:08', ended_at: '2026-07-10 16:02:10' },
      { invocation_id: 'pv_inv3', run_id: runId, step_no: 3, tool_name: 'u8_writeback', status: 'succeeded', started_at: '2026-07-10 16:02:41', ended_at: '2026-07-10 16:02:52' },
    ],
    approval: { request_id: 'ap0', tool_name: 'u8_writeback', status: 'approved', approver_scope: 'production', render_summary: 'u8_writeback(item=纸杯 12oz, qty=40)', expires_at: '2026-07-12 18:30', created_at: '2026-07-10 16:02:20', decided_at: '2026-07-10 16:02:38' },
    final: { message_id: 'pv_m2', answer_text: '已按规范把补货单写回 U8：纸杯 12oz × 40，单据号 20260710-114；库存联动已确认。', answered_at: '2026-07-10 16:03:10' },
  }
}
/** run 详情轮询节拍（perf 批次 A §4.3）：suspended 走慢拍（_pollMs×SUSPENDED_MULT），其余
 *  （running/resuming/未知）走 _pollMs（未知按快拍尽快学到状态）；叠加网络/5xx 指数退避。 */
function _cadenceFor(runId: string): number {
  const st = runDetails.value[runId]?.run?.status || ''
  const base = st === 'suspended' ? _pollMs * SUSPENDED_MULT : _pollMs
  const b = _pollBackoff.get(runId) || 0
  return base * Math.min(2 ** b, BACKOFF_MAX_MULT)
}
/** 自续定时：仅当该 run 仍在 _pollActive 时排下一拍（终态/404/403/身份切换经 stopRunPolling
 *  清除 _pollActive → 不再续）；始终每 run 至多一个 timer 句柄，pollTimerCount 恒 0/1。 */
function _scheduleNext(runId: string): void {
  if (!_pollActive.has(runId)) return
  if (typeof setTimeout !== 'function') return
  _pollTimers.set(runId, setTimeout(() => { void _tick(runId) }, _cadenceFor(runId)))
}
/** 单拍：隐藏页整拍跳过（不发网络、不推进退避，下拍可见即恢复——镜像 ManageView 的
 *  visibilityState 守卫），否则拉一次详情再排下一拍。_pollRunOnce 在终态/404/403 会 stopRunPolling。 */
async function _tick(runId: string): Promise<void> {
  if (typeof document !== 'undefined' && document.hidden) { _scheduleNext(runId); return }
  // finally 续拍：即便 _pollRunOnce 意外抛出也自愈（保留旧 setInterval 的自愈性；终态/404/403
  // 已在 _pollRunOnce 内 stopRunPolling 清除 _pollActive → _scheduleNext 自然不续）。
  try {
    await _pollRunOnce(runId)
  } finally {
    _scheduleNext(runId)
  }
}
// perf 批次 B（批次 A 复核微改进①）：隐藏页恢复可见时立即补拍——不等下一拍（suspended
// 慢拍最长 45s、叠加退避可到分钟级）。清掉待发 timer 后直接 _tick（其 finally 会重排下一拍，
// pollTimerCount 每 run 仍恒 ≤1）；模块单例、document 同生命周期，监听器不摘除。
if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return
    for (const runId of Array.from(_pollActive)) {
      const t = _pollTimers.get(runId)
      if (t) { clearTimeout(t); _pollTimers.delete(runId) }
      void _tick(runId)
    }
  })
}
/** 确保某 run 在轮询（终态自动停；隐藏页暂停、恢复可见立即补拍；suspended 慢拍；
 *  网络失败指数退避；state_key 未变的拍只打轻量 /status）。
 *  挂起卡挂载/断流/运行中心聚焦时调用。 */
function ensureRunPolling(runId: string): void {
  if (!runId) return
  const known = runDetails.value[runId]?.run?.status
  if (known && RUN_TERMINAL.has(known)) return
  if (_pollActive.has(runId)) return
  _pollActive.add(runId)
  void _pollRunOnce(runId)     // 立即拉一次（断线恢复即时反馈；测试依赖立即填充 agentRunDetails）
  _scheduleNext(runId)
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
    agentRunDetailErrors: runDetailErrors,
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
  runDetailErrors.value = {}
  runCenterOpen.value = false
  runCenterRunId.value = ''
  inflight.value = new Set()
  lastLoadedAt = 0
  for (const t of _pollTimers.values()) clearTimeout(t)
  _pollTimers.clear()
  _pollActive.clear()
  _pollBackoff.clear()
  _runMsgIndex.clear()
  _runStateKeys.clear()
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
    // perf 批次 B：run→消息索引规模（O(1) 定位断言）+ 渲染埋点只读（§6.2 render duration）
    runIndexSize(): number { return _runMsgIndex.size },
    renderStats() { return { ..._renderStats } },
  }
}
