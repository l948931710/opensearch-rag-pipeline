import { computed, ref } from 'vue'
import { useSession } from '@/stores/session'
import { ApiError, apiFetch, apiJson } from '@/lib/api'
import { useDialog } from '@/composables/useDialog'

// Agent 高风险操作审批队列（WS3 审批闭环前端；报告 §N「审批队列挂进 ManageView」）。
// 后端：GET /api/agent/approvals（kb_admin 全量 / dept_admin 按 approver_scope）+
// POST /api/agent/approve（四处置；职责分离在服务端硬校验——发起人只能终止撤回）。
// RAG_AGENT_ENABLE 关闭时端点 404 → supported=false，ManageView 直接不渲染该 tab（功能未开不造噪声）。

export interface AgentApprovalItem {
  request_id: string
  run_id: string
  call_id: string
  tool_name: string
  tool_version: string
  proposed_args: Record<string, unknown> | null   // 服务端脱敏后参数（渲染安全）
  args_digest: string
  render_summary: string | null
  requested_by: string
  requested_dept: string | null
  approver_scope: string
  status: string
  expires_at: string | null
  created_at: string | null
  decided_at: string | null
}

export type AgentDecision = 'approved' | 'rejected_feedback' | 'rejected_terminate'

const items = ref<AgentApprovalItem[]>([])
// null=尚未探测（tab 先不显）；false=404（flag 未开）；true=端点在。
const supported = ref<boolean | null>(null)
const loadError = ref('')
const inflight = ref<Set<string>>(new Set())

const STALE_MS = 30_000
let lastLoadedAt = 0

function isBusy(key: string): boolean { return inflight.value.has(key) }
async function withInflight<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
  if (inflight.value.has(key)) return undefined
  inflight.value = new Set(inflight.value).add(key)
  try { return await fn() } finally { const n = new Set(inflight.value); n.delete(key); inflight.value = n }
}

async function loadAgentApprovals(force = false) {
  const s = useSession()
  if (!s.identity?.canManage) { items.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    items.value = [
      { request_id: 'ap1', run_id: 'r1', call_id: 'c1', tool_name: 'u8_writeback', tool_version: '1.0', proposed_args: { qty: 120, item: 'PP 刀叉 8寸' }, args_digest: 'd', render_summary: 'u8_writeback(item, qty)', requested_by: 'user_wang', requested_dept: 'production', approver_scope: 'production', status: 'pending', expires_at: '2026-07-12 20:00', created_at: '2026-07-09 20:00', decided_at: null },
      { request_id: 'ap2', run_id: 'r2', call_id: 'c1', tool_name: 'u8_writeback', tool_version: '1.0', proposed_args: { qty: 40, item: '纸杯 12oz' }, args_digest: 'd', render_summary: 'u8_writeback(item, qty)', requested_by: 'user_li', requested_dept: 'production', approver_scope: 'production', status: 'pending', expires_at: '2026-07-12 18:30', created_at: '2026-07-09 18:30', decided_at: null },
    ]
    supported.value = true
    return
  }
  if (!force && Date.now() - lastLoadedAt < STALE_MS) return
  lastLoadedAt = Date.now()
  loadError.value = ''
  try {
    const r = await apiJson<{ items: AgentApprovalItem[] }>('/api/agent/approvals', { auth: true })
    items.value = r.items || []
    supported.value = true
  } catch (e) {
    lastLoadedAt = 0
    items.value = []
    if (e instanceof ApiError && e.status === 404) { supported.value = false; return }   // flag 未开：静默隐藏
    if (e instanceof ApiError && e.status === 403) { supported.value = false; return }   // 员工/无审批权：不显 tab
    loadError.value = '加载失败，请重试'
  }
}

/** 提交处置并（服务端）resume 续跑。approve 返回的是续跑 run 的 SSE 流——审批人不需要
 *  旁观发起人的 run，拿到 2xx 即取消读流（run 在服务端继续，落库在 run 完成侧，断流无损）。 */
async function decideAgentApproval(d: AgentApprovalItem, kind: AgentDecision, reason?: string) {
  const { notice } = useDialog()
  await withInflight(`agent:${d.request_id}`, async () => {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') {
      items.value = items.value.filter((x) => x.request_id !== d.request_id)
      return
    }
    const outcome: Record<string, unknown> = { kind }
    if (kind === 'rejected_feedback') outcome.reason = reason || '审批未通过'
    try {
      const res = await apiFetch('/api/agent/approve', {
        method: 'POST', auth: true,
        body: JSON.stringify({
          run_id: d.run_id, outcome,
          // 幂等键 = 请求+处置（客户端重试同一意图不产生第二条 decision）
          idempotency_key: `${d.request_id}:${kind}`,
        }),
      })
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try { detail = (await res.json())?.detail || detail } catch { /* SSE/非 JSON */ }
        void notice({ title: '审批提交失败', message: detail, danger: true })
        if (res.status === 409) await loadAgentApprovals(true)   // 已被他人处置/过期 → 刷新队列
        return
      }
      void res.body?.cancel()   // 不旁观续跑流
      items.value = items.value.filter((x) => x.request_id !== d.request_id)
    } catch {
      void notice({ title: '审批提交失败', message: '网络异常，请重试', danger: true })
    }
  })
}

const agentApprovalCount = computed(() => items.value.length)

export function useAgentApprovals() {
  return {
    agentApprovals: items,
    agentApprovalsSupported: supported,
    agentApprovalError: loadError,
    agentApprovalCount,
    isAgentApprovalBusy: isBusy,
    loadAgentApprovals,
    decideAgentApproval,
  }
}

export function __resetAgentApprovals() {
  items.value = []
  supported.value = null
  loadError.value = ''
  inflight.value = new Set()
  lastLoadedAt = 0
}
