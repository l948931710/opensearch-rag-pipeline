import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia } from 'pinia'
import { createTestingPinia } from '@pinia/testing'
import type { Identity } from '@/stores/session'
import { useKb, __resetKb, type DocItem } from '@/composables/useKb'
import { mount } from '@vue/test-utils'
import DocTable from '@/components/manage/DocTable.vue'

/**
 * P2-12：退役/恢复对**公开文档需 kb_admin**（后端 kb_retire:3108 / kb_restore:3214 均 403）。
 * 前端此前不做前置判断 ⇒ dept_admin 在公开文档上点「退役下线」会先弹**危险确认框**、
 * 确认后才吃 403 —— 一个注定失败的破坏性确认。
 * 批量侧同病：「退役 N 篇」把注定 403 的公开文档也计进去，事后再报「M 篇失败」。
 */
beforeEach(() => { vi.restoreAllMocks(); __resetKb() })

function activate(role: Identity['role']) {
  const id: Identity = { userId: 'u', name: 'x', role, aclGroups: ['hr'], canManage: true, managedOwnerDepts: ['hr'] }
  setActivePinia(createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 't', ready: true } } }))
}
const doc = (id: string, perm: string): DocItem => ({
  doc_id: id, title: id, original_filename: id, owner_dept: 'hr', permission_level: perm,
  current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '2026-08-03',
} as DocItem)

describe('批量退役：计数与实际请求必须一致', () => {
  it('dept_admin：公开文档被排除，只对可退役的发请求', async () => {
    activate('dept_admin')
    const kb = useKb()
    ;(kb as unknown as { docs: { value: DocItem[] } }).docs.value = [doc('A', 'public'), doc('B', 'dept_internal')]
    kb.toggleSelect('A'); kb.toggleSelect('B')
    const sent: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (u: string, init?: { body?: string }) => {
      // 只记退役请求——_bulkRun 批末还会发一次 loadDocs 的 GET
      if (String(u).includes('/api/kb/retire')) sent.push(JSON.parse(String(init?.body || '{}')).doc_id)
      return { ok: true, status: 200, json: async () => ({}) }
    }))
    await kb.bulkRetire()
    expect(sent).toEqual(['B'])   // A 是 public 且非 kb_admin ⇒ 绝不发注定 403 的请求
  })

  it('kb_admin：公开文档照常退役（规则只针对 dept_admin）', async () => {
    activate('kb_admin')
    const kb = useKb()
    ;(kb as unknown as { docs: { value: DocItem[] } }).docs.value = [doc('A', 'public')]
    kb.toggleSelect('A')
    const sent: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (u: string, init?: { body?: string }) => {
      // 只记退役请求——_bulkRun 批末还会发一次 loadDocs 的 GET
      if (String(u).includes('/api/kb/retire')) sent.push(JSON.parse(String(init?.body || '{}')).doc_id)
      return { ok: true, status: 200, json: async () => ({}) }
    }))
    await kb.bulkRetire()
    expect(sent).toEqual(['A'])
  })
})


describe('行级菜单：公开文档的退役/恢复按钮前置禁用', () => {
  async function openMenuFor(role: Identity['role'], perm: string, badge = '已上线') {
    const id: Identity = { userId: 'u', name: 'x', role, aclGroups: ['hr'], canManage: true, managedOwnerDepts: ['hr'] }
    const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 't', ready: true } } })
    setActivePinia(pinia)
    const kb = useKb()
    ;(kb as unknown as { docs: { value: DocItem[] } }).docs.value = [
      { ...doc('A', perm), status_badge: badge, can_manage: true } as DocItem,
    ]
    const w = mount(DocTable, { global: { plugins: [pinia] } })
    const gear = w.findAll('button').find((b) => (b.attributes('aria-label') || '').includes('更多'))
      || w.findAll('button').find((b) => b.html().includes('Settings') || b.classes().join(' ').includes('menu'))
    if (gear) await gear.trigger('click')
    return w
  }

  it('dept_admin + 公开文档 → 退役按钮 disabled 且给出原因', async () => {
    const w = await openMenuFor('dept_admin', 'public')
    const btn = w.find('[data-testid="doc-retire"]')
    expect(btn.exists()).toBe(true)
    expect(btn.attributes('disabled')).toBeDefined()
    expect(btn.attributes('title') || '').toContain('知识库管理员')
  })

  it('dept_admin + 部门内文档 → 可退役（规则只针对 public）', async () => {
    const w = await openMenuFor('dept_admin', 'dept_internal')
    const btn = w.find('[data-testid="doc-retire"]')
    expect(btn.attributes('disabled')).toBeUndefined()
  })

  it('kb_admin + 公开文档 → 可退役', async () => {
    const w = await openMenuFor('kb_admin', 'public')
    expect(w.find('[data-testid="doc-retire"]').attributes('disabled')).toBeUndefined()
  })

  it('已退役的公开文档：dept_admin 的「恢复上线」同样前置禁用（后端 kb_restore 也 403）', async () => {
    const w = await openMenuFor('dept_admin', 'public', '已退役')
    const btn = w.find('[data-testid="doc-restore"]')
    expect(btn.exists()).toBe(true)
    expect(btn.attributes('disabled')).toBeDefined()
  })
})
