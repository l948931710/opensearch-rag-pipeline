import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import AgentApprovalQueue from '@/components/manage/AgentApprovalQueue.vue'
import { __resetAgentApprovals, useAgentApprovals, type AgentApprovalItem } from '@/composables/useAgentApprovals'
import { useDialog } from '@/composables/useDialog'

// 第四处置「修改后批准」（kind=edited，服务端按改后参数重算 digest，重放安全）
// + expires_at 相对化（「3 天后过期」）。独立成文件，不动既有 agent-approvals.spec 的断言。

beforeEach(() => { vi.restoreAllMocks(); __resetAgentApprovals() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'admin1', name: '管理员', role: 'dept_admin', aclGroups: ['production'], canManage: true, managedOwnerDepts: ['production'], ...over }
}
function activate(id: Identity) {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 't', ready: true } } })
  setActivePinia(pinia)
  return pinia
}
function fmt(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
function areq(over: Partial<AgentApprovalItem> = {}): AgentApprovalItem {
  return {
    request_id: 'ap1', run_id: 'r1', call_id: 'c1', tool_name: 'u8_writeback', tool_version: '1.0',
    proposed_args: { qty: 7 }, args_digest: 'd', render_summary: 'u8_writeback(qty)',
    requested_by: 'user_wang', requested_dept: 'production', approver_scope: 'production',
    status: 'pending', expires_at: fmt(new Date(Date.now() + 3 * 86_400_000)),
    created_at: '2026-07-09 20:00:00', decided_at: null, ...over,
  }
}
function mountQueue(items: AgentApprovalItem[], id = identity()) {
  const pinia = activate(id)
  useAgentApprovals().agentApprovals.value = items
  return mount(AgentApprovalQueue, { global: { plugins: [pinia] } })
}

describe('修改后批准（kind=edited）', () => {
  it('他人申请 → 「改参」按钮可用；预填当前参数 JSON，提交 POST outcome={kind:edited, edited_args}', async () => {
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: any) => {
      calls.push([String(path), init])
      return { ok: true, status: 200, body: { cancel: vi.fn() } }
    }))
    const w = mountQueue([areq()])
    const btn = w.findAll('button').find((b) => b.text() === '改参')!
    expect(btn).toBeTruthy()
    await btn.trigger('click')

    const dlg = useDialog()
    expect(dlg.dialog.value.open).toBe(true)
    expect(dlg.dialog.value.value).toContain('"qty"')      // 预填 proposed_args JSON 供编辑
    dlg.dialog.value.value = '{ "qty": 42 }'               // 人工改参
    dlg.onConfirm()
    await vi.waitFor(() => expect(calls.some(([p]) => p === '/api/agent/approve')).toBe(true))

    const body = JSON.parse(calls.find(([p]) => p === '/api/agent/approve')![1].body)
    expect(body.run_id).toBe('r1')
    expect(body.outcome).toEqual({ kind: 'edited', edited_args: { qty: 42 } })
    expect(body.idempotency_key).toBe('ap1:edited')
    // 2xx → 本地移除出队
    expect(useAgentApprovals().agentApprovals.value).toEqual([])
  })

  it('非法 JSON → 告知框拦下，不发请求、不出队', async () => {
    const f = vi.fn()
    vi.stubGlobal('fetch', f)
    const w = mountQueue([areq()])
    await w.findAll('button').find((b) => b.text() === '改参')!.trigger('click')
    const dlg = useDialog()
    dlg.dialog.value.value = '{qty: 不是JSON'
    dlg.onConfirm()
    await vi.waitFor(() => expect(dlg.dialog.value.kind).toBe('notice'))   // 参数格式错误告知
    expect(f).not.toHaveBeenCalled()
    expect(useAgentApprovals().agentApprovals.value).toHaveLength(1)
  })

  it('自己发起的申请 → 不渲染「改参」（职责分离下无可用场景，禁用位仍是 批准/驳回 两枚）', () => {
    const w = mountQueue([areq({ requested_by: 'admin1' })])
    expect(w.findAll('button').some((b) => b.text() === '改参')).toBe(false)
    const disabled = w.findAll('button').filter((b) => b.attributes('disabled') !== undefined)
    expect(disabled.length).toBe(2)
  })
})

describe('expires_at 相对化', () => {
  it('未来 3 天 → 「3 天后过期」；已过期 → 「已过期」；绝对时间挂 title', () => {
    const w = mountQueue([
      areq(),
      areq({ request_id: 'ap2', run_id: 'r2', expires_at: fmt(new Date(Date.now() - 3_600_000)) }),
    ])
    expect(w.text()).toContain('3 天后过期')
    expect(w.text()).toContain('已过期')
    expect(w.html()).toContain('过期（超时视同拒绝）')   // title 提供绝对时间兜底
  })
})
