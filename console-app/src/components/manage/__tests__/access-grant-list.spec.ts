import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import { apiJson } from '@/lib/api'
import AccessGrantList from '@/components/manage/AccessGrantList.vue'
import { useKb, __resetKb, type AccessGrantItem } from '@/composables/useKb'
import { useDialog } from '@/composables/useDialog'

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
const GRANT: AccessGrantItem = {
  id: 'ag1', doc_id: 'D1', doc_title: '营销规范', owner_dept: 'marketing',
  requester_dept: 'production', requester_name: '王伟', permission_level: 'dept_internal', reason: '引用', decided_at: '2026-06-26',
}

describe('loadAccessGrants', () => {
  it('canManage → 拉 /api/kb/access-grants 回填', async () => {
    activate(identity())
    const kb = useKb()
    ;(apiJson as any).mockResolvedValue({ items: [GRANT] })
    await kb.loadAccessGrants()
    expect(kb.accessGrants.value.map((g) => g.id)).toEqual(['ag1'])
    expect(apiJson).toHaveBeenCalledWith('/api/kb/access-grants', { auth: true })
  })

  it('非管理员 → 空、不打接口', async () => {
    activate(identity({ canManage: false, role: 'employee' }))
    const kb = useKb()
    await kb.loadAccessGrants()
    expect(kb.accessGrants.value).toEqual([])
    expect(apiJson).not.toHaveBeenCalled()
  })

  it('端点出错 → 静默空（不抛）', async () => {
    activate(identity())
    const kb = useKb()
    ;(apiJson as any).mockRejectedValue({ status: 404 })
    await kb.loadAccessGrants()
    expect(kb.accessGrants.value).toEqual([])
  })
})

describe('AccessGrantList', () => {
  function mountList(grants: AccessGrantItem[], token = 't') {
    const pinia = activate(identity(), token)
    ;(useKb() as any).accessGrants.value = grants
    return mount(AccessGrantList, { global: { plugins: [pinia] } })
  }

  it('空 → 整块不渲染', () => {
    expect(mountList([]).find('section').exists()).toBe(false)
  })

  it('有数据 → 已授权头 + 行（可检索《标题》）+ 撤销按钮', () => {
    const w = mountList([GRANT])
    expect(w.text()).toContain('已授权')
    expect(w.text()).toContain('可检索《营销规范》')
    expect(w.text()).toContain('授权于 2026-06-26')
    expect(w.text()).toContain('撤销')
  })

  it('撤销（DEV preview，单个 danger 原因弹窗确认）→ 本地移除该行', async () => {
    // P2 统一：原 confirm+prompt 两段弹窗 → 与驳回同构的单个 danger 原因弹窗（一步说明影响面+采集原因）。
    const pinia = activate(identity(), 'dev-preview')
    const kb = useKb()
    ;(kb as any).accessGrants.value = [GRANT, { ...GRANT, id: 'ag2', doc_title: '另一篇' }]
    const w = mount(AccessGrantList, { global: { plugins: [pinia] } })
    const { dialog, onConfirm } = useDialog()
    // 头部新增排序 chip 后按钮序号不再稳定 → 按文案定位「撤销」
    await w.findAll('button').find((b) => b.text() === '撤销')!.trigger('click')   // onRevoke → 打开原因弹窗（单段）
    await flushPromises()
    expect(dialog.value.open).toBe(true)
    expect(dialog.value.kind).toBe('prompt')
    expect(dialog.value.danger).toBe(true)
    dialog.value.value = '离职收回'
    onConfirm()                                         // 确认理由 → revokeAccess
    await flushPromises()
    expect(kb.accessGrants.value.map((g: AccessGrantItem) => g.id)).toEqual(['ag2'])
  })

  it('撤销取消（原因弹窗点取消）→ 不动', async () => {
    const pinia = activate(identity(), 'dev-preview')
    const kb = useKb()
    ;(kb as any).accessGrants.value = [GRANT]
    const w = mount(AccessGrantList, { global: { plugins: [pinia] } })
    const { dialog, onCancel } = useDialog()
    await w.findAll('button').find((b) => b.text() === '撤销')!.trigger('click')
    await flushPromises()
    expect(dialog.value.open).toBe(true)
    onCancel()                                          // 取消 → promptText(null)，不执行撤销
    await flushPromises()
    expect(dialog.value.open).toBe(false)
    expect(kb.accessGrants.value.map((g: AccessGrantItem) => g.id)).toEqual(['ag1'])
  })

  // ── 分页 + 排序（设计稿 2026-07-19 §2：队列 2 条/页，默认新→旧）──
  const grantAt = (id: string, decided_at: string): AccessGrantItem => ({ ...GRANT, id, doc_title: `文档${id}`, decided_at })

  it('分页：5 条 → 2 条/页；默认新→旧；翻页脚显「第 x–y 条 · 共 N 条」', async () => {
    const w = mountList([
      grantAt('g1', '2026-07-01'), grantAt('g2', '2026-07-02'), grantAt('g3', '2026-07-03'),
      grantAt('g4', '2026-07-04'), grantAt('g5', '2026-07-05'),
    ])
    // 第 1 页 = 最新两条（07-05、07-04）
    expect(w.text()).toContain('文档g5')
    expect(w.text()).toContain('文档g4')
    expect(w.text()).not.toContain('文档g1')
    expect(w.find('[data-testid="queue-pager"]').exists()).toBe(true)
    expect(w.find('[data-testid="pager-info"]').text()).toBe('第 1–2 条 · 共 5 条')
    // 翻到末页 → 最旧一条
    await w.find('[aria-label="下一页"]').trigger('click')
    await w.find('[aria-label="下一页"]').trigger('click')
    expect(w.text()).toContain('文档g1')
    expect(w.find('[data-testid="pager-info"]').text()).toBe('第 5–5 条 · 共 5 条')
  })

  it('排序切换 → 旧→新 + 回第 1 页；单页不显翻页脚', async () => {
    const w = mountList([grantAt('g1', '2026-07-01'), grantAt('g2', '2026-07-02'), grantAt('g3', '2026-07-03')])
    await w.find('[aria-label="下一页"]').trigger('click')       // 先离开第 1 页
    await w.find('[data-testid="queue-sort"]').trigger('click')  // 切旧→新
    expect(w.find('[data-testid="queue-sort"]').text()).toContain('旧→新')
    expect(w.text()).toContain('文档g1')                          // 回第 1 页 = 最旧两条
    expect(w.text()).toContain('文档g2')
    expect(w.text()).not.toContain('文档g3')
    // 单页自隐：2 条数据无翻页脚
    const w2 = mountList([grantAt('h1', '2026-07-01'), grantAt('h2', '2026-07-02')])
    expect(w2.find('[data-testid="queue-pager"]').exists()).toBe(false)
  })

  it('列表变短（撤销后）→ 当前页自动收敛不越界', async () => {
    const pinia = activate(identity())
    const kb = useKb()
    ;(kb as any).accessGrants.value = [grantAt('g1', '2026-07-01'), grantAt('g2', '2026-07-02'), grantAt('g3', '2026-07-03')]
    const w = mount(AccessGrantList, { global: { plugins: [pinia] } })
    await w.find('[aria-label="下一页"]').trigger('click')       // 第 2 页（仅 g1）
    expect(w.find('[data-testid="pager-info"]').text()).toBe('第 3–3 条 · 共 3 条')
    ;(kb as any).accessGrants.value = [grantAt('g2', '2026-07-02'), grantAt('g3', '2026-07-03')]   // 变短到单页
    await flushPromises()
    expect(w.find('[data-testid="queue-pager"]').exists()).toBe(false)   // 单页自隐
    expect(w.text()).toContain('文档g3')                                  // 页码收敛回第 1 页，行照常显示
    expect(w.text()).toContain('文档g2')
  })
})
