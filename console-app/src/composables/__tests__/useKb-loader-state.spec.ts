import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { useKb, __resetKb } from '@/composables/useKb'
import { useSession } from '@/stores/session'
import { setActivePinia, createPinia } from 'pinia'
import * as api from '@/lib/api'

/**
 * GET loader 的在途/已定态。锁的是一个**实测过的具体故障**：
 * `/api/kb/stats` 返回 404 时 noteLoadError 走静默分支，kbStats 与 loadErrors 同时恒空，
 * 于是任何 `!data && !error` 形状的加载判据永远为真——实测 4 张 hero 卡骨架转 5 秒仍在转。
 */
function seedIdentity(role: 'kb_admin' | 'dept_admin' = 'kb_admin') {
  const s = useSession()
  s.setToken('t')
  s.identity = {
    userId: 'u', displayName: 'U', role, canManage: true,
    aclGroups: ['production'], managedOwnerDepts: ['production'],
  } as any
}

describe('useKb GET loader 三态', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    __resetKb()
    seedIdentity()
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('在途期间 isLoading=true 且 hasSettled=false；跑完后反转', async () => {
    const kb = useKb()
    let release: (v: any) => void = () => {}
    vi.spyOn(api, 'apiJson').mockImplementation(() => new Promise((res) => { release = res }))

    expect(kb.isLoading('stats')).toBe(false)
    expect(kb.hasSettled('stats')).toBe(false)

    const p = kb.loadStats()
    expect(kb.isLoading('stats'), '发出请求后应立即在途').toBe(true)
    expect(kb.hasSettled('stats')).toBe(false)

    release({ total: 1, active: 1, retired: 0, chunks: 1, by_badge: {} })
    await p
    expect(kb.isLoading('stats')).toBe(false)
    expect(kb.hasSettled('stats'), '跑完即定态').toBe(true)
  })

  it('【核心】404 静默也会定态——骨架不会永久转圈', async () => {
    const kb = useKb()
    vi.spyOn(api, 'apiJson').mockRejectedValue(new api.ApiError('not found', 404))

    await kb.loadStats()

    // 这三条正是故障现场：数据空、错误也空（404 被静默）
    expect(kb.kbStats.value).toBeNull()
    expect(kb.loadErrors.value['stats']).toBeUndefined()
    // 而定态标记独立成立 —— 组件据此可以停掉骨架、改出「端点未上线」的诚实占位，
    // 而不是靠 `!data && !error` 反推（那个组合在此刻恒为真）。
    expect(kb.isLoading('stats')).toBe(false)
    expect(kb.hasSettled('stats'), '404 也必须定态，否则骨架永久转圈').toBe(true)
  })

  it('5xx 同样定态，且错误条已置（两条信息互不覆盖）', async () => {
    const kb = useKb()
    vi.spyOn(api, 'apiJson').mockRejectedValue(new api.ApiError('boom', 500))
    await kb.loadStats()
    expect(kb.loadErrors.value['stats']).toBeTruthy()
    expect(kb.hasSettled('stats')).toBe(true)
    expect(kb.isLoading('stats')).toBe(false)
  })

  it('【合流】并发两次只发一个请求，且两个调用方都等到真结果', async () => {
    // ensureTabLoaded 从 dash 与 docs 两条路径各调一次 loadStats——审计实测确实发出 2 个并发请求。
    // 合流后只发 1 个；关键是第二个调用方**不能**立即拿到 undefined（那会让 Promise.allSettled
    // 提前 resolve、tab 级指示提前收掉），必须等到同一份真结果。
    const kb = useKb()
    let calls = 0
    let release: (v: any) => void = () => {}
    vi.spyOn(api, 'apiJson').mockImplementation(() => { calls++; return new Promise((res) => { release = res }) })

    const p1 = kb.loadStats()
    const p2 = kb.loadStats()
    expect(calls, '并发第二次不得重复发请求').toBe(1)

    let p2Done = false
    void p2.then(() => { p2Done = true })
    await Promise.resolve()
    expect(p2Done, '第二个调用方不得提前 resolve').toBe(false)

    release({ total: 7, active: 7, retired: 0, chunks: 1, by_badge: {} })
    await Promise.all([p1, p2])
    expect(p2Done).toBe(true)
    expect(kb.kbStats.value?.total, '两个调用方拿到同一份真结果').toBe(7)
  })

  it('__resetKb 清空在途与定态（换身份不带旧痕迹）', async () => {
    const kb = useKb()
    vi.spyOn(api, 'apiJson').mockResolvedValue({ total: 1, active: 1, retired: 0, chunks: 1, by_badge: {} })
    await kb.loadStats()
    expect(kb.hasSettled('stats')).toBe(true)
    __resetKb()
    expect(kb.hasSettled('stats')).toBe(false)
    expect(kb.isLoading('stats')).toBe(false)
  })
})
