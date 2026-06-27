import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import DocTable from '@/components/manage/DocTable.vue'
import { useKb, __resetKb, type DocItem } from '@/composables/useKb'

beforeEach(() => { vi.restoreAllMocks(); __resetKb() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '张三', role: 'dept_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'], ...over }
}
function activate(id: Identity, token = 't') {
  const p = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token, ready: true } } })
  setActivePinia(p)
  return p
}
function doc(over: Partial<DocItem>): DocItem {
  return { doc_id: 'd', title: 't', original_filename: 'f', owner_dept: 'marketing', permission_level: 'dept_internal', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '2026-06-26', can_manage: true, ...over }
}

describe('DocTable — 本部门/全部门 切换', () => {
  it('dept_admin 显示切换；kb_admin 不显示（本就全见）', () => {
    const w1 = mount(DocTable, { global: { plugins: [activate(identity({ role: 'dept_admin' }))] } })
    expect(w1.text()).toContain('本部门')
    expect(w1.text()).toContain('全部门')

    __resetKb()
    const w2 = mount(DocTable, { global: { plugins: [activate(identity({ role: 'kb_admin', managedOwnerDepts: ['marketing', 'hr'] }))] } })
    expect(w2.text()).not.toContain('全部门')
  })

  it('setScope 切换作用域并清状态筛选', () => {
    activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).filter.value = '已上线'
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ items: [], has_more: false }), text: async () => '{}' })))
    kb.setScope('all')
    expect(kb.docScope.value).toBe('all')
    expect(kb.filter.value).toBe('')
  })
})

describe('DocTable — 全部门只读浏览', () => {
  it('横幅 + 外部门行「申请授权」、本部门行「退役」', () => {
    const p = activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).docScope.value = 'all'
    ;(kb as any).docs.value = [
      doc({ doc_id: 'mine', owner_dept: 'marketing', can_manage: true }),
      doc({ doc_id: 'foreign', owner_dept: 'hr', can_manage: false }),
    ]
    const w = mount(DocTable, { global: { plugins: [p] } })
    expect(w.text()).toContain('全部门为只读视图')
    expect(w.text()).toContain('其他部门')
    const btns = w.findAll('button').map((b) => b.text())
    expect(btns.filter((t) => t.includes('申请授权')).length).toBe(1)   // 仅外部门 1 行
    expect(btns.filter((t) => t.includes('退役')).length).toBe(1)       // 仅本部门 1 行
  })

  it('外部门行不暴露 升版/退役（只读）', () => {
    const p = activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).docScope.value = 'all'
    ;(kb as any).docs.value = [doc({ doc_id: 'foreign', owner_dept: 'hr', can_manage: false })]
    const w = mount(DocTable, { global: { plugins: [p] } })
    const btns = w.findAll('button').map((b) => b.text())
    expect(btns.some((t) => t.includes('升版'))).toBe(false)
    expect(btns.some((t) => t.includes('退役'))).toBe(false)
    expect(btns.some((t) => t.includes('申请授权'))).toBe(true)
  })
})

describe('授权申请（申请人侧）', () => {
  it('submitAccessRequest（DEV preview）→ requestedDocIds 标记审批中', async () => {
    activate(identity({ role: 'dept_admin' }), 'dev-preview')
    const kb = useKb()
    kb.openAccessRequest(doc({ doc_id: 'foreign', owner_dept: 'hr', can_manage: false }))
    await kb.submitAccessRequest('需引用')
    expect(kb.accessStateOf('foreign')).toBe('pending')
    expect(kb.accessReqDoc.value).toBeNull()
  })
})
