import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import { apiJson } from '@/lib/api'
import { useKb, __resetKb } from '@/composables/useKb'
import { useContribute, __resetContribute } from '@/composables/useContribute'

/**
 * 追加代际守卫（2026-08-06 codex 补评审确认的 append-vs-replace 竞态）。
 *
 * 既有的「追加在途闸」只挡**追加对追加**（双击「加载更多」）。它挡不住的是:
 * 追加在途时列表被**替换** —— 切视图、处置后重载、采纳/驳回都走 offset=0 替换。
 * 旧的第 2 页回来后仍会追加到**新列表**上,两种视图/两个时刻的数据混在一起。
 *
 * ⚠️ 本仓 850 行外就有正确范式(useKb 的 `loadMoreDocs` + `docsSeq`),这两处只是没照抄。
 * 因此测试必须**控制两个响应的反向完成顺序**并检查**最终列表内容** ——
 * 只数请求次数的测试对这个缺陷完全无感(既有 load-more-inflight.spec 就是那样)。
 */
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, apiJson: vi.fn() }
})

beforeEach(() => { __resetKb(); __resetContribute?.(); (apiJson as any).mockReset() })

function activate(role = 'kb_admin') {
  const p = createTestingPinia({
    createSpy: vi.fn,
    initialState: {
      session: {
        identity: { userId: 'u1', name: '张三', role, aclGroups: ['hr'], canManage: true, managedOwnerDepts: ['hr'] },
        token: 't', ready: true,
      },
    },
  })
  setActivePinia(p)
  return p
}
/** 可外部决议的 deferred，用来精确编排两个请求的完成顺序。 */
function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

describe('loadReviewTasks —— 追加在途时换视图，旧页必须被丢弃', () => {
  it('第 2 页在途 → 切「显示已处理」替换 → 旧第 2 页后到，不得混入', async () => {
    activate()
    const kb = useKb() as any

    // ① 首页（未处理视图）
    ;(apiJson as any).mockResolvedValueOnce({ items: [{ task_id: 'A1' }], has_more: true })
    await kb.loadReviewTasks(0)
    expect(kb.reviewTasks.value.map((t: any) => t.task_id)).toEqual(['A1'])

    // ② 第 2 页发出但**挂起**
    const page2 = deferred<any>()
    ;(apiJson as any).mockReturnValueOnce(page2.promise)
    const appending = kb.loadReviewTasks(1)

    // ③ 期间用户切视图 → offset=0 替换，先返回
    ;(apiJson as any).mockResolvedValueOnce({ items: [{ task_id: 'B1' }], has_more: false })
    await kb.loadReviewTasks(0)
    expect(kb.reviewTasks.value.map((t: any) => t.task_id)).toEqual(['B1'])

    // ④ 旧第 2 页此刻才回来 —— 它属于**上一个视图**
    page2.resolve({ items: [{ task_id: 'A2' }], has_more: true })
    await appending

    expect(kb.reviewTasks.value.map((t: any) => t.task_id)).toEqual(['B1'])
    expect(kb.reviewTasksHasMore.value).toBe(false)   // 旧页的 has_more 也不得覆盖
  })

  it('无替换发生时，正常追加不受影响（守卫不能误伤主路径）', async () => {
    activate()
    const kb = useKb() as any
    ;(apiJson as any).mockResolvedValueOnce({ items: [{ task_id: 'A1' }], has_more: true })
    await kb.loadReviewTasks(0)
    ;(apiJson as any).mockResolvedValueOnce({ items: [{ task_id: 'A2' }], has_more: false })
    await kb.loadReviewTasks(1)
    expect(kb.reviewTasks.value.map((t: any) => t.task_id)).toEqual(['A1', 'A2'])
  })
})

describe('loadMine —— 采纳/驳回触发替换时，在途旧页必须被丢弃', () => {
  it('第 2 页在途 → 列表被替换 → 旧页后到，不得追加', async () => {
    activate('dept_admin')
    const c = useContribute() as any

    ;(apiJson as any).mockResolvedValueOnce({ items: [{ id: 1 }], has_more: true })
    await c.loadMine(0)

    const page2 = deferred<any>()
    ;(apiJson as any).mockReturnValueOnce(page2.promise)
    const appending = c.loadMine(50)

    ;(apiJson as any).mockResolvedValueOnce({ items: [{ id: 9 }], has_more: false })
    await c.loadMine(0)

    page2.resolve({ items: [{ id: 2 }], has_more: true })
    await appending

    expect(c.myContribs.value.map((x: any) => x.id)).toEqual([9])
  })
})
