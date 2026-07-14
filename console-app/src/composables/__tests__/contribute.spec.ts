import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { useSession } from '@/stores/session'
import { useContribute, __resetContribute } from '@/composables/useContribute'
import { contribStateLabel, contribStateTone, gapKindLabel, fmtTs } from '@/lib/kb'
import ContribBadge from '@/components/contribute/ContribBadge.vue'
import MyContributions from '@/components/contribute/MyContributions.vue'

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

  it('fmtTs 时间戳归一（批次α-⑥）：isoformat T 分隔 / MySQL 空格分隔 / 纯日期 / 相对文案', () => {
    expect(fmtTs('2026-07-11T08:57:17')).toBe('2026-07-11 08:57')   // 裸 slice(0,16) 会留 T
    expect(fmtTs('2026-07-09 20:00:00')).toBe('2026-07-09 20:00')
    expect(fmtTs('2026-06-20')).toBe('2026-06-20')
    expect(fmtTs('刚刚')).toBe('刚刚')
    expect(fmtTs(null)).toBe('')
    expect(fmtTs(undefined)).toBe('')
  })
})

describe('MyContributions — 时间戳渲染（批次α-⑥）', () => {
  it('后端 isoformat（带 T）在列表中显示为 YYYY-MM-DD HH:MM', () => {
    withSession()
    const { myContribs } = useContribute()
    myContribs.value = [{
      contribution_id: 'c9', question: '宿舍门禁卡丢了怎么补办？', content: '联系前台',
      category_dept: 'admin', author_id: 'u1', author_name: '张三', review_status: 'pending',
      ingestion_status: 'none', state: 'pending', doc_id: null, review_note: '',
      created_at: '2026-07-11T08:57:17', reviewed_at: null,
    }] as never
    const w = mount(MyContributions)
    expect(w.text()).toContain('2026-07-11 08:57')
    expect(w.text()).not.toContain('2026-07-11T')
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

// 批次α-⑤：审核动作后兄弟面板联动——此前 accept 只刷 pending+mine、reject 只刷 pending，
// 「待回答」徽标/统计卡保持旧值，同一问题会被第二人重复回答（staging 2026-07-11 遗留）。
describe('useContribute — 审核动作联动兄弟面板（批次α-⑤）', () => {
  const CONTRIB = {
    contribution_id: 'c1', question: '如何申请密钥', content: '提交工单', category_dept: 'it',
    author_id: 'u2', author_name: '李', review_status: 'pending', ingestion_status: 'none',
    state: 'pending', doc_id: null, review_note: '', created_at: '2026-07-01', reviewed_at: null,
  }
  function recordFetch(): string[] {
    const paths: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (p: string) => {
      paths.push(String(p))
      return { ok: true, status: 200, json: async () => ({ items: [], summary: null, has_more: false }), text: async () => '{}' }
    }))
    return paths
  }

  it('acceptContribution 成功 → 重拉 pending + mine + gaps（徽标/统计卡即时翻转）', async () => {
    withSession({ role: 'dept_admin', canManage: true })
    const paths = recordFetch()
    await useContribute().acceptContribution(CONTRIB as never)
    expect(paths.some((p) => p.includes('/accept'))).toBe(true)
    expect(paths.some((p) => p.includes('/api/kb/contributions/pending'))).toBe(true)
    expect(paths.some((p) => p.includes('/api/kb/contributions/mine'))).toBe(true)
    expect(paths.some((p) => p.includes('/api/kb/gaps')), '缺口列表必须同步刷新').toBe(true)
  })

  it('rejectContribution 成功 → 同样重拉 mine + gaps（审核人=作者时「我的贡献」即时显驳回）', async () => {
    withSession({ role: 'dept_admin', canManage: true })
    const paths = recordFetch()
    await useContribute().rejectContribution(CONTRIB as never, '与现行制度冲突')
    expect(paths.some((p) => p.includes('/reject'))).toBe(true)
    expect(paths.some((p) => p.includes('/api/kb/contributions/mine'))).toBe(true)
    expect(paths.some((p) => p.includes('/api/kb/gaps'))).toBe(true)
  })
})
