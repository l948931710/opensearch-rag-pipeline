import { computed, ref } from 'vue'
import { useSession } from '@/stores/session'
import { ApiError, apiJson } from '@/lib/api'
import { useDialog } from '@/composables/useDialog'
import { __resetIdentityScope, identityFingerprint, registerIdentityScopedStore, syncIdentityScope } from '@/composables/identityScope'

// Agent 治理（批次β F1+F2）：工具注册表 + kill switch + 代码↔DB 漂移告警 + uncertain 对账台。
// 后端（全部 kb_admin 专属，_require_kb_admin）：
//   GET  /api/agent/tools               → { items, disabled, drift }
//   POST /api/agent/tools/toggle        → { tool_name, disabled, reason? }（⚠️ 按 tool_name 整体翻转，不分版本）
//   GET  /api/agent/invocations?status= → { items }（uncertain=超时/崩溃后副作用不明，无原始参数只有 digest）
//   POST /api/agent/invocations/resolve → { invocation_id, resolution, note }（note 必填，服务端 422 兜底）
// RAG_AGENT_ENABLE 关闭时端点 404（对任何角色）→ supported=false，tab 自隐；403（非 kb_admin）同。
// ⚠️ 治理端点复用调用者自己的 ask 限流桶（agent.py _enforce_rate_limit scope="ask"）——
// 本 store 绝不接自动轮询（ManageView 的 60s pollQueues 也不得纳入），刷新只走手动按钮。
// 渲染真值源=items[].status；disabled 数组是 30s TTL 缓存（多实例下有一致性窗口），不作主渲染源。
// P0-D：模块级单例挂 identityScope——身份/token 一变同步清空，在途响应按指纹判废。

export interface AgentToolRow {
  tool_name: string
  version: string
  risk_level: string          // read_only | low_write | high_write
  permission_scope: string | null
  owner_team: string | null
  status: string              // active | deprecated | disabled
  registered_by: string | null
  created_at: string | null
}

export interface AgentInvocationRow {
  invocation_id: string
  run_id: string
  step_no: number
  tool_name: string
  status: string
  policy_decision: string | null
  approval_request_id: string | null
  idempotency_key: string | null
  args_digest: string | null
  error_text: string | null
  started_at: string | null
  ended_at: string | null
}

const STALE_MS = 30_000

const tools = ref<AgentToolRow[]>([])
const drift = ref<string[]>([])
const uncertain = ref<AgentInvocationRow[]>([])
// null=尚未探测（tab 先不显）；false=404（flag 未开）/403（非 kb_admin）；true=端点在。
const supported = ref<boolean | null>(null)
const loadError = ref('')
const inflight = ref<Set<string>>(new Set())
let lastLoadedAt = 0

function isBusy(key: string): boolean { return inflight.value.has(key) }
async function withInflight<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
  if (inflight.value.has(key)) return undefined
  inflight.value = new Set(inflight.value).add(key)
  try { return await fn() } finally { const n = new Set(inflight.value); n.delete(key); inflight.value = n }
}

async function loadAgentGovernance(force = false) {
  syncIdentityScope()               // P0-D：读取前先对账身份
  const fp = identityFingerprint()  // 在途判废基准
  const s = useSession()
  // kb_admin 专属：非 kb_admin 直接探测为不支持（后端也会 403，这里省一次必败请求）。
  if (s.identity?.role !== 'kb_admin') { tools.value = []; drift.value = []; uncertain.value = []; supported.value = false; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    // 预览 mock：三工具覆盖 risk 三态 + active/disabled 两态；一条漂移告警；两笔 uncertain。
    tools.value = [
      { tool_name: 'knowledge_search', version: '1.0', risk_level: 'read_only', permission_scope: 'acl', owner_team: 'kb', status: 'active', registered_by: 'system', created_at: '2026-07-01 10:00' },
      { tool_name: 'u8_writeback', version: '1.0', risk_level: 'high_write', permission_scope: 'approval', owner_team: 'erp', status: 'active', registered_by: 'system', created_at: '2026-07-05 09:00' },
      { tool_name: 'ontology_resolve', version: '1.1', risk_level: 'low_write', permission_scope: 'steward', owner_team: 'kb', status: 'disabled', registered_by: 'admin', created_at: '2026-07-08 14:00' },
    ]
    drift.value = ['spec 漂移（代码 ≠ DB）: ontology_resolve@1.1']
    uncertain.value = [
      { invocation_id: 'inv_demo1', run_id: 'run_demo1', step_no: 3, tool_name: 'u8_writeback', status: 'uncertain', policy_decision: 'require_approval', approval_request_id: 'ap1', idempotency_key: 'r1:3:u8', args_digest: 'a1b2c3d4', error_text: 'stale executing（进程崩溃/超时僵尸，900s 无收尾）', started_at: '2026-07-13 16:02', ended_at: null },
      { invocation_id: 'inv_demo2', run_id: 'run_demo2', step_no: 2, tool_name: 'u8_writeback', status: 'uncertain', policy_decision: 'authorized', approval_request_id: null, idempotency_key: 'r2:2:u8', args_digest: 'e5f6a7b8', error_text: 'stale executing（进程崩溃/超时僵尸，1800s 无收尾）', started_at: '2026-07-12 11:30', ended_at: null },
    ]
    supported.value = true
    return
  }
  if (!force && Date.now() - lastLoadedAt < STALE_MS) return
  lastLoadedAt = Date.now()
  loadError.value = ''
  try {
    const [t, inv] = await Promise.all([
      apiJson<{ items: AgentToolRow[]; disabled: string[]; drift: string[] }>('/api/agent/tools', { auth: true }),
      apiJson<{ items: AgentInvocationRow[] }>('/api/agent/invocations?status=uncertain&limit=100', { auth: true }),
    ])
    if (fp !== identityFingerprint()) return   // 身份已切换：旧身份响应整体丢弃
    tools.value = t.items || []
    drift.value = t.drift || []
    uncertain.value = inv.items || []
    supported.value = true
  } catch (e) {
    if (fp !== identityFingerprint()) return
    lastLoadedAt = 0
    if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
      // flag 未开 / 无权：tab 自隐，不造噪声
      tools.value = []; drift.value = []; uncertain.value = []
      supported.value = false
      return
    }
    loadError.value = '加载失败，请重试'
  }
}

/** kill switch：停用/恢复一个工具。⚠️ 服务端按 tool_name 整体生效（同名全部版本一起翻转），
 *  UI 已按工具分组渲染单开关。成功后本地翻转该工具全部行的 status（响应只回 {tool_name,status,rows}）。 */
async function toggleTool(toolName: string, disabled: boolean, reason?: string): Promise<boolean> {
  syncIdentityScope()   // 处置入口同样先对账身份（与 useOntology 处置一致；服务端授权仍是硬校验）
  const { notice } = useDialog()
  const r = await withInflight(`tool:${toolName}`, async () => {
    try {
      const resp = await apiJson<{ tool_name: string; status: string }>('/api/agent/tools/toggle', {
        method: 'POST', auth: true,
        body: JSON.stringify({ tool_name: toolName, disabled, ...(reason ? { reason } : {}) }),
      })
      const next = resp?.status || (disabled ? 'disabled' : 'active')
      tools.value = tools.value.map((t) => (t.tool_name === toolName ? { ...t, status: next } : t))
      return true
    } catch (e: any) {
      void notice({ title: disabled ? '停用失败' : '恢复失败', message: e?.message || '请求失败，请重试', danger: true })
      return false
    }
  })
  return r === true
}

/** uncertain 对账：人工核实副作用后回填权威状态（confirmed_succeeded/confirmed_failed）。
 *  note 必填（服务端 422 兜底）；409=已被处置/状态已变 → 强刷队列（与审批 409 同款）。 */
async function resolveInvocation(inv: AgentInvocationRow, resolution: 'confirmed_succeeded' | 'confirmed_failed', note: string): Promise<boolean> {
  syncIdentityScope()   // 同 toggleTool
  const { notice } = useDialog()
  const r = await withInflight(`inv:${inv.invocation_id}`, async () => {
    try {
      await apiJson('/api/agent/invocations/resolve', {
        method: 'POST', auth: true,
        body: JSON.stringify({ invocation_id: inv.invocation_id, resolution, note }),
      })
      uncertain.value = uncertain.value.filter((x) => x.invocation_id !== inv.invocation_id)
      return true
    } catch (e: any) {
      const conflict = e instanceof ApiError && e.status === 409
      void notice({ title: '对账处置失败', message: e?.message || '请求失败，请重试', danger: true })
      if (conflict) void loadAgentGovernance(true)   // 已被他人处置/状态已变 → 强刷
      return false
    }
  })
  return r === true
}

export function useAgentGovernance() {
  return {
    agentTools: tools,
    agentToolDrift: drift,
    agentUncertain: uncertain,
    agentGovSupported: computed(() => supported.value),
    agentGovError: loadError,
    isAgentGovBusy: isBusy,
    loadAgentGovernance,
    toggleTool,
    resolveInvocation,
  }
}

// 探测/数据/错误/在途/freshness 全部作废（运行期身份切换与测试复位共用）。
function _resetAgentGovernance() {
  tools.value = []
  drift.value = []
  uncertain.value = []
  supported.value = null
  loadError.value = ''
  inflight.value = new Set()
  lastLoadedAt = 0
}

export function __resetAgentGovernance() {
  _resetAgentGovernance()
  __resetIdentityScope()   // 测试复位顺带忘掉已观测身份（下次 sync 首见采纳）
}

// P0-D：身份/token 变更 → 同步清空（identityScope 统一触发）。
registerIdentityScopedStore('agentGovernance', _resetAgentGovernance)
