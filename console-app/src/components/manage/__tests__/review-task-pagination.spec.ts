import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import ReviewTaskQueue from '@/components/manage/ReviewTaskQueue.vue'
import { useKb, __resetKb } from '@/composables/useKb'

/**
 * P2-11 收窄：复审任务的「加载更多」**只在「只看未处置」视图给**。
 *
 * 真库实测（MySQL 8.0.46，12 条同秒任务、每页 4 条）：
 *   · 默认视图 `ORDER BY created_at ASC, task_id ASC`：处置即本地移除 ⇒ offset(=本地条数)
 *     与服务端前缀**同步收缩** ⇒ 处置 0/1/2/3/4 条，第 2 页恒为 T05,T06,T07,T08。**零漏**。
 *   · `include_closed=true` 的 `ORDER BY (open_pred) DESC, created_at DESC, task_id DESC`：
 *     首排序键是**可变谓词**，处置会让该行跨组跳到队尾（前缀 -1），而该视图下
 *     resolveReviewTask 只标 closed、**不从本地移除**（offset 不变）
 *     ⇒ **每处置 1 条，下一页漏 1 条**：处置 T12/T11 后第 2 页从应有的 T08,T07,T06,T05
 *     变成 T06,T05,T04,T03 —— T08/T07 永远看不到。
 *
 * 结论：分页只在可证正确的分支开放；另一分支如实说明被截断，绝不静默丢行。
 */
beforeEach(() => {
  vi.restoreAllMocks(); vi.unstubAllGlobals(); __resetKb()
  // `toggleShowClosedReviewTasks` 会 fire-and-forget 一次真实 loadReviewTasks（useKb.ts:1651）。
  // 不打桩的话这里会打真 fetch，靠 catch 兜住 —— 测试意图之外的副作用，会污染
  // loadErrors/reviewTasks（2026-08-06 B7 补评审，codex MINOR）。显式打桩，不靠兜底。
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => ({ items: [], has_more: false }), text: async () => '{}',
  })))
})

function activate() {
  const id: Identity = { userId: 'k', name: '管', role: 'kb_admin', aclGroups: [], canManage: true, managedOwnerDepts: [] }
  setActivePinia(createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 't', ready: true } } }))
}

const ONE = [
  { task_id: 'T1', doc_id: 'D1', title: 'x', version_no: 1, review_type: 'spot_check_mismatch',
    review_reason: 'r', owner_dept: 'hr', suggested_permission_level: 'restricted',
    created_at: '2026-08-03', age_days: 1, status: 'PENDING', closed: false, reviewer_name: '' },
]

function mountQueue(hasMore: boolean, showClosed: boolean,
                    { items = ONE as unknown[], degraded = false } = {}) {
  activate()
  const kb = useKb()
  if (showClosed) kb.toggleShowClosedReviewTasks()   // 先切视图（它会替换列表），再灌状态
  ;(kb as unknown as { reviewTasks: { value: unknown[] } }).reviewTasks.value = items
  ;(kb as unknown as { reviewTasksHasMore: { value: boolean } }).reviewTasksHasMore.value = hasMore
  ;(kb as unknown as { reviewTasksDegraded: { value: boolean } }).reviewTasksDegraded.value = degraded
  return mount(ReviewTaskQueue)
}

const MORE = '[data-testid="review-task-load-more"]'

describe('复审任务分页：只在排序稳定的分支开放', () => {
  it('只看未处置 + 还有更多 → 给「加载更多」', () => {
    const w = mountQueue(true, false)
    expect(w.find(MORE).exists()).toBe(true)
  })

  it('显示已处置 + 还有更多 → **不给按钮**（该分支翻页会漏行），改为如实说明截断', () => {
    const w = mountQueue(true, true)
    expect(w.find(MORE).exists()).toBe(false)
    expect(w.text()).toContain('暂不支持翻页')
  })

  it('没有更多 → 两种视图都不显示（既不给按钮也不给截断提示）', () => {
    expect(mountQueue(false, false).find(MORE).exists()).toBe(false)
    const w = mountQueue(false, true)
    expect(w.find(MORE).exists()).toBe(false)
    expect(w.text()).not.toContain('暂不支持翻页')
  })
})

/**
 * B7 补评审（2026-08-06）：空态是一句**对安全网状态的断言**，此前一律说「安全网干净」。
 * 实测两条 v-if 链互不排斥 ⇒ 把第 1 页 20 条全部处置后，界面同时渲染
 * 「安全网干净」和「加载更多」——而队列里第 21 条起还有真隐患。
 */
describe('空列表的三种成因必须分开说', () => {
  it('处置光本页但队列还有 → 不得说「干净」，且**保留**加载更多', () => {
    const w = mountQueue(true, false, { items: [] })
    expect(w.text()).not.toContain('安全网干净')        // ★ 反证锚：修复前为 true
    expect(w.text()).toContain('队列里还有更多')
    expect(w.find(MORE).exists()).toBe(true)
  })

  it('closed 视图空列表 + 还有更多 → 仍不给按钮，且截断说明只出现一次', () => {
    const w = mountQueue(true, true, { items: [] })
    expect(w.find(MORE).exists()).toBe(false)          // b8e11b4 刻意关掉的翻页不得被重新打开
    expect(w.text().match(/暂不支持翻页/g) || []).toHaveLength(1)
  })

  it('服务端降级 → 明说「这不代表安全网干净」', () => {
    const w = mountQueue(false, false, { items: [], degraded: true })
    expect(w.text()).toContain('这不代表安全网干净')
    expect(w.text()).not.toContain('没有待复审的安全任务')
  })

  it('真的空且没有更多 → 才允许说「安全网干净」', () => {
    const w = mountQueue(false, false, { items: [] })
    expect(w.text()).toContain('安全网干净')
    expect(w.find(MORE).exists()).toBe(false)
  })
})
