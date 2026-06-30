import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import ContributionReviewQueue from '@/components/contribute/ContributionReviewQueue.vue'
import { useContribute, __resetContribute, type ContributionItem } from '@/composables/useContribute'

// 回归守卫：采纳/驳回的「处理中」态。修复用户实测发现的卡顿观感——采纳要同步写 OSS .md + register
// （~1–2s），此前按钮仅变灰、无进度提示。现 isBusy 期间显 spinner + 「采纳中…/驳回中…」。
const PENDING: ContributionItem = {
  contribution_id: 'c1', question: '差旅报销保存几年？', content: '≥5年', category_dept: 'finance',
  author_id: 'u9', author_name: '王伟', review_status: 'pending', ingestion_status: 'none',
  state: 'pending', doc_id: null, review_note: '', created_at: '2026-06-29', reviewed_at: null,
}

beforeEach(() => { vi.restoreAllMocks(); __resetContribute() })

function activate() {
  const pinia = createTestingPinia({
    createSpy: vi.fn,
    initialState: { session: { identity: { userId: 'a', name: '管理员', role: 'dept_admin', aclGroups: ['finance'], canManage: true, managedOwnerDepts: ['finance'] }, token: 't', ready: true } },
  })
  setActivePinia(pinia)
  return pinia
}

describe('ContributionReviewQueue — 采纳/驳回 处理中态', () => {
  it('静态（未在途）→ 按钮显「采纳/驳回」，无 spinner', () => {
    const pinia = activate()
    ;(useContribute() as any).pendingContribs.value = [PENDING]
    const w = mount(ContributionReviewQueue, { global: { plugins: [pinia] } })
    expect(w.text()).toContain('采纳')
    expect(w.text()).toContain('驳回')
    expect(w.find('.animate-spin').exists()).toBe(false)
  })

  it('采纳进行中（在途）→ 同行两键显 spinner + 「采纳中…/驳回中…」并禁用', async () => {
    const pinia = activate()
    const c = useContribute()
    ;(c as any).pendingContribs.value = [PENDING]
    // fetch 永不 resolve → 模拟 accept 的 OSS 写 + register 耗时窗口（inflight 同步置位、保持）
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    const w = mount(ContributionReviewQueue, { global: { plugins: [pinia] } })

    void c.acceptContribution(PENDING)   // 触发但不等其完成
    await flushPromises()

    expect(w.find('.animate-spin').exists()).toBe(true)     // 出现进度指示（消除"卡死"观感）
    expect(w.text()).toContain('采纳中…')
    expect(w.text()).toContain('驳回中…')                    // 同 ct:key → 两键同时进入处理态
    expect(w.findAll('button').every((b) => b.attributes('disabled') !== undefined)).toBe(true)
  })
})
