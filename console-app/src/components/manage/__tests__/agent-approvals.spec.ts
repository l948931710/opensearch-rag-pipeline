import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import AgentApprovalQueue from '@/components/manage/AgentApprovalQueue.vue'
import {
  __resetAgentApprovals,
  useAgentApprovals,
  type AgentApprovalItem,
} from '@/composables/useAgentApprovals'

beforeEach(() => { vi.restoreAllMocks(); __resetAgentApprovals() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'admin1', name: '管理员', role: 'dept_admin', aclGroups: ['production'], canManage: true, managedOwnerDepts: ['production'], ...over }
}
function activate(id: Identity, token = 't') {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token, ready: true } } })
  setActivePinia(pinia)
  return pinia
}
function areq(over: Partial<AgentApprovalItem> = {}): AgentApprovalItem {
  return {
    request_id: 'ap1', run_id: 'r1', call_id: 'c1', tool_name: 'u8_writeback', tool_version: '1.0',
    proposed_args: { qty: 7 }, args_digest: 'd', render_summary: 'u8_writeback(qty)',
    requested_by: 'user_wang', requested_dept: 'production', approver_scope: 'production',
    status: 'pending', expires_at: '2026-07-12 20:00:00', created_at: '2026-07-09 20:00:00',
    decided_at: null, ...over,
  }
}

describe('useAgentApprovals — 加载与端点探测', () => {
  it('200 → items 填充 + supported=true（tab 出现的判据）', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => ({ items: [areq()] }),
    })))
    const a = useAgentApprovals()
    await a.loadAgentApprovals(true)
    expect(a.agentApprovals.value.length).toBe(1)
    expect(a.agentApprovalsSupported.value).toBe(true)
    expect(a.agentApprovalCount.value).toBe(1)
  })

  it('404（RAG_AGENT_ENABLE 未开）→ supported=false 静默、不置错（tab 自隐不造噪声）', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) })))
    const a = useAgentApprovals()
    await a.loadAgentApprovals(true)
    expect(a.agentApprovalsSupported.value).toBe(false)
    expect(a.agentApprovalError.value).toBe('')
  })

  it('403（无审批权）→ 同样自隐；5xx → 置错可重试', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 403, json: async () => ({}) })))
    const a = useAgentApprovals()
    await a.loadAgentApprovals(true)
    expect(a.agentApprovalsSupported.value).toBe(false)

    __resetAgentApprovals()
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })))
    const b = useAgentApprovals()
    await b.loadAgentApprovals(true)
    expect(b.agentApprovalError.value).toContain('加载失败')
  })

  it('canManage=false → 不打网络（员工不拉队列）', async () => {
    activate(identity({ canManage: false, role: 'employee' }))
    const spy = vi.fn()
    vi.stubGlobal('fetch', spy)
    await useAgentApprovals().loadAgentApprovals(true)
    expect(spy).not.toHaveBeenCalled()
  })
})

describe('useAgentApprovals — 处置提交', () => {
  it('批准 → POST /api/agent/approve 带 run_id/outcome/幂等键；2xx 取消读流 + 本地移除', async () => {
    activate(identity())
    const cancel = vi.fn()
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string, init: any) => {
      calls.push([path, init])
      return { ok: true, status: 200, body: { cancel } }
    }))
    const a = useAgentApprovals()
    a.agentApprovals.value = [areq(), areq({ request_id: 'ap2', run_id: 'r2' })]
    await a.decideAgentApproval(areq(), 'approved')
    expect(calls.length).toBe(1)
    expect(calls[0][0]).toBe('/api/agent/approve')
    const body = JSON.parse(calls[0][1].body)
    expect(body.run_id).toBe('r1')
    expect(body.outcome).toEqual({ kind: 'approved' })
    expect(body.idempotency_key).toBe('ap1:approved')   // 同意图重试不产生第二条 decision
    expect(cancel).toHaveBeenCalled()                   // 审批人不旁观续跑 SSE
    expect(a.agentApprovals.value.map((x) => x.request_id)).toEqual(['ap2'])
  })

  it('驳回反馈 → outcome 带 reason；409（已被他人处置）→ 弹错并强刷队列', async () => {
    activate(identity())
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: any) => {
      calls.push([path, init])
      if (path === '/api/agent/approve') return { ok: false, status: 409, json: async () => ({ detail: '已被处置' }) }
      return { ok: true, status: 200, json: async () => ({ items: [] }) }
    }))
    const a = useAgentApprovals()
    a.agentApprovals.value = [areq()]
    await a.decideAgentApproval(areq(), 'rejected_feedback', '金额过大')
    const body = JSON.parse(calls[0][1].body)
    expect(body.outcome).toEqual({ kind: 'rejected_feedback', reason: '金额过大' })
    // 409 → 触发一次队列强刷（第二个 fetch 打的是列表端点）
    expect(calls.some(([p]) => String(p).startsWith('/api/agent/approvals'))).toBe(true)
  })
})

describe('AgentApprovalQueue 组件', () => {
  function mountQueue(items: AgentApprovalItem[], id = identity()) {
    const pinia = activate(id)
    useAgentApprovals().agentApprovals.value = items
    return mount(AgentApprovalQueue, { global: { plugins: [pinia] } })
  }

  it('空 → 显式空态（独立 tab 不自隐白屏）', () => {
    const w = mountQueue([])
    expect(w.text()).toContain('当前没有待审批的 Agent 操作')
  })

  it('有数据 → 工具名/参数/发起人/过期时间 + 三处置按钮', () => {
    const w = mountQueue([areq()])
    expect(w.text()).toContain('u8_writeback')
    expect(w.text()).toContain('qty=7')
    expect(w.text()).toContain('user_wang')
    expect(w.text()).toContain('批准')
    expect(w.text()).toContain('驳回')
    expect(w.text()).toContain('终止')
  })

  it('职责分离：自己发起的申请 → 批准/驳回禁用、按钮变「撤回」', () => {
    const w = mountQueue([areq({ requested_by: 'admin1' })])   // == identity.userId
    expect(w.text()).toContain('我发起的')
    expect(w.text()).toContain('撤回')
    const disabled = w.findAll('button').filter((b) => (b.attributes('disabled') !== undefined))
    // 批准 + 驳回两枚被禁；撤回可用
    expect(disabled.length).toBe(2)
  })
})
