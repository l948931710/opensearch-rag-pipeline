import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useKb, __resetKb } from '@/composables/useKb'
import { useContribute } from '@/composables/useContribute'
import { useSession } from '@/stores/session'

// 「加载更多」追加在途闸（2026-08-04 独立核验）：双击 = 同 offset 双请求 = 同批条目
// 重复追加（duplicate key）。契约：offset>0 的追加在途时二次调用 no-op；offset=0 的
// 替换路径**不**受闸（toggle 重载不受影响）。

function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  setActivePinia(createPinia())
  __resetKb()
  useSession().setIdentity({ userId: 'u', name: '张三', role: 'kb_admin', aclGroups: ['hr'],
                             canManage: true, managedOwnerDepts: ['hr'] })
  useSession().setToken('TKN')
})

function gatedFetch(payload: unknown) {
  let release!: () => void
  const gate = new Promise<void>((r) => { release = r })
  const calls: string[] = []
  const fetchFn = vi.fn(async (path: string) => {
    calls.push(String(path))
    await gate
    return jsonResp(payload)
  })
  vi.stubGlobal('fetch', fetchFn)
  return { calls, release }
}

describe('loadReviewTasks 追加在途闸', () => {
  it('追加在途时二次追加 no-op（恰好 1 次请求）；完成后可再追加', async () => {
    const { calls, release } = gatedFetch({ items: [], has_more: true })
    const kb = useKb()
    const p1 = kb.loadReviewTasks(20)
    const p2 = kb.loadReviewTasks(20)     // 双击：必须 no-op
    release()
    await Promise.all([p1, p2])
    expect(calls.length).toBe(1)
    await kb.loadReviewTasks(40)          // 闸已释放：后续追加正常
    expect(calls.length).toBe(2)
  })

  it('offset=0 替换路径不受闸（toggle 重载语义不变）', async () => {
    const { calls, release } = gatedFetch({ items: [], has_more: false })
    const kb = useKb()
    const p1 = kb.loadReviewTasks(20)
    const p2 = kb.loadReviewTasks(0)      // 替换：放行
    release()
    await Promise.all([p1, p2])
    expect(calls.length).toBe(2)
  })
})

describe('loadMine 追加在途闸', () => {
  it('追加在途时二次追加 no-op；完成后可再追加', async () => {
    const { calls, release } = gatedFetch({ items: [], has_more: true })
    const c = useContribute()
    const p1 = c.loadMine(50)
    const p2 = c.loadMine(50)
    release()
    await Promise.all([p1, p2])
    expect(calls.length).toBe(1)
    await c.loadMine(100)
    expect(calls.length).toBe(2)
  })
})
