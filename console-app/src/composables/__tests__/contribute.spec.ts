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

// 批次ε-2 Round1「告知→行动半环」：被驳回修改重交 + 失败原因透出 + 原稿可见
describe('useContribute — openModal content 预填（批次ε-2）', () => {
  it('传 content → 答案框带旧稿；不传（GapList 既有调用形）→ 照旧空白', () => {
    withSession()
    const { openModal, formContent, formQuestion } = useContribute()
    openModal({ question: '如何报销', content: '走 OA 流程提交。', dept: 'hr' })
    expect(formContent.value).toBe('走 OA 流程提交。')
    openModal({ question: '如何报销', dept: 'hr', sourceMessageId: 'm1', gapQuery: '如何报销' })
    expect(formContent.value).toBe('')          // 既有调用点行为不变
    expect(formQuestion.value).toBe('如何报销')
  })
})

describe('MyContributions — 重交/失败原因/原稿展开（批次ε-2）', () => {
  const BASE = {
    contribution_id: 'c1', question: '差旅报销保存几年？', content: '至少 5 年，见财务制度。',
    category_dept: 'finance', author_id: 'u1', author_name: '张三', review_status: 'pending',
    ingestion_status: 'none', state: 'pending', doc_id: null, review_note: '', created_at: '2026-07-01',
    reviewed_at: null,
  }
  function mountMine(rows: any[]) {
    withSession()
    useContribute().myContribs.value = rows as never
    return mount(MyContributions)
  }

  it('rejected 行显「修改重交」→ 弹窗带旧稿+原归属，提交体继承溯源；空驳回理由有兜底句', async () => {
    const w = mountMine([{ ...BASE, state: 'rejected', review_status: 'rejected', review_note: '', source_message_id: 'm9', gap_query: '报销凭证保存年限' }])
    expect(w.text()).toContain('未填写驳回理由')
    await w.find('[data-testid="mycontrib-reopen"]').trigger('click')
    const { modalOpen, formQuestion, formContent, formDept, submitContribution } = useContribute()
    expect(modalOpen.value).toBe(true)
    expect(formQuestion.value).toBe(BASE.question)
    expect(formContent.value).toBe(BASE.content)
    expect(formDept.value).toBe('finance')
    // 溯源继承走行为断言：重交提交体带原 source_message_id/gap_query（接上缺口关闭链路）
    const bodies: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (_p: string, init?: any) => {
      if (init?.body) bodies.push(JSON.parse(String(init.body)))
      return { ok: true, status: 200, json: async () => ({ items: [], summary: null, has_more: false }), text: async () => '{}' }
    }))
    await submitContribution()
    const submit = bodies.find((b) => 'category_dept' in b)
    expect(submit).toMatchObject({ source_message_id: 'm9', gap_query: '报销凭证保存年限' })
  })

  it('非 rejected 行不显重交入口', () => {
    const w = mountMine([{ ...BASE }])
    expect(w.find('[data-testid="mycontrib-reopen"]').exists()).toBe(false)
  })

  it('failed 行透出 ingestion_error；老后端缺字段 → 兜底句不留空白', () => {
    const w = mountMine([
      { ...BASE, contribution_id: 'f1', state: 'failed', ingestion_status: 'failed', ingestion_error: 'OSS 写入超时' },
      { ...BASE, contribution_id: 'f2', state: 'failed', ingestion_status: 'failed' },
    ])
    const reasons = w.findAll('[data-testid="mycontrib-fail-reason"]')
    expect(reasons.length).toBe(2)
    expect(reasons[0].text()).toBe('OSS 写入超时')
    expect(reasons[1].text()).toContain('入库失败')
  })

  it('点行标题展开原稿全文，再点收起', async () => {
    const w = mountMine([{ ...BASE }])
    expect(w.find('[data-testid="mycontrib-content"]').exists()).toBe(false)
    await w.find('[data-testid="mycontrib-toggle"]').trigger('click')
    expect(w.find('[data-testid="mycontrib-content"]').text()).toBe(BASE.content)
    await w.find('[data-testid="mycontrib-toggle"]').trigger('click')
    expect(w.find('[data-testid="mycontrib-content"]').exists()).toBe(false)
  })
})

// 批次ε-2 R2「价值反馈」：被引用数（cited 口径全期窗；null=算不出自隐、0=真零照显；排名不变）
describe('被引用数展示（批次ε-2 R2）', () => {
  it('HeroBoard：hits 非空显「引用 N」（含 0=真零）；null/缺字段（老后端）自隐', async () => {
    withSession()
    useContribute().heroes.value = [
      { rank: 1, author_id: 'u1', author_name: '李娜', count: 8, hits: 41 },
      { rank: 2, author_id: 'u2', author_name: '张三', count: 5, hits: 0 },
      { rank: 3, author_id: 'u3', author_name: '王五', count: 3, hits: null },
      { rank: 4, author_id: 'u4', author_name: '赵六', count: 1 },
    ] as never
    const HeroBoard = (await import('@/components/contribute/HeroBoard.vue')).default
    const w = mount(HeroBoard)
    const chips = w.findAll('[data-testid="hero-hits"]')
    expect(chips.map((c) => c.text())).toEqual(['引用 41', '引用 0'])   // null/缺字段两行自隐
    expect(w.text()).toContain('8')                                     // 排名主数字（入库篇数）仍在
  })

  it('MyContributions：仅已入库行显「被引用 N 次」；非 searchable / hits 缺失自隐', () => {
    withSession()
    useContribute().myContribs.value = [
      { contribution_id: 'h1', question: 'q1', content: 'a', category_dept: 'hr', author_id: 'u1', author_name: '', review_status: 'accepted', ingestion_status: 'searchable', state: 'searchable', doc_id: 'D1', review_note: '', created_at: '2026-07-01', reviewed_at: null, hits: 6 },
      { contribution_id: 'h2', question: 'q2', content: 'a', category_dept: 'hr', author_id: 'u1', author_name: '', review_status: 'accepted', ingestion_status: 'searchable', state: 'searchable', doc_id: 'D2', review_note: '', created_at: '2026-07-01', reviewed_at: null, hits: null },
      { contribution_id: 'h3', question: 'q3', content: 'a', category_dept: 'hr', author_id: 'u1', author_name: '', review_status: 'pending', ingestion_status: 'none', state: 'pending', doc_id: null, review_note: '', created_at: '2026-07-01', reviewed_at: null, hits: 9 },
    ] as never
    const w = mount(MyContributions)
    const chips = w.findAll('[data-testid="mycontrib-hits"]')
    expect(chips.length).toBe(1)
    expect(chips[0].text()).toBe('被引用 6 次')
  })
})
