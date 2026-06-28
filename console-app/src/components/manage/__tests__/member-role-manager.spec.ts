import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import { apiJson } from '@/lib/api'
import MemberRoleManager from '@/components/manage/MemberRoleManager.vue'
import { useKb, __resetKb, type AdminItem } from '@/composables/useKb'

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, apiJson: vi.fn() }
})

beforeEach(() => { __resetKb(); (apiJson as any).mockReset(); vi.unstubAllGlobals() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '张三', role: 'kb_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: [], ...over }
}
function activate(id: Identity, token = 't') {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token, ready: true } } })
  setActivePinia(pinia)
  return pinia
}
const ADMINS: AdminItem[] = [
  { user_id: 'mgr1', user_name: '王伟', role: 'dept_admin', managed_owner_depts: ['marketing'] },
  { user_id: 'kb1', user_name: '系统管理员', role: 'kb_admin', managed_owner_depts: [] },
]

describe('loadAdminGrants — 仅 kb_admin', () => {
  it('kb_admin → 拉 /api/kb/admin-grants 回填名单 + grantable', async () => {
    activate(identity())
    const kb = useKb()
    ;(apiJson as any).mockResolvedValue({ items: ADMINS, grantable_owner_depts: ['marketing', 'finance'] })
    await kb.loadAdminGrants()
    expect(kb.adminGrants.value.map((a) => a.user_id)).toEqual(['mgr1', 'kb1'])
    expect(kb.grantableDepts.value).toContain('finance')
    expect(apiJson).toHaveBeenCalledWith('/api/kb/admin-grants', { auth: true })
  })

  it('非 kb_admin（dept_admin）→ 空、不打接口', async () => {
    activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    await kb.loadAdminGrants()
    expect(kb.adminGrants.value).toEqual([])
    expect(apiJson).not.toHaveBeenCalled()
  })
})

describe('MemberRoleManager 渲染', () => {
  function mountIt(admins: AdminItem[], grantable = ['marketing', 'finance'], token = 't') {
    const pinia = activate(identity(), token)
    const kb = useKb()
    ;(kb as any).adminGrants.value = admins
    ;(kb as any).grantableDepts.value = grantable
    return { w: mount(MemberRoleManager, { global: { plugins: [pinia] } }), kb }
  }

  it('有数据 → 部门管理员行 + kb_admin 受保护行', () => {
    const { w } = mountIt(ADMINS)
    expect(w.text()).toContain('部门管理员')
    expect(w.text()).toContain('王伟')
    expect(w.text()).toContain('知识库管理员')
    expect(w.text()).toContain('受保护')
  })

  it('无部门管理员 → 占位文案', () => {
    const { w } = mountIt([ADMINS[1]])   // 只有 kb_admin
    expect(w.text()).toContain('暂无部门管理员')
  })
})

describe('grant/revoke 组合式逻辑（DEV preview）', () => {
  it('grantDeptAdmin 新增一行;再次提交同人 = 覆盖其 managed depts', async () => {
    vi.stubGlobal('alert', vi.fn())
    activate(identity(), 'dev-preview')
    const kb = useKb()
    ;(kb as any).adminGrants.value = []
    await kb.grantDeptAdmin('newmgr', '赵六', ['marketing', 'finance'], '测试')
    expect(kb.adminGrants.value.find((a) => a.user_id === 'newmgr')?.managed_owner_depts).toEqual(['marketing', 'finance'])
    await kb.grantDeptAdmin('newmgr', '赵六', ['hr'], '')   // 覆盖
    expect(kb.adminGrants.value.find((a) => a.user_id === 'newmgr')?.managed_owner_depts).toEqual(['hr'])
  })

  it('revokeAdminGrant 撤单部门 → 该 dept 移除;撤全部 → 整行移出（降级）', async () => {
    activate(identity(), 'dev-preview')
    const kb = useKb()
    ;(kb as any).adminGrants.value = [
      { user_id: 'mgr1', user_name: '王伟', role: 'dept_admin', managed_owner_depts: ['marketing', 'finance'] },
      { user_id: 'kb1', user_name: 'KB', role: 'kb_admin', managed_owner_depts: [] },
    ]
    await kb.revokeAdminGrant('mgr1', 'finance')   // 撤一个
    expect(kb.adminGrants.value.find((a) => a.user_id === 'mgr1')?.managed_owner_depts).toEqual(['marketing'])
    await kb.revokeAdminGrant('mgr1', '')          // 撤全部 → 降级移出
    expect(kb.adminGrants.value.find((a) => a.user_id === 'mgr1')).toBeUndefined()
    expect(kb.adminGrants.value.find((a) => a.user_id === 'kb1')).toBeDefined()   // kb_admin 不受影响
  })
})
