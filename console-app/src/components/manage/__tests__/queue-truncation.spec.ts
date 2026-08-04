import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import { apiJson } from '@/lib/api'
import ApprovalQueue from '@/components/manage/ApprovalQueue.vue'
import AccessRequestQueue from '@/components/manage/AccessRequestQueue.vue'
import AccessGrantList from '@/components/manage/AccessGrantList.vue'
import { useKb, __resetKb } from '@/composables/useKb'

/**
 * P3-3（2026-08-04）：硬 LIMIT 队列的截断必须**看得见**。
 *
 * 后端六个队列端点改成 `LIMIT N+1` 取探针行并回 `truncated`（见
 * `tests/test_queue_truncation_visible.py`）；那一半只保证"后端知道"。
 * 这里保证**前端会说** —— 否则 `truncated` 只是个没人读的字段，
 * 使用者看到的仍然是「就这些」。
 *
 * ⚠️ 提示语**刻意不带「加载更多」**：真分页需要稳定排序键（`DATETIME` 只到秒，
 * OFFSET 翻页会漏行/重行——真库实测见 `d2c8e12`），属设计变更，另议。
 */
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, apiJson: vi.fn() }
})

beforeEach(() => { __resetKb(); (apiJson as any).mockReset() })

function activate(role: Identity['role'] = 'kb_admin') {
  const id: Identity = { userId: 'u1', name: '管', role, aclGroups: ['marketing'],
    canManage: true, managedOwnerDepts: ['marketing'] }
  setActivePinia(createTestingPinia({ createSpy: vi.fn,
    initialState: { session: { identity: id, token: 't', ready: true } } }))
}

const NOTICE = '[data-testid="queue-truncated"]'

const CASES = [
  { name: '待审批队列', comp: ApprovalQueue, load: 'loadApprovals', cap: 100, label: '待审批',
    item: { doc_id: 'D1', version_no: 1, title: 't', original_filename: 'f.docx',
            owner_dept: 'marketing', permission_level: 'public', owner_name: 'n', created_at: '2026-08-01' } },
  // ⚠️ 角色不是可选项：`/api/kb/access-requests` 是 **dept_admin** 的待办队列，
  //    `loadAccessRequests` 对 kb_admin 直接置空返回（useKb.ts «kb_admin → accessRequests=[]»）
  //    ⇒ 用 kb_admin 跑这条，组件因列表为空整块不渲染，三个用例会**一起空转通过**。
  { name: '授权申请', comp: AccessRequestQueue, load: 'loadAccessRequests', cap: 100, label: '授权申请',
    role: 'dept_admin' as const,
    item: { id: 'r1', doc_id: 'D1', doc_title: 't', owner_dept: 'marketing', requester_dept: 'production',
            requester_name: '王', permission_level: 'dept_internal', reason: 'x', created_at: '2026-08-01' } },
  { name: '已授权清单', comp: AccessGrantList, load: 'loadAccessGrants', cap: 200, label: '已授权',
    item: { id: 'g1', doc_id: 'D1', doc_title: 't', owner_dept: 'marketing', requester_dept: 'production',
            requester_name: '王', permission_level: 'dept_internal', reason: 'x', decided_at: '2026-06-26' } },
] as const

describe('硬 LIMIT 队列：截断如实告知', () => {
  for (const c of CASES) {
    it(`${c.name}：truncated=true → 显示「仅显示前 ${c.cap} 条」`, async () => {
      activate((c as any).role)
      const kb = useKb() as any
      ;(apiJson as any).mockResolvedValue({ items: [c.item], truncated: true })
      await kb[c.load]()
      const w = mount(c.comp)
      const el = w.find(NOTICE)
      expect(el.exists()).toBe(true)
      expect(el.text()).toContain(`仅显示前 ${c.cap} 条`)
      expect(el.text()).toContain(c.label)
      // 截断提示 ≠ 分页入口：这里给的是"知情"，不是"翻页"（后端排序键不稳）
      expect(w.text()).not.toContain('加载更多')
    })

    it(`${c.name}：truncated=false → **不显示**（防"恒显"式假通过）`, async () => {
      activate((c as any).role)
      const kb = useKb() as any
      ;(apiJson as any).mockResolvedValue({ items: [c.item], truncated: false })
      await kb[c.load]()
      expect(mount(c.comp).find(NOTICE).exists()).toBe(false)
    })

    it(`${c.name}：老后端不回该字段 → 当作未截断（不吓唬人）`, async () => {
      activate((c as any).role)
      const kb = useKb() as any
      ;(apiJson as any).mockResolvedValue({ items: [c.item] })
      await kb[c.load]()
      expect(mount(c.comp).find(NOTICE).exists()).toBe(false)
    })
  }
})
