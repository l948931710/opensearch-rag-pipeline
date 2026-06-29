import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import AccessRequestQueue from '@/components/manage/AccessRequestQueue.vue'
import AccessSyncPill from '@/components/manage/AccessSyncPill.vue'
import Sidebar from '@/components/shell/Sidebar.vue'
import { useKb, __resetKb, type AccessRequestItem } from '@/composables/useKb'

beforeEach(() => { vi.restoreAllMocks(); __resetKb() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '张三', role: 'kb_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'], ...over }
}
function activate(id: Identity, token = 't') {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token, ready: true } } })
  setActivePinia(pinia)
  return pinia
}
const REQ: AccessRequestItem = {
  id: 'ar1', doc_id: 'D1', doc_title: '营销规范', owner_dept: 'marketing',
  requester_dept: 'production', requester_name: '王伟', permission_level: 'dept_internal', reason: '需引用', created_at: '2026-06-26',
}

describe('reviewCount — 待审核单一来源', () => {
  it('kb_admin = 待审批上传 + 授权申请；dept_admin = 仅授权申请', () => {
    activate(identity({ role: 'kb_admin' }))
    const kb = useKb()
    ;(kb as any).approvals.value = [{ doc_id: 'x', version_no: 1 }]
    ;(kb as any).accessRequests.value = [REQ]
    expect(kb.reviewCount.value).toBe(2)

    __resetKb()
    activate(identity({ role: 'dept_admin' }))
    const kb2 = useKb()
    ;(kb2 as any).approvals.value = [{ doc_id: 'x', version_no: 1 }]   // dept_admin 不该有上传审批 → 不计入
    ;(kb2 as any).accessRequests.value = [REQ]
    expect(kb2.reviewCount.value).toBe(1)
  })
})

describe('AccessRequestQueue', () => {
  function mountQueue(reqs: AccessRequestItem[], token = 't') {
    const pinia = activate(identity(), token)
    ;(useKb() as any).accessRequests.value = reqs
    return mount(AccessRequestQueue, { global: { plugins: [pinia] } })
  }

  it('空 → 整块不渲染（无后端不造占位噪声）', () => {
    expect(mountQueue([]).find('section').exists()).toBe(false)
  })

  it('有数据 → 行 + 绿头计数 + 授权/驳回', () => {
    const w = mountQueue([REQ])
    expect(w.text()).toContain('授权申请')
    expect(w.text()).toContain('申请访问《营销规范》')
    expect(w.text()).toContain('授权')
    expect(w.text()).toContain('驳回')
  })

  it('approveAccess（DEV preview）→ 本地移除该行', async () => {
    activate(identity(), 'dev-preview')
    const kb = useKb()
    ;(kb as any).accessRequests.value = [REQ, { ...REQ, id: 'ar2' }]
    await kb.approveAccess(REQ)
    expect(kb.accessRequests.value.map((r) => r.id)).toEqual(['ar2'])
  })
})

describe('Sidebar — 知识库管理入口待审核角标', () => {
  const stubs = { RouterLink: { props: ['to'], template: '<a class="rl" :data-to="to"><slot /></a>' } }

  it('reviewCount>0 → 入口出现数字角标', () => {
    const pinia = activate(identity())
    ;(useKb() as any).approvals.value = [{ doc_id: 'x', version_no: 1 }]
    ;(useKb() as any).accessRequests.value = [REQ, { ...REQ, id: 'ar2' }]
    const w = mount(Sidebar, { global: { plugins: [pinia], stubs } })
    expect(w.text()).toContain('知识库管理')
    expect(w.find('[aria-label="待审核 3 项"]').exists()).toBe(true)   // 1 上传审批 + 2 授权申请
  })

  it('reviewCount=0 → 无角标', () => {
    const pinia = activate(identity())
    const w = mount(Sidebar, { global: { plugins: [pinia], stubs } })
    expect(w.text()).toContain('知识库管理')
    expect(w.find('[aria-label^="待审核"]').exists()).toBe(false)
  })
})


describe('accessStateOf — 申请人侧 4 态派生', () => {
  it('approved+projected→projected · approved+pending_sync→approved_pending_sync · pending · rejected→none', () => {
    activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).myAccessReqs.value = new Map([
      ['DA', { status: 'approved', sync_state: 'projected' }],
      ['DB', { status: 'approved', sync_state: 'pending_sync' }],
      ['DC', { status: 'pending', sync_state: 'n/a' }],
      ['DD', { status: 'rejected', sync_state: 'n/a' }],
    ])
    expect(kb.accessStateOf('DA')).toBe('projected')
    expect(kb.accessStateOf('DB')).toBe('approved_pending_sync')
    expect(kb.accessStateOf('DC')).toBe('pending')
    expect(kb.accessStateOf('DD')).toBe('none')          // 驳回 → 可重新申请
    expect(kb.accessStateOf('DX')).toBe('none')          // 无 row
  })

  it('乐观标记：本会话刚提交（requestedDocIds）→ pending（权威态回灌前）', () => {
    activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).requestedDocIds.value = new Set(['DZ'])
    expect(kb.accessStateOf('DZ')).toBe('pending')
  })
})

describe('loadMyAccessRequests — 终态清乐观标记（B6）', () => {
  it('权威回灌「已驳回」→ 清乐观 requestedDocIds，accessStateOf 回 none（不再卡「审批中」）', async () => {
    activate(identity())
    const kb = useKb()
    ;(kb as any).requestedDocIds.value = new Set(['DZ'])
    expect(kb.accessStateOf('DZ')).toBe('pending')   // 回灌前：乐观显审批中
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (path.startsWith('/api/kb/my-access-requests')) {
        return { ok: true, status: 200, json: async () => ({ items: [{ id: 'm1', doc_id: 'DZ', status: 'rejected', sync_state: 'n/a' }] }) }
      }
      return { ok: false, status: 404, json: async () => ({}) }
    }))
    await kb.loadMyAccessRequests()
    expect((kb as any).requestedDocIds.value.has('DZ')).toBe(false)   // 乐观标记被清
    expect(kb.accessStateOf('DZ')).toBe('none')                       // 可重新申请
  })
})

describe('AccessSyncPill — 独立同步态徽章', () => {
  it('projected→已放行（live 调）· approved_pending_sync→同步中（busy 调）', () => {
    const p = mount(AccessSyncPill, { props: { state: 'projected' } })
    expect(p.text()).toContain('已放行')
    expect(p.find('.text-st-live').exists()).toBe(true)
    const s = mount(AccessSyncPill, { props: { state: 'approved_pending_sync' } })
    expect(s.text()).toContain('同步中')
    expect(s.find('.text-st-busy').exists()).toBe(true)
  })
})
