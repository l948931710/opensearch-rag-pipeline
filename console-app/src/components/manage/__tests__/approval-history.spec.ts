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

  it('dept_admin 只见两类 chip（无 上传审批/成员授权）', () => {
    const w = mountView([ACCESS])
    expect(w.text()).toContain('访问授权')
    expect(w.text()).toContain('知识贡献')
    expect(w.text()).not.toContain('上传审批')
    expect(w.text()).not.toContain('成员授权')
  })

  it('kb_admin 见四类 chip', () => {
    const w = mountView([ACCESS, UPLOAD, ADMIN], 'kb_admin')
    expect(w.text()).toContain('上传审批')
    expect(w.text()).toContain('成员授权')
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

  // ── 分页 + 排序（设计稿 2026-07-19 §2：历史时间线 3 条/页，按决策时间默认新→旧）──
  const histAt = (i: number, decided_at: string): ApprovalHistoryItem => ({ ...ACCESS, title: `记录${i}`, decided_at })

  it('分页：5 条 → 3 条/页；默认新→旧；翻页脚「第 x–y 条 · 共 N 条」', async () => {
    const w = mountView([1, 2, 3, 4, 5].map((i) => histAt(i, `2026-07-0${i} 10:00:00`)))
    // 第 1 页 = 最新三条（05/04/03）
    expect(w.text()).toContain('记录5')
    expect(w.text()).toContain('记录3')
    expect(w.text()).not.toContain('记录2')
    expect(w.find('[data-testid="pager-info"]').text()).toBe('第 1–3 条 · 共 5 条')
    await w.find('[aria-label="下一页"]').trigger('click')
    expect(w.text()).toContain('记录2')
    expect(w.text()).toContain('记录1')
    expect(w.find('[data-testid="pager-info"]').text()).toBe('第 4–5 条 · 共 5 条')
    // 排序切换 → 旧→新 + 回第 1 页
    await w.find('[data-testid="queue-sort"]').trigger('click')
    expect(w.find('[data-testid="queue-sort"]').text()).toContain('旧→新')
    expect(w.text()).toContain('记录1')
    expect(w.text()).not.toContain('记录4')
  })

  it('类型筛选切换 → 回第 1 页（翻页后再筛选不残留越界页码）；≤3 条单页自隐翻页脚', async () => {
    const items = [1, 2, 3, 4].map((i) => histAt(i, `2026-07-0${i} 10:00:00`))
    const w = mountView([...items, { ...CONTRIB, decided_at: '2026-06-01 09:00:00' }])
    await w.find('[aria-label="下一页"]').trigger('click')                          // 第 2 页
    const chip = w.findAll('button').find((b) => b.text() === '知识贡献')!
    await chip.trigger('click')                                                     // 筛选 → 回第 1 页
    expect(w.text()).toContain('龙盛机速度是多少')
    expect(w.text()).not.toContain('记录4')
    expect(w.find('[data-testid="queue-pager"]').exists()).toBe(false)              // 1 条 = 单页自隐
  })
})
