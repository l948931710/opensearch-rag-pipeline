import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import ApprovalQueue from '@/components/manage/ApprovalQueue.vue'
import AccessRequestQueue from '@/components/manage/AccessRequestQueue.vue'
import { useKb, __resetKb, type PendingItem, type AccessRequestItem } from '@/composables/useKb'

// 批次α-③：上传审批「通过」与跨部门「授权」此前是审批面唯二无确认的放行动作（对照：退役/
// 批量/Agent 批准全有 confirm）。本组固化新契约：
//   确认前绝不发请求（取消 = 零副作用）；确认后才执行放行。
// confirm 落在组件层——useKb.approve/approveAccess 保持「调用即执行」，useKb.spec 的直调用例不受影响。

const confirmMock = vi.hoisted(() => vi.fn())
vi.mock('@/composables/useDialog', () => ({
  useDialog: () => ({ confirm: confirmMock, promptText: vi.fn(), notice: vi.fn() }),
}))

beforeEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); confirmMock.mockReset(); __resetKb() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'admin1', name: '管理员', role: 'kb_admin', aclGroups: ['production'], canManage: true, managedOwnerDepts: ['production'], ...over }
}
function activePinia(id: Identity) {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 'TKN', ready: true } } })
  setActivePinia(pinia)
  return pinia
}

function pending(over: Partial<PendingItem> = {}): PendingItem {
  return {
    doc_id: 'P1', version_no: 2, title: '2026 客户验厂应答模板', original_filename: '验厂应答.docx',
    owner_dept: 'quality', permission_level: 'public', owner_name: '李娜', created_at: '2026-06-27',
    ...over,
  } as PendingItem
}
function accessReq(over: Partial<AccessRequestItem> = {}): AccessRequestItem {
  return {
    id: 'ar1', doc_id: 'D1', doc_title: '营销物料使用规范 v3', owner_dept: 'production',
    requester_dept: 'marketing', requester_name: '王伟', permission_level: 'dept_internal',
    reason: '包装设计需引用营销规范。', created_at: '2026-07-09',
    ...over,
  } as AccessRequestItem
}

describe('ApprovalQueue「通过」— 组件层二次确认', () => {
  function mountQueue() {
    const pinia = activePinia(identity())
    const kb = useKb()
    kb.approvals.value = [pending()]
    return mount(ApprovalQueue, { global: { plugins: [pinia] } })
  }

  it('取消确认 → 不发任何请求（零副作用），队列项保留', async () => {
    confirmMock.mockResolvedValue(false)
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    const w = mountQueue()
    await w.findAll('button').find((b) => b.text().includes('通过'))!.trigger('click')
    await vi.waitFor(() => expect(confirmMock).toHaveBeenCalled())
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(useKb().approvals.value).toHaveLength(1)
  })

  it('确认 → POST /api/kb/approve；确认文案含文档名与放行语义', async () => {
    confirmMock.mockResolvedValue(true)
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      calls.push(String(path))
      return { ok: true, status: 200, json: async () => ({}) }
    }))
    const w = mountQueue()
    await w.findAll('button').find((b) => b.text().includes('通过'))!.trigger('click')
    await vi.waitFor(() => expect(calls.some((p) => p.includes('/api/kb/approve'))).toBe(true))
    const opts = confirmMock.mock.calls[0][0]
    expect(String(opts.message)).toContain('2026 客户验厂应答模板')
    expect(String(opts.message)).toContain('放行')
  })
})

describe('AccessRequestQueue「授权」— 组件层二次确认', () => {
  function mountQueue() {
    const pinia = activePinia(identity({ role: 'dept_admin' }))
    const kb = useKb()
    kb.accessRequests.value = [accessReq()]
    return mount(AccessRequestQueue, { global: { plugins: [pinia] } })
  }

  it('取消确认 → 不发任何请求，申请保留', async () => {
    confirmMock.mockResolvedValue(false)
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    const w = mountQueue()
    await w.findAll('button').find((b) => b.text().includes('授权'))!.trigger('click')
    await vi.waitFor(() => expect(confirmMock).toHaveBeenCalled())
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(useKb().accessRequests.value).toHaveLength(1)
  })

  it('确认 → POST /api/kb/access-requests/approve；文案含申请方与文档名', async () => {
    confirmMock.mockResolvedValue(true)
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      calls.push(String(path))
      return { ok: true, status: 200, json: async () => ({}) }
    }))
    const w = mountQueue()
    await w.findAll('button').find((b) => b.text().includes('授权'))!.trigger('click')
    await vi.waitFor(() => expect(calls.some((p) => p.includes('/api/kb/access-requests/approve'))).toBe(true))
    const opts = confirmMock.mock.calls[0][0]
    expect(String(opts.message)).toContain('王伟')
    expect(String(opts.message)).toContain('营销物料使用规范 v3')
  })
})
