import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useKb, __resetKb } from '@/composables/useKb'
import { useContribute } from '@/composables/useContribute'
import { useSession } from '@/stores/session'

/**
 * B7 补评审（2026-08-06）：分页状态机的三条真缺陷，每条都有代跑实证。
 *
 * ① 追加在途 × 处置交错 ⇒ **静默漏行**（codex MAJOR，实测 T20 消失）。
 *    seq 只在 offset=0 替换时自增，挡不住处置：处置让服务端开集前缀 -1，而客户端发的
 *    offset 是点击瞬间的本地条数 ⇒ 那一页按**旧基准**取，边界后的第一条被跳过。
 * ② `degraded=true` 时前端不得把 items/has_more 当业务数据（codex BLOCKER）。
 *    追加 ⇒ 不追加、不覆盖 has_more；替换 ⇒ 必须清空（否则 toggle 后旧视图的数据
 *    会挂在新标题下——toggle 先同步翻转视图状态，再发首页替换）。
 * ③ offset 超过服务端深分页上界 ⇒ 后端会静默钳位、反复返回同一页 ⇒ 前端先拦。
 */

function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}

function task(i: number) {
  return { task_id: `T${String(i).padStart(2, '0')}`, doc_id: 'D1', title: 'x', version_no: 1,
           review_type: 'spot_check_mismatch', review_reason: 'r', owner_dept: 'hr',
           suggested_permission_level: 'restricted', created_at: '2026-08-03', age_days: 1,
           status: 'PENDING', closed: false, reviewer_name: '' }
}

/** 直接写内部 ref（组件层同款做法，见 review-task-pagination.spec.ts）。 */
function seed(kb: ReturnType<typeof useKb>, items: unknown[], hasMore: boolean) {
  ;(kb as unknown as { reviewTasks: { value: unknown[] } }).reviewTasks.value = items
  ;(kb as unknown as { reviewTasksHasMore: { value: boolean } }).reviewTasksHasMore.value = hasMore
}
function ids(kb: ReturnType<typeof useKb>) {
  return ((kb as unknown as { reviewTasks: { value: Array<{ task_id: string }> | null } })
    .reviewTasks.value || []).map((t) => t.task_id)
}

beforeEach(() => {
  vi.restoreAllMocks(); vi.unstubAllGlobals()
  setActivePinia(createPinia()); __resetKb()
  useSession().setIdentity({ userId: 'k', name: '管', role: 'kb_admin', aclGroups: [],
                             canManage: true, managedOwnerDepts: [] })
  useSession().setToken('TKN')
})

describe('① 追加在途 × 处置交错', () => {
  /** 服务端模型：维护真实开集，resolve 即从开集移除，GET 被 gate 挂起到 resolve 之后才执行。 */
  function server(n: number) {
    const rows = Array.from({ length: n }, (_, i) => task(i))
    const gets: string[] = []
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
      const p = String(path)
      if (p.includes('/resolve')) {
        const body = JSON.parse(String(init?.body ?? '{}'))
        const i = rows.findIndex((t) => t.task_id === body.task_id)
        if (i >= 0) rows.splice(i, 1)
        return jsonResp({ ok: true })
      }
      gets.push(p)
      await gate
      const off = Number(new URL(p, 'http://x').searchParams.get('offset') ?? 0)
      return jsonResp({ items: rows.slice(off, off + 20), has_more: rows.length > off + 20 })
    }))
    return { gets, release, rows }
  }

  it('处置在途 ⇒ 旧基准那一页被丢弃并按新条数重取一次 ⇒ 零漏行', async () => {
    const kb = useKb()
    seed(kb, Array.from({ length: 20 }, (_, i) => task(i)), true)   // 已加载 T00..T19，服务端共 41 条
    const { gets, release } = server(41)

    const append = kb.loadReviewTasks(20)          // 点「加载更多」，offset=20
    await kb.resolveReviewTask('T05', 'resolve')   // 在途期间处置本页一条
    release()
    await append

    // 修复前实测：GET 只有一次（offset=20），结果里 T20 永远消失。
    expect(gets).toEqual(['/api/kb/review-tasks?offset=20', '/api/kb/review-tasks?offset=19'])
    expect(ids(kb)).not.toContain('T05')
    expect(ids(kb)).toContain('T20')               // ★ 反证锚：修复前这里是 false
    // 全序无重复、无缺口（T05 已处置）
    const want = Array.from({ length: 40 }, (_, i) => task(i).task_id).filter((x) => x !== 'T05')
    expect(ids(kb)).toEqual(want)
  })

  it('重试那一页回来时基准又变了 ⇒ 只重试一次，丢弃并保留 has_more（按钮仍在）', async () => {
    const kb = useKb()
    seed(kb, Array.from({ length: 20 }, (_, i) => task(i)), true)
    const gets: string[] = []
    // 两道独立的闸：第 2 次 GET 挂起的窗口里再处置一次，让**重试那一页**也按旧基准取。
    const rel: Array<() => void> = []
    const gates = [0, 1].map((i) => new Promise<void>((r) => { rel[i] = r }))
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      const p = String(path)
      if (p.includes('/resolve')) return jsonResp({ ok: true })
      const n = gets.length; gets.push(p)
      await gates[Math.min(n, 1)]
      return jsonResp({ items: [task(90 + n)], has_more: true })
    }))

    const append = kb.loadReviewTasks(20)
    await kb.resolveReviewTask('T05', 'resolve')   // 基准变化 ①
    rel[0]()                                        // 第 1 页回来 ⇒ 失配 ⇒ 发出重试（第 2 次 GET）
    await new Promise((r) => setTimeout(r, 0))
    expect(gets.length).toBe(2)
    await kb.resolveReviewTask('T06', 'resolve')   // 基准变化 ②：落在重试的在途窗口里
    rel[1]()
    await append

    expect(gets.length).toBe(2)                    // 恰好一次重试，不无限重试
    expect(ids(kb).some((x) => x.startsWith('T9'))).toBe(false)   // 两次都丢弃 ⇒ 不追加
    expect(kb.reviewTasksHasMore.value).toBe(true) // has_more 保留 ⇒ 按钮还在，用户可再点
  })

  it('seq 优先于 base：追加在途时切视图 ⇒ 直接丢弃，**不得**在新视图里重试', async () => {
    const kb = useKb()
    seed(kb, Array.from({ length: 20 }, (_, i) => task(i)), true)
    const gets: string[] = []
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      const p = String(path)
      if (p.includes('/resolve')) return jsonResp({ ok: true })
      gets.push(p)
      if (p.includes('offset=20')) { await gate; return jsonResp({ items: [task(90)], has_more: true }) }
      return jsonResp({ items: [task(70)], has_more: false })    // 新视图的首页
    }))

    const append = kb.loadReviewTasks(20)
    await kb.resolveReviewTask('T05', 'resolve')   // base 变了
    kb.toggleShowClosedReviewTasks()               // seq 也变了（替换 + 换视图）
    await new Promise((r) => setTimeout(r, 0))
    release()
    await append

    // 只该有：旧追加(offset=20) + toggle 的首页替换，**恰好 2 次**。
    // ⚠️ 断言写成「没有 offset=19|20&include_closed」是不够的（第一版就这么写，变异存活）：
    // 若把 base 判在 seq 之前，重试会用**新视图**的条数（1）发 `offset=1&include_closed=true`
    // ——那个 URL 不匹配上面的正则，照样全绿。这里改为钉总次数 + 钉最终列表。
    expect(gets.length).toBe(2)
    expect(gets.some((g) => g.includes('include_closed') && !g.includes('offset=0'))).toBe(false)
    expect(ids(kb)).toEqual(['T70'])               // 新视图的结果没被旧页污染（不得追加两遍）
  })

  it('处置的那条不在当前列表里 ⇒ 基准不变，在途追加照常落地（不白跑一次重取）', async () => {
    const kb = useKb()
    seed(kb, Array.from({ length: 20 }, (_, i) => task(i)), true)
    const gets: string[] = []
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      const p = String(path)
      if (p.includes('/resolve')) return jsonResp({ ok: true })
      gets.push(p); await gate
      return jsonResp({ items: [task(90)], has_more: false })
    }))

    const append = kb.loadReviewTasks(20)
    await kb.resolveReviewTask('T99', 'resolve')   // 不在列表里（换过视图后回来的迟到回执）
    release()
    await append

    expect(gets).toEqual(['/api/kb/review-tasks?offset=20'])   // ★ 无条件自增会多出一次重取
    expect(ids(kb)).toContain('T90')
  })
})

describe('② degraded：后端 fail-open 自陈本次不是业务数据', () => {
  it('追加降级 ⇒ 不追加、**不覆盖** has_more、保留已加载页、置错误横幅', async () => {
    const kb = useKb()
    seed(kb, Array.from({ length: 20 }, (_, i) => task(i)), true)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ items: [], has_more: false, degraded: true })))

    await kb.loadReviewTasks(20)

    expect(ids(kb).length).toBe(20)                       // 已加载页保留
    expect(kb.reviewTasksHasMore.value).toBe(true)        // ★ 反证锚：修复前会被打成 false（按钮消失）
    expect(kb.reviewTasksDegraded.value).toBe(true)
    expect(kb.loadErrors.value.reviewTasks).toBeTruthy()  // ★ 反证锚：修复前是 200 ⇒ 零横幅
  })

  it('替换降级 ⇒ 必须清空列表与 has_more（否则旧视图数据会挂在新标题下）', async () => {
    const kb = useKb()
    seed(kb, Array.from({ length: 20 }, (_, i) => task(i)), true)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ items: [], has_more: false, degraded: true })))

    await kb.loadReviewTasks(0)

    expect(ids(kb)).toEqual([])
    expect(kb.reviewTasksHasMore.value).toBe(false)
    expect(kb.reviewTasksDegraded.value).toBe(true)
  })

  it('追加降级后重试成功 ⇒ degraded 复位（否则空态会一直说「服务端查询失败」）', async () => {
    const kb = useKb()
    seed(kb, Array.from({ length: 20 }, (_, i) => task(i)), true)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ items: [], has_more: false, degraded: true })))
    await kb.loadReviewTasks(20)
    expect(kb.reviewTasksDegraded.value).toBe(true)

    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ items: [task(90)], has_more: false })))
    await kb.loadReviewTasks(20)
    expect(kb.reviewTasksDegraded.value).toBe(false)   // ★ 只在 offset=0 复位的话这里留 true
    expect(ids(kb)).toContain('T90')
  })

  it('换视图时首页失败 ⇒ 清空（同视图刷新失败仍保留旧列表，不回归）', async () => {
    const kb = useKb()
    // 先成功加载默认视图，让 reviewTasksView 落到 false
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ items: [task(1)], has_more: false })))
    await kb.loadReviewTasks(0)
    expect(ids(kb)).toEqual(['T01'])

    // 同视图刷新失败 ⇒ 保留
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ detail: 'boom' }, { ok: false, status: 500 })))
    await kb.loadReviewTasks(0)
    expect(ids(kb)).toEqual(['T01'])

    // 换视图失败 ⇒ 清空（旧视图的数据不得挂在「含已处理」标题下）
    kb.toggleShowClosedReviewTasks()
    await new Promise((r) => setTimeout(r, 0))
    expect(ids(kb)).toEqual([])
  })
})

describe('②-bis LoadError 重试', () => {
  it('追加失败 → 先处置一条 → 点重试：必须用**当下**的本地条数，不得复用旧 offset', async () => {
    const kb = useKb()
    seed(kb, Array.from({ length: 20 }, (_, i) => task(i)), true)   // 服务端开集 T00..T40
    const rows = Array.from({ length: 41 }, (_, i) => task(i))
    const gets: string[] = []
    let failNext = true
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
      const p = String(path)
      if (p.includes('/resolve')) {
        const body = JSON.parse(String(init?.body ?? '{}'))
        const i = rows.findIndex((t) => t.task_id === body.task_id)
        if (i >= 0) rows.splice(i, 1)
        return jsonResp({ ok: true })
      }
      gets.push(p)
      if (failNext) { failNext = false; return jsonResp({ detail: 'boom' }, { ok: false, status: 500 }) }
      const off = Number(new URL(p, 'http://x').searchParams.get('offset') ?? 0)
      return jsonResp({ items: rows.slice(off, off + 20), has_more: rows.length > off + 20 })
    }))

    await kb.loadReviewTasks(20)                   // 追加失败 ⇒ retryOffset=20
    expect(kb.loadErrors.value.reviewTasks).toBeTruthy()
    await kb.resolveReviewTask('T05', 'resolve')   // 失败之后才处置 ⇒ base 检查兜不住
    kb.retryReviewTasks()
    await new Promise((r) => setTimeout(r, 0))

    expect(gets[1]).toBe('/api/kb/review-tasks?offset=19')   // ★ 复用旧 offset=20 会漏 T20
    expect(ids(kb)).toContain('T20')
    expect(new Set(ids(kb)).size).toBe(ids(kb).length)       // 无重复
  })

  it('首屏失败后重试仍走替换（retryOffset=0 时语义不变）', async () => {
    const kb = useKb()
    const gets: string[] = []
    let failNext = true
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      gets.push(String(path))
      if (failNext) { failNext = false; return jsonResp({ detail: 'boom' }, { ok: false, status: 500 }) }
      return jsonResp({ items: [task(1)], has_more: false })
    }))
    await kb.loadReviewTasks(0)
    kb.retryReviewTasks()
    await new Promise((r) => setTimeout(r, 0))
    expect(gets).toEqual(['/api/kb/review-tasks?offset=0', '/api/kb/review-tasks?offset=0'])
    expect(ids(kb)).toEqual(['T01'])
  })
})

describe('③ 深分页上界前端镜像', () => {
  it('offset 超上界 ⇒ 零请求（后端会静默钳位、反复返回同一页）', async () => {
    const kb = useKb()
    const fetchFn = vi.fn(async () => jsonResp({ items: [], has_more: true }))
    vi.stubGlobal('fetch', fetchFn)
    await kb.loadReviewTasks(10020)
    expect(fetchFn).not.toHaveBeenCalled()
    await kb.loadReviewTasks(10000)                       // 上界之内照常发
    expect(fetchFn).toHaveBeenCalledTimes(1)
  })
})

describe('④ 我的贡献：空态与 has_more 必须同生同灭', () => {
  it('替换失败 ⇒ 清空列表的同时清掉 mineHasMore', async () => {
    const c = useContribute()
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ items: [{ contribution_id: 'c1' }], has_more: true })))
    await c.loadMine(0)
    expect(c.mineHasMore.value).toBe(true)

    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ detail: 'boom' }, { ok: false, status: 500 })))
    await c.loadMine(0)
    expect(c.myContribs.value).toEqual([])
    expect(c.mineHasMore.value).toBe(false)               // ★ 反证锚：修复前留 true ⇒ 空态 + 加载更多并存
  })
})
