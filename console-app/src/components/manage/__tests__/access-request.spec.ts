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

describe('reviewCount — 待审核单一来源（职权分流）', () => {
  it('kb_admin = 仅上传审批（只管入库）；dept_admin = 仅授权申请（部门管理员之间的事）', () => {
    // 拍板 2026-07-04：授权申请审批划归 dept_admin，kb_admin 即使 ref 里有数据也不计入待办。
    activate(identity({ role: 'kb_admin' }))
    const kb = useKb()
    ;(kb as any).approvals.value = [{ doc_id: 'x', version_no: 1 }]
    ;(kb as any).accessRequests.value = [REQ]
    expect(kb.reviewCount.value).toBe(1)

    __resetKb()
    activate(identity({ role: 'dept_admin' }))
    const kb2 = useKb()
    ;(kb2 as any).approvals.value = [{ doc_id: 'x', version_no: 1 }]   // dept_admin 不该有上传审批 → 不计入
    ;(kb2 as any).accessRequests.value = [REQ]
    expect(kb2.reviewCount.value).toBe(1)
  })

  it('loadAccessRequests：kb_admin 源头 no-op（不拉不显，兜底 API 仍在后端放行）', async () => {
    activate(identity({ role: 'kb_admin' }))
    const kb = useKb()
    const spy = vi.fn()
    vi.stubGlobal('fetch', spy)
    await kb.loadAccessRequests(true)
    expect(spy).not.toHaveBeenCalled()
    expect((kb as any).accessRequests.value).toEqual([])
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

  it('有数据 → 行 + 橙头计数 + 授权/驳回（待处理=琥珀，设计稿 2026-07-19 §1）', () => {
    const w = mountQueue([REQ])
    expect(w.text()).toContain('授权申请')
    expect(w.text()).toContain('申请访问《营销规范》')
    expect(w.text()).toContain('授权')
    expect(w.text()).toContain('驳回')
    // 头部语义：st-busy 底 + st-busy 计数（原绿头改橙）
    expect(w.find('[class*="bg-st-busy/"]').exists()).toBe(true)
    expect(w.find('span[class*="rounded-full"][class*="bg-st-busy"]').text()).toBe('1')
    expect(w.find('[class*="bg-accent-soft"][class*="border-b"]').exists()).toBe(false)
  })

  it('approveAccess（DEV preview）→ 本地移除该行', async () => {
    activate(identity(), 'dev-preview')
    const kb = useKb()
    ;(kb as any).accessRequests.value = [REQ, { ...REQ, id: 'ar2' }]
    await kb.approveAccess(REQ)
    expect(kb.accessRequests.value.map((r) => r.id)).toEqual(['ar2'])
  })

  // ── 分页 + 排序（设计稿 2026-07-19 §2：队列 2 条/页，按申请时间默认新→旧）──
  const reqAt = (id: string, created_at: string): AccessRequestItem => ({ ...REQ, id, doc_title: `文档${id}`, created_at })

  it('分页：5 条 → 2 条/页；默认新→旧；翻页脚「第 x–y 条 · 共 N 条」', async () => {
    const w = mountQueue([
      reqAt('r1', '2026-07-01 09:00'), reqAt('r2', '2026-07-02 09:00'), reqAt('r3', '2026-07-03 09:00'),
      reqAt('r4', '2026-07-04 09:00'), reqAt('r5', '2026-07-05 09:00'),
    ])
    expect(w.text()).toContain('文档r5')
    expect(w.text()).toContain('文档r4')
    expect(w.text()).not.toContain('文档r1')
    expect(w.find('[data-testid="pager-info"]').text()).toBe('第 1–2 条 · 共 5 条')
    await w.find('[aria-label="下一页"]').trigger('click')
    expect(w.text()).toContain('文档r3')
    expect(w.find('[data-testid="pager-info"]').text()).toBe('第 3–4 条 · 共 5 条')
  })

  it('排序切换 → 旧→新 + 回第 1 页；审批后变短 → 页码收敛不越界', async () => {
    const w = mountQueue([reqAt('r1', '2026-07-01 09:00'), reqAt('r2', '2026-07-02 09:00'), reqAt('r3', '2026-07-03 09:00')], 'dev-preview')
    const kb = useKb()
    await w.find('[data-testid="queue-sort"]').trigger('click')
    expect(w.find('[data-testid="queue-sort"]').text()).toContain('旧→新')
    expect(w.text()).toContain('文档r1')                          // 旧→新第 1 页 = 最旧两条
    expect(w.text()).not.toContain('文档r3')
    // 到第 2 页后审批掉一条 → 只剩 2 条 = 单页，翻页脚自隐、行不丢
    await w.find('[aria-label="下一页"]').trigger('click')
    await kb.approveAccess(kb.accessRequests.value[0])            // DEV preview 本地移除
    await vi.waitFor(() => expect(w.find('[data-testid="queue-pager"]').exists()).toBe(false))
    expect(w.text()).toContain('文档r2')
    expect(w.text()).toContain('文档r3')
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
    // kb_admin 只计上传审批（授权申请已划归 dept_admin 职权，即便 ref 有 2 条也不入角标）
    expect(w.find('[aria-label="待审核 1 项"]').exists()).toBe(true)
  })

  it('reviewCount=0 → 无角标', () => {
    const pinia = activate(identity())
    const w = mount(Sidebar, { global: { plugins: [pinia], stubs } })
    expect(w.text()).toContain('知识库管理')
    expect(w.find('[aria-label^="待审核"]').exists()).toBe(false)
  })
})


describe('accessStateOf — 申请人侧 4 态派生', () => {
  it('approved+projected→projected · approved+pending_sync→approved_pending_sync · pending · rejected→rejected', () => {
    activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).myAccessReqs.value = new Map([
      ['DA', { status: 'approved', sync_state: 'projected', note: '' }],
      ['DB', { status: 'approved', sync_state: 'pending_sync', note: '' }],
      ['DC', { status: 'pending', sync_state: 'n/a', note: '' }],
      ['DD', { status: 'rejected', sync_state: 'n/a', note: '涉密暂不外放' }],
    ])
    expect(kb.accessStateOf('DA')).toBe('projected')
    expect(kb.accessStateOf('DB')).toBe('approved_pending_sync')
    expect(kb.accessStateOf('DC')).toBe('pending')
    // 反馈闭环：驳回是显式态（原折叠回 none，申请人对被驳回无感）；原因可查，可重申
    expect(kb.accessStateOf('DD')).toBe('rejected')
    expect(kb.accessNoteOf('DD')).toBe('涉密暂不外放')
    expect(kb.accessStateOf('DX')).toBe('none')          // 无 row
  })

  it('rejected 后重新申请（乐观标记）→ 回到 pending（不再卡「已驳回」）', () => {
    activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).myAccessReqs.value = new Map([['DD', { status: 'rejected', sync_state: 'n/a', note: 'x' }]])
    ;(kb as any).requestedDocIds.value = new Set(['DD'])
    expect(kb.accessStateOf('DD')).toBe('pending')
  })

  it('乐观标记：本会话刚提交（requestedDocIds）→ pending（权威态回灌前）', () => {
    activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).requestedDocIds.value = new Set(['DZ'])
    expect(kb.accessStateOf('DZ')).toBe('pending')
  })
})

describe('loadMyAccessRequests — 终态清乐观标记（B6）', () => {
  it('权威回灌「已驳回」→ 清乐观 requestedDocIds，accessStateOf 显 rejected（原因随行回灌）', async () => {
    activate(identity())
    const kb = useKb()
    ;(kb as any).requestedDocIds.value = new Set(['DZ'])
    expect(kb.accessStateOf('DZ')).toBe('pending')   // 回灌前：乐观显审批中
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (path.startsWith('/api/kb/my-access-requests')) {
        return { ok: true, status: 200, json: async () => ({ items: [{ id: 'm1', doc_id: 'DZ', status: 'rejected', sync_state: 'n/a', decision_note: '本季度不外放' }] }) }
      }
      return { ok: false, status: 404, json: async () => ({}) }
    }))
    await kb.loadMyAccessRequests()
    expect((kb as any).requestedDocIds.value.has('DZ')).toBe(false)   // 乐观标记被清
    expect(kb.accessStateOf('DZ')).toBe('rejected')                   // 显式驳回态（可重申，原因可查）
    expect(kb.accessNoteOf('DZ')).toBe('本季度不外放')
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
