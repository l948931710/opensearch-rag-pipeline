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
beforeEach(() => { vi.restoreAllMocks(); __resetKb() })

function activate() {
  const id: Identity = { userId: 'k', name: '管', role: 'kb_admin', aclGroups: [], canManage: true, managedOwnerDepts: [] }
  setActivePinia(createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 't', ready: true } } }))
}

function mountQueue(hasMore: boolean, showClosed: boolean) {
  activate()
  const kb = useKb()
  ;(kb as unknown as { reviewTasks: { value: unknown[] } }).reviewTasks.value = [
    { task_id: 'T1', doc_id: 'D1', title: 'x', version_no: 1, review_type: 'spot_check_mismatch',
      review_reason: 'r', owner_dept: 'hr', suggested_permission_level: 'restricted',
      created_at: '2026-08-03', age_days: 1, status: 'PENDING', closed: false, reviewer_name: '' },
  ]
  ;(kb as unknown as { reviewTasksHasMore: { value: boolean } }).reviewTasksHasMore.value = hasMore
  if (showClosed) kb.toggleShowClosedReviewTasks()
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
