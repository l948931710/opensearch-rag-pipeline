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

  it('撤销（DEV preview，确认框确认 → 输入理由确认）→ 本地移除该行', async () => {
    const pinia = activate(identity(), 'dev-preview')
    const kb = useKb()
    ;(kb as any).accessGrants.value = [GRANT, { ...GRANT, id: 'ag2', doc_title: '另一篇' }]
    const w = mount(AccessGrantList, { global: { plugins: [pinia] } })
    const { dialog, onConfirm } = useDialog()
    await w.findAll('button')[0].trigger('click')      // onRevoke → 打开确认框
    await flushPromises()
    expect(dialog.value.open).toBe(true)
    expect(dialog.value.kind).toBe('confirm')
    onConfirm()                                         // 确认 → 接着打开理由输入框
    await flushPromises()
    expect(dialog.value.kind).toBe('prompt')
    dialog.value.value = '离职收回'
    onConfirm()                                         // 确认理由 → revokeAccess
    await flushPromises()
    expect(kb.accessGrants.value.map((g: AccessGrantItem) => g.id)).toEqual(['ag2'])
  })

  it('撤销取消（确认框点取消）→ 不动、不进理由框', async () => {
    const pinia = activate(identity(), 'dev-preview')
    const kb = useKb()
    ;(kb as any).accessGrants.value = [GRANT]
    const w = mount(AccessGrantList, { global: { plugins: [pinia] } })
    const { dialog, onCancel } = useDialog()
    await w.findAll('button')[0].trigger('click')
    await flushPromises()
    expect(dialog.value.open).toBe(true)
    onCancel()                                          // 取消 → confirm(false)，不再弹理由框
    await flushPromises()
    expect(dialog.value.open).toBe(false)
    expect(kb.accessGrants.value.map((g: AccessGrantItem) => g.id)).toEqual(['ag1'])
  })
})
