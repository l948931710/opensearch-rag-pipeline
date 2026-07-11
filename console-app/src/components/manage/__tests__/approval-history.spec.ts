import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import { apiJson } from '@/lib/api'
import ApprovalHistory from '@/components/manage/ApprovalHistory.vue'
import { useKb, __resetKb, type ApprovalHistoryItem } from '@/composables/useKb'

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, apiJson: vi.fn() }
})

beforeEach(() => { __resetKb(); (apiJson as any).mockReset(); vi.unstubAllGlobals() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '张三', role: 'dept_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'], ...over }
}
function activate(id: Identity, token = 't') {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token, ready: true } } })
  setActivePinia(pinia)
  return pinia
}

const ACCESS: ApprovalHistoryItem = {
  kind: 'access', action: 'approved', title: '销售SOP', owner_dept: 'marketing', subject: '王伟',
  detail: '生产部引用', extra: '', decided_by: 'da1', decided_by_name: '李娜', decided_at: '2026-06-28 14:00:00',
}
const CONTRIB: ApprovalHistoryItem = {
  kind: 'contribution', action: 'accepted', title: '龙盛机速度是多少', owner_dept: 'production', subject: '孙工',
  detail: '', extra: 'searchable', decided_by: 'mgr2', decided_by_name: '陈立', decided_at: '2026-06-27 09:00:00',
}
const UPLOAD: ApprovalHistoryItem = {
  kind: 'upload', action: 'rejected', title: '旧版规范.pdf', owner_dept: 'marketing', subject: '',
  detail: '内容过期', extra: '', decided_by: 'kb1', decided_by_name: '系统管理员', decided_at: '2026-06-26 17:00:00',
}
const ADMIN: ApprovalHistoryItem = {
  kind: 'admin_grant', action: 'granted', title: 'mgr002', owner_dept: '', subject: 'mgr002',
  detail: 'grant dept_admin mgr002 → quality', extra: '', decided_by: 'kb1', decided_by_name: '系统管理员', decided_at: '2026-06-25 09:00:00',
}
const AGENT: ApprovalHistoryItem = {
  kind: 'agent', action: 'rejected_terminate', title: 'u8_writeback', owner_dept: 'production', subject: '王伟',
  detail: '参数有误', extra: '', decided_by: 'da1', decided_by_name: '李娜', decided_at: '2026-07-10 09:00:00',
}
const AGENT_EXPIRED: ApprovalHistoryItem = {
  kind: 'agent', action: 'expired', title: 'u8_writeback', owner_dept: 'production', subject: '王伟',
  detail: '', extra: '', decided_by: '', decided_by_name: '', decided_at: '2026-07-09 20:00:00',
}

describe('loadApprovalHistory', () => {
  it('canManage → 拉 /api/kb/approval-history 回填', async () => {
    activate(identity())
    const kb = useKb()
    ;(apiJson as any).mockResolvedValue({ items: [ACCESS, CONTRIB] })
    await kb.loadApprovalHistory()
    expect(kb.approvalHistory.value.map((r) => r.kind)).toEqual(['access', 'contribution'])
    expect(apiJson).toHaveBeenCalledWith('/api/kb/approval-history', { auth: true })
  })

  it('非管理员 → 空、不打接口', async () => {
    activate(identity({ canManage: false, role: 'employee' }))
    const kb = useKb()
    await kb.loadApprovalHistory()
    expect(kb.approvalHistory.value).toEqual([])
    expect(apiJson).not.toHaveBeenCalled()
  })

  it('端点出错 → 静默空（不抛）', async () => {
    activate(identity())
    const kb = useKb()
    ;(apiJson as any).mockRejectedValue({ status: 404 })
    await kb.loadApprovalHistory()
    expect(kb.approvalHistory.value).toEqual([])
  })
})

describe('ApprovalHistory', () => {
  function mountView(items: ApprovalHistoryItem[], role: 'dept_admin' | 'kb_admin' = 'dept_admin') {
    const pinia = activate(identity({ role }))
    ;(useKb() as any).approvalHistory.value = items
    return mount(ApprovalHistory, { global: { plugins: [pinia] } })
  }

  it('空 → 显空态占位（tab 内容常驻，不隐藏）', () => {
    const w = mountView([])
    expect(w.find('[data-testid="approval-history"]').exists()).toBe(true)
    expect(w.text()).toContain('暂无审批历史')
  })

  it('有数据 → 时间线渲染类型/动作/标题/决策人', () => {
    const w = mountView([ACCESS, CONTRIB])
    expect(w.text()).toContain('访问授权')
    expect(w.text()).toContain('通过')
    expect(w.text()).toContain('销售SOP')
    expect(w.text()).toContain('申请人 王伟')
    expect(w.text()).toContain('决策人 李娜')
    expect(w.text()).toContain('已入库')          // contribution extra=searchable
  })

  it('dept_admin 见 访问授权/知识贡献/Agent 操作 三类 chip（无 上传审批/成员授权）', () => {
    const w = mountView([ACCESS])
    expect(w.text()).toContain('访问授权')
    expect(w.text()).toContain('知识贡献')
    expect(w.text()).toContain('Agent 操作')
    expect(w.text()).not.toContain('上传审批')
    expect(w.text()).not.toContain('成员授权')
  })

  it('kb_admin 见五类 chip', () => {
    const w = mountView([ACCESS, UPLOAD, ADMIN], 'kb_admin')
    expect(w.text()).toContain('上传审批')
    expect(w.text()).toContain('成员授权')
    expect(w.text()).toContain('Agent 操作')
  })

  it('agent 行：类型标/动作标/发起人/决策人；expired → 过期未审、无决策人', () => {
    const w = mountView([AGENT, AGENT_EXPIRED])
    expect(w.text()).toContain('Agent 操作')
    expect(w.text()).toContain('终止')                 // rejected_terminate → 终止
    expect(w.text()).toContain('u8_writeback')
    expect(w.text()).toContain('发起人 王伟')           // agent 类主体前缀
    expect(w.text()).toContain('决策人 李娜')
    expect(w.text()).toContain('过期未审')              // expired 徽标
  })

  it('Agent 操作 chip 本地过滤', async () => {
    const w = mountView([ACCESS, AGENT])
    const chip = w.findAll('button').find((b) => b.text() === 'Agent 操作')!
    await chip.trigger('click')
    expect(w.text()).toContain('u8_writeback')
    expect(w.text()).not.toContain('销售SOP')
  })

  it('类型 chip 本地过滤（点「知识贡献」→ 只剩贡献行）', async () => {
    const w = mountView([ACCESS, CONTRIB])
    const chip = w.findAll('button').find((b) => b.text() === '知识贡献')!
    await chip.trigger('click')
    expect(w.text()).toContain('龙盛机速度是多少')
    expect(w.text()).not.toContain('销售SOP')
  })

  it('过滤后为空 → 当前筛选无记录', async () => {
    const w = mountView([ACCESS], 'kb_admin')
    const chip = w.findAll('button').find((b) => b.text() === '成员授权')!
    await chip.trigger('click')
    expect(w.text()).toContain('当前筛选无记录')
  })
})
