import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { useSession } from '@/stores/session'
import { useContribute, __resetContribute } from '@/composables/useContribute'
import { contribStateLabel, contribStateTone, gapKindLabel } from '@/lib/kb'
import ContribBadge from '@/components/contribute/ContribBadge.vue'

function stubFetch(json: any) {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => json, text: async () => JSON.stringify(json),
  })))
}

// 在 Pinia 上下文里设好身份（非 dev-preview，走真实 fetch 分支）。
function withSession(over: Record<string, any> = {}) {
  mount({ template: '<i/>' }, { global: { plugins: [createTestingPinia({ createSpy: vi.fn, stubActions: false })] } })
  const s = useSession()
  s.setToken('t')
  s.setIdentity({ userId: 'u1', name: '张三', role: 'employee', aclGroups: ['marketing'], canManage: false, managedOwnerDepts: [], ...over })
  return s
}

beforeEach(() => { vi.restoreAllMocks(); __resetContribute() })

describe('lib/kb — 贡献状态/缺口词表', () => {
  it('5 态徽章 label/tone', () => {
    expect(contribStateLabel('pending')).toBe('待审核')
    expect(contribStateLabel('registering')).toBe('已采纳·待入库')
    expect(contribStateLabel('searchable')).toBe('已入库')
    expect(contribStateLabel('failed')).toBe('入库失败')
    expect(contribStateTone('searchable')).toBe('live')
    expect(contribStateTone('failed')).toBe('fail')
    expect(contribStateTone('unknown')).toBe('muted')
  })
  it('缺口来源短标', () => {
    expect(gapKindLabel('no_result')).toBe('没有相关文档')
    expect(gapKindLabel('refusal')).toBe('答案不够好')
  })
})

describe('ContribBadge', () => {
  it('渲染 state 对应文案', () => {
    const w = mount(ContribBadge, { props: { state: 'searchable' } })
    expect(w.text()).toBe('已入库')
  })
})

describe('useContribute', () => {
  it('loadGaps 填充列表与 summary', async () => {
    withSession()
    stubFetch({ items: [{ question: 'Q1', asks: 2, last_days: 1, dept: 'marketing', kind: 'refusal', question_hash: 'h', source_message_id: 'm', has_pending_contribution: false }], summary: { unanswered: 1, answered: 5, this_month: 2, contributors: 3 }, has_more: false })
    const { loadGaps, gaps, gapsSummary } = useContribute()
    await loadGaps()
    expect(gaps.value.length).toBe(1)
    expect(gapsSummary.value?.answered).toBe(5)
  })

  it('openModal 默认归属取本部门，prefill 优先', () => {
    withSession({ aclGroups: ['finance'] })
    const { openModal, formDept, formQuestion } = useContribute()
    openModal()
    expect(formDept.value).toBe('finance')          // 本部门兜底
    openModal({ question: '如何报销', dept: 'hr' })
    expect(formDept.value).toBe('hr')               // prefill 优先
    expect(formQuestion.value).toBe('如何报销')
  })

  it('submit 空问题不发请求、给错误提示', async () => {
    withSession()
    const fetchSpy = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '{}' }))
    vi.stubGlobal('fetch', fetchSpy)
    const { openModal, formContent, submitContribution, submitErr } = useContribute()
    openModal()
    formContent.value = '有答案但没问题'
    const ok = await submitContribution()
    expect(ok).toBe(false)
    expect(submitErr.value).toContain('问题')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('reviewCount = 待审核贡献数', async () => {
    withSession({ canManage: true })
    stubFetch({ items: [{ contribution_id: 'p1', question: 'q', content: 'c', category_dept: 'marketing', author_id: 'a', author_name: '', review_status: 'pending', ingestion_status: 'none', state: 'pending', doc_id: null, review_note: '', created_at: '', reviewed_at: null }], has_more: false })
    const { loadPending, reviewCount } = useContribute()
    await loadPending()
    expect(reviewCount.value).toBe(1)
  })
})
