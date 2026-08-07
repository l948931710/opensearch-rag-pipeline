import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { useSession } from '@/stores/session'
import { useContribute, __resetContribute } from '@/composables/useContribute'
import { contribStateLabel, contribStateTone, gapKindLabel, fmtTs } from '@/lib/kb'
import ContribBadge from '@/components/contribute/ContribBadge.vue'
import MyContributions from '@/components/contribute/MyContributions.vue'
import GapList from '@/components/contribute/GapList.vue'

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

  it('非 rejected/非入库受阻 行不显重交入口', () => {
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

  it('HeroBoard 总积分榜（2026-08-01，预览权重 10/1/3）：主数字=score、副行构成、预览口径标', async () => {
    withSession()
    useContribute().heroes.value = [
      { rank: 1, author_id: 'u2', author_name: '王伟', count: 2, hits: 0, adopted: 2, feedback: 8, score: 44 },
      { rank: 2, author_id: 'u1', author_name: '李娜', count: 3, hits: null, adopted: 3, feedback: 0, score: 30 },
    ] as never
    const HeroBoard = (await import('@/components/contribute/HeroBoard.vue')).default
    const w = mount(HeroBoard)
    expect(w.text()).toContain('总积分榜')
    expect(w.text()).toContain('预览口径')
    const rows = w.findAll('[data-testid="hero-breakdown"]')
    expect(rows.map((r) => r.text().replace(/\s+/g, ''))).toEqual(
      ['采纳×2·引用×0·反馈×8', '采纳×3·引用×—·反馈×0'])   // hits=null → 「—」不用 0 顶替
    expect(w.text()).toContain('44')
    // 总分模式下旧「引用 N」chip 不再出现（构成进了副行）
    expect(w.findAll('[data-testid="hero-hits"]').length).toBe(0)
  })

  it('HeroBoard 三榜切换：tab 栏随组织数据出现；部门榜行/本部门空态/全公司默认', async () => {
    withSession()
    const c = useContribute()
    c.heroes.value = [
      { rank: 1, author_id: 'u1', author_name: '李娜', count: 3, hits: 0, adopted: 3, feedback: 0, score: 30 },
    ] as never
    c.deptHeroes.value = [
      { rank: 1, dept_id: 100, dept_name: '生产中心', members: 2, score: 50 },
      { rank: 2, dept_id: 200, dept_name: '综合管理中心', members: 1, score: 10 },
    ] as never
    c.myDeptHeroes.value = [] as never
    c.myDeptName.value = '生产中心'
    const HeroBoard = (await import('@/components/contribute/HeroBoard.vue')).default
    const w = mount(HeroBoard)
    // 默认全公司
    expect(w.find('[data-testid="hero-breakdown"]').exists()).toBe(true)
    // 切部门榜
    await w.find('[data-testid="hero-tab-dept"]').trigger('click')
    const rows = w.findAll('[data-testid="hero-dept-row"]')
    expect(rows.length).toBe(2)
    expect(rows[0].text()).toContain('生产中心')
    expect(rows[0].text()).toContain('2 人参与')
    expect(rows[0].text()).toContain('50')
    // 切本部门（空态诚实提示）
    await w.find('[data-testid="hero-tab-mydept"]').trigger('click')
    expect(w.text()).toContain('你所在部门暂无上榜数据')
    expect(w.text()).toContain('本部门 · 生产中心')
  })

  it('HeroBoard 组织数据缺失：tab 栏自隐，观感与单榜一致', async () => {
    withSession()
    const c = useContribute()
    c.heroes.value = [
      { rank: 1, author_id: 'u1', author_name: '李娜', count: 3, adopted: 3, feedback: 0, score: 30 },
    ] as never
    c.deptHeroes.value = [] as never
    c.myDeptHeroes.value = [] as never
    const HeroBoard = (await import('@/components/contribute/HeroBoard.vue')).default
    const w = mount(HeroBoard)
    expect(w.find('[data-testid="hero-tab-dept"]').exists()).toBe(false)
    expect(w.find('[data-testid="hero-breakdown"]').exists()).toBe(true)
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

// 批次ε-3 R1：registering 的展示细分（待放行/入库受阻/正常排队）——此前三者同显「已采纳·待入库」
describe('MyContributions — 管线徽章细分（批次ε-3 R1）', () => {
  const REG = {
    contribution_id: 'r1', question: '车间安全巡检表在哪？', content: '在 OA…', category_dept: 'production',
    author_id: 'u1', author_name: '张三', review_status: 'accepted', ingestion_status: 'registered',
    state: 'registering', doc_id: 'D1', review_note: '', created_at: '2026-07-01', reviewed_at: '2026-07-02',
  }
  function mountRows(rows: any[]) {
    withSession()
    useContribute().myContribs.value = rows as never
    return mount(MyContributions)
  }

  it('doc_badge=待审核 → 「已采纳·待放行」+ 放行提示；已隔离/未入索引 → 「入库受阻」+ 死链提示（两种文案互异）', () => {
    const w = mountRows([
      { ...REG, contribution_id: 'r1', doc_badge: '待审核' },
      { ...REG, contribution_id: 'r2', doc_badge: '已隔离' },
      { ...REG, contribution_id: 'r3', doc_badge: '未入索引' },
    ])
    expect(w.text()).toContain('已采纳·待放行')
    expect(w.find('[data-testid="mycontrib-approval-hint"]').text()).toContain('放行')
    const stalls = w.findAll('[data-testid="mycontrib-stall-reason"]')
    expect(stalls.length).toBe(2)
    expect(stalls[0].text()).toContain('敏感信息隔离')
    expect(stalls[1].text()).toContain('未能生成可检索内容')
    expect(w.text()).toContain('入库受阻')
  })

  it('doc_badge=排队中/缺省（老后端）→ 回落默认「已采纳·待入库」，零提示行', () => {
    const w = mountRows([
      { ...REG, contribution_id: 'r4', doc_badge: '排队中' },
      { ...REG, contribution_id: 'r5' },
    ])
    expect(w.text()).toContain('已采纳·待入库')
    expect(w.text()).not.toContain('待放行')
    expect(w.find('[data-testid="mycontrib-approval-hint"]').exists()).toBe(false)
    expect(w.find('[data-testid="mycontrib-stall-reason"]').exists()).toBe(false)
  })

  it('doc_badge 只对 registering 生效：searchable 行即使带值也不细分（状态机词表不越界）', () => {
    const w = mountRows([
      { ...REG, contribution_id: 'r6', review_status: 'accepted', ingestion_status: 'searchable', state: 'searchable', doc_badge: '待审核' },
    ])
    expect(w.text()).toContain('已入库')
    expect(w.text()).not.toContain('待放行')
  })
})

// 批次ε-3 R3：缺口窗口标注（后端下发防漂移）——「待回答」不是完整积压，超窗老缺口静默消失
describe('GapList — 窗口标注（ε-3 R3）', () => {
  it('卡头显「近 N 天」（读后端 window_days）；空态同样带窗口语义', async () => {
    withSession()
    const { gapsWindowDays } = useContribute()
    gapsWindowDays.value = 30
    const w = mount(GapList)
    expect(w.find('[data-testid="gap-window-note"]').text()).toContain('近 30 天')
    // ε-5 R2 空态两成因：标题自带窗口限定 + 副题明说出窗语义（出窗计数=远期立项，文案方案）
    expect(w.text()).toContain('近 30 天内暂无未答出的提问')
    expect(w.text()).toContain('超出统计窗口')
    expect(w.text()).toContain('不代表已解决')
  })

  it('loadGaps 收 window_days=14 → 标注跟随；老后端缺字段 → 回落 30', async () => {
    withSession()
    stubFetch({ items: [], summary: { unanswered: 0, answered: 0, this_month: 0, contributors: 0 }, has_more: false, window_days: 14 })
    const { loadGaps, gapsWindowDays } = useContribute()
    await loadGaps()
    expect(gapsWindowDays.value).toBe(14)
    stubFetch({ items: [], summary: { unanswered: 0, answered: 0, this_month: 0, contributors: 0 }, has_more: false })
    await loadGaps()
    expect(gapsWindowDays.value).toBe(30)
  })
})

// 批次ε-4：「待回答」→ 高频无人回答排行 Top 30（365 天窗）
describe('GapList — Top 30 排行（ε-4）', () => {
  const GAP = (i: number) => ({
    question: `高频问题第${i}号是什么？`, asks: 100 - i, last_days: i, dept: 'it', kind: 'no_result',
    question_hash: `h${i}`, source_message_id: `m${i}`, has_pending_contribution: false,
  })
  function seed(n: number, unanswered: number) {
    withSession()
    const c = useContribute()
    c.gaps.value = Array.from({ length: n }, (_, i) => GAP(i + 1)) as never
    c.gapsSummary.value = { unanswered, answered: 0, this_month: 0, contributors: 0 } as never
    return mount(GapList)
  }

  it('fmtWindowDays：365→「近一年」；其余沿用「近 N 天」全站惯例', async () => {
    const { fmtWindowDays } = await import('@/lib/kb')
    expect(fmtWindowDays(365)).toBe('近一年')
    expect(fmtWindowDays(30)).toBe('近 30 天')
    expect(fmtWindowDays(400)).toBe('近一年')
  })

  it('截断态：卡头徽标=全集数（非当页数）、名次 1..N 递增、尾注披露两口径、永无「加载更多」', () => {
    const w = seed(30, 87)
    expect(w.find('[data-testid="gap-total-badge"]').text()).toBe('87')   // 与统计卡同口径
    const ranks = w.findAll('[data-testid="gap-rank"]').map((r) => r.text())
    expect(ranks.length).toBe(30)
    expect(ranks[0]).toBe('1'); expect(ranks[29]).toBe('30')
    expect(w.find('[data-testid="gap-top-note"]').text()).toBe('仅显示询问最多的前 30 条 · 共 87 条待回答')
    expect(w.text()).not.toContain('加载更多')
  })

  it('未截断（全集 ≤30）：尾注自隐不误导、徽标=真实条数；窗口 365 → 卡头显「近一年」', () => {
    withSession()
    useContribute().gapsWindowDays.value = 365
    const w = seed(12, 12)
    expect(w.find('[data-testid="gap-top-note"]').exists()).toBe(false)
    expect(w.find('[data-testid="gap-total-badge"]').text()).toBe('12')
    expect(w.find('[data-testid="gap-window-note"]').text()).toContain('近一年')
    expect(w.find('[data-testid="gap-window-note"]').text()).toContain('按询问次数排序')
  })
})

// 批次ε-5 R1：状态可辨收尾——隐形态补全（处理失败/内容未变）+ 入库受阻行分流重投
describe('MyContributions — 隐形态补全与分流重投（ε-5 R1）', () => {
  const REG5 = (over: Record<string, unknown> = {}) => ({
    contribution_id: 'r1', question: '车间安全巡检表在哪？', content: '在 OA…', category_dept: 'production',
    author_id: 'u1', author_name: '张三', review_status: 'accepted', ingestion_status: 'registered',
    state: 'registering', doc_id: 'D1', review_note: '', created_at: '2026-07-01', reviewed_at: '2026-07-02',
    source_message_id: 'm7', gap_query: '巡检表原问', ...over,
  })

  it('doc_badge=处理失败 → 「入库受阻」+专属提示（非排队措辞）+重投入口（作者无重试权的出路）', () => {
    withSession()
    useContribute().myContribs.value = [REG5({ doc_badge: '处理失败' })] as never
    const w = mount(MyContributions)
    expect(w.text()).toContain('入库受阻')
    expect(w.find('[data-testid="mycontrib-stall-reason"]').text()).toContain('入库处理失败')
    expect(w.find('[data-testid="mycontrib-reopen"]').exists()).toBe(true)
  })

  it('doc_badge=内容未变 → 「同内容已在库」muted+去重事实提示，无重投（原样重投仍判重复）', () => {
    withSession()
    useContribute().myContribs.value = [REG5({ doc_badge: '内容未变' })] as never
    const w = mount(MyContributions)
    expect(w.text()).toContain('同内容已在库')
    expect(w.find('[data-testid="mycontrib-duplicate-hint"]').text()).toContain('完全相同')
    expect(w.find('[data-testid="mycontrib-reopen"]').exists()).toBe(false)
    expect(w.find('[data-testid="mycontrib-stall-reason"]').exists()).toBe(false)
    expect(w.text()).not.toContain('待入库')
  })

  it('入库受阻重投：已隔离 → 弹窗警示明说「再次被隔离」；未入索引 → 常规改稿措辞（分流）', async () => {
    withSession()
    const c = useContribute()
    c.myContribs.value = [REG5({ contribution_id: 'q1', doc_badge: '已隔离' }),
                          REG5({ contribution_id: 'q2', doc_badge: '未入索引' })] as never
    const w = mount(MyContributions)
    const btns = w.findAll('[data-testid="mycontrib-reopen"]')
    expect(btns.length).toBe(2)
    await btns[0].trigger('click')
    expect(c.formWarning.value).toContain('再次被隔离')
    expect(c.formContent.value).toBe('在 OA…')          // 预填保留：旧稿做底稿
    expect(c.formQuestion.value).toContain('巡检表')
    await btns[1].trigger('click')
    expect(c.formWarning.value).toContain('修改完善')
    expect(c.formWarning.value).not.toContain('隔离')
  })

  it('rejected 行重投不带警示；openModal 无 warning 清残留（不跨行泄漏）', async () => {
    withSession()
    const c = useContribute()
    c.formWarning.value = '残留警示'
    c.openModal({ question: 'Q' })
    expect(c.formWarning.value).toBe('')
  })

  it('重试按钮按角色收敛：员工不显死按钮（端点管理员专属，点了恒 403），但有「修改重交」自助出路；canManage 显重试', () => {
    withSession({ canManage: false })
    const FAILED = REG5({ review_status: 'accepted', ingestion_status: 'failed', state: 'failed', doc_badge: undefined })
    useContribute().myContribs.value = [FAILED] as never
    let w = mount(MyContributions)
    const retryBtns = (x: ReturnType<typeof mount>) => x.findAll('button').filter((b) => b.text().trim() === '重试')
    expect(retryBtns(w).length).toBe(0)
    expect(w.find('[data-testid="mycontrib-reopen"]').exists()).toBe(true)   // 自助出路
    __resetContribute()
    withSession({ canManage: true, role: 'dept_admin' })
    useContribute().myContribs.value = [FAILED] as never
    w = mount(MyContributions)
    expect(retryBtns(w).length).toBe(1)
  })
})

// 批次ε-5 R2：台账词表 seam 锁——后端封闭集(_KB_BADGE_VOCAB, test_kb_status_badge_closed_set 锁)
// ↔ 前端 BADGE_TONE ↔ MyContributions displayState 特判词，三处人工同步靠双侧测试互指兜底
describe('台账词表 seam 锁（ε-5 R2）', () => {
  // 与 opensearch_pipeline/api.py::_KB_BADGE_VOCAB 逐字镜像——改词表两边测试都得动（有意的摩擦）
  const BACKEND_VOCAB = ['已退役', '已隔离', '未入索引', '已上线', '处理失败',
                         '已驳回', '内容未变', '待审核', '排队中', '处理中',
                         // C8（2026-08-04）：审批放行的字节 ≠ 摄取到的字节，不可自动重试
                         '内容不符',
                         // 2026-08-06：document_version.status='superseded' —— 升版后旧版本的
                         // 正常生命周期终点。此前无人消费该列，旧版本一路显示成「已上线」。
                         '历史版本']

  it('BADGE_TONE 键集 = 后端封闭集镜像（漂移=可见性静默回归，先在这里红）', async () => {
    const { BADGE_TONE } = await import('@/lib/kb')
    expect(new Set(Object.keys(BADGE_TONE))).toEqual(new Set(BACKEND_VOCAB))
  })

  it('MyContributions displayState 特判词 ⊆ 词表（新增徽章词没做分流决策 → 这里红）', async () => {
    const { BADGE_TONE } = await import('@/lib/kb')
    // 与 MyContributions.vue 的 STALLED_BADGES + 待审核/内容未变 特判逐字对齐
    const DISPLAY_KEYS = ['待审核', '内容未变', '已隔离', '未入索引', '处理失败', '内容不符']
    for (const k of DISPLAY_KEYS) expect(k in BADGE_TONE, `特判词 ${k} 不在词表`).toBe(true)
  })
})

// 「忽略此缺口」（2026-07-15 拍板交 dept_admin）：composable 动作层 + 组件门禁
describe('GapList — 忽略此缺口', () => {
  const GAPX = () => ({
    question: '模具保养周期是多久', asks: 5, last_days: 3, dept: 'production', kind: 'no_result',
    question_hash: 'a'.repeat(64), source_message_id: 'm1', has_pending_contribution: false,
  })
  function seedOne(canManage: boolean) {
    withSession({ canManage, role: canManage ? 'dept_admin' : 'employee' })
    const c = useContribute()
    c.gaps.value = [GAPX()] as never
    c.gapsSummary.value = { unanswered: 7, answered: 0, this_month: 0, contributors: 0 } as never
    return c
  }

  it('dismissGap 成功：POST 带 hash/question/reason，行内翻转 dismissed=true 不重拉', async () => {
    const c = seedOne(true)
    stubFetch({ ok: true, affected: 1 })
    await c.dismissGap(c.gaps.value[0], '闲聊噪音')
    const fetchMock = (globalThis.fetch as any)
    expect(fetchMock).toHaveBeenCalledTimes(1)                       // 无 loadGaps 重拉
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/api/kb/gaps/dismiss')
    expect(JSON.parse(init.body)).toMatchObject({
      question_hash: 'a'.repeat(64), question: '模具保养周期是多久', reason: '闲聊噪音',
    })
    expect(c.gaps.value[0].dismissed).toBe(true)
  })

  it('dismissGap 失败：不乐观翻转（dismissed 保持 falsy），行不消失', async () => {
    const c = seedOne(true)
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false, status: 500, json: async () => ({ detail: 'boom' }), text: async () => 'boom',
    })))
    await c.dismissGap(c.gaps.value[0], '')
    expect(c.gaps.value[0].dismissed).not.toBe(true)
    expect(c.gaps.value.length).toBe(1)
  })

  it('restoreGap：POST 同 hash，翻回 dismissed=false', async () => {
    const c = seedOne(true)
    c.gaps.value[0].dismissed = true
    stubFetch({ ok: true, affected: 1 })
    await c.restoreGap(c.gaps.value[0])
    const [url, init] = (globalThis.fetch as any).mock.calls[0]
    expect(String(url)).toContain('/api/kb/gaps/restore')
    expect(JSON.parse(init.body)).toMatchObject({ question_hash: 'a'.repeat(64) })
    expect(c.gaps.value[0].dismissed).toBe(false)
  })

  it('组件门禁：canManage 才渲染忽略钮；员工 DOM 无忽略/撤销节点（零变化）', () => {
    seedOne(false)
    let w = mount(GapList)
    expect(w.find('[data-testid="gap-dismiss"]').exists()).toBe(false)
    expect(w.find('[data-testid="gap-restore"]').exists()).toBe(false)
    expect(w.text()).toContain('回答')
    __resetContribute()
    const c = seedOne(true)
    w = mount(GapList)
    expect(w.find('[data-testid="gap-dismiss"]').exists()).toBe(true)
    // 已忽略态：末尾动作位互斥切换为「已忽略+撤销」，回答钮隐藏
    c.gaps.value[0].dismissed = true
    return w.vm.$nextTick().then(() => {
      expect(w.find('[data-testid="gap-dismissed-hint"]').exists()).toBe(true)
      expect(w.find('[data-testid="gap-restore"]').exists()).toBe(true)
      expect(w.find('[data-testid="gap-dismiss"]').exists()).toBe(false)
    })
  })
})

// 「已忽略」折叠区（2026-07-15 拍板补齐）：composable 层
describe('GapList — 已忽略折叠区', () => {
  it('loadDismissed 拉取 active 行；restoreDismissed=restore+移出+重拉 gaps', async () => {
    withSession({ canManage: true, role: 'dept_admin' })
    const c = useContribute()
    const D = { question_hash: 'd'.repeat(64), question_preview: '今天食堂有什么菜',
                reason: '闲聊', dismissed_by_name: '管理员', dismissed_at: '2026-07-15T10:00:00' }
    stubFetch({ items: [D] })
    await c.loadDismissed()
    expect(c.dismissedGaps.value.length).toBe(1)
    // restore：POST 同 hash → 移出折叠区 → 触发 loadGaps 重拉（回到待回答的诚实闭环）
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: any) => {
      calls.push(String(url))
      return { ok: true, status: 200, json: async () => ({ ok: true, items: [], summary: null }), text: async () => '{}' }
    }))
    await c.restoreDismissed(c.dismissedGaps.value[0])
    expect(c.dismissedGaps.value.length).toBe(0)
    expect(calls.some((u) => u.includes('/api/kb/gaps/restore'))).toBe(true)
    expect(calls.some((u) => u.includes('/api/kb/gaps?limit=30'))).toBe(true)
  })

  it('restore 失败：行留在折叠区（不乐观移除）', async () => {
    withSession({ canManage: true, role: 'dept_admin' })
    const c = useContribute()
    c.dismissedGaps.value = [{ question_hash: 'e'.repeat(64), question_preview: 'x',
                               reason: '', dismissed_by_name: '', dismissed_at: '' }] as never
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false, status: 500, json: async () => ({ detail: 'boom' }), text: async () => 'boom',
    })))
    await c.restoreDismissed(c.dismissedGaps.value[0])
    expect(c.dismissedGaps.value.length).toBe(1)
  })

  it('组件门禁：员工无折叠区节点；管理员默认收起', () => {
    withSession({ canManage: false })
    const c = useContribute()
    c.gaps.value = [{ question: 'q', asks: 1, last_days: 1, dept: '', kind: 'no_result',
                      question_hash: 'f'.repeat(64), source_message_id: '', has_pending_contribution: false }] as never
    let w = mount(GapList)
    expect(w.find('[data-testid="gap-dismissed-toggle"]').exists()).toBe(false)
    __resetContribute()
    withSession({ canManage: true, role: 'dept_admin' })
    const c2 = useContribute()
    c2.gaps.value = [] as never
    w = mount(GapList)
    expect(w.find('[data-testid="gap-dismissed-toggle"]').exists()).toBe(true)
    expect(w.find('[data-testid="gap-dismissed-row"]').exists()).toBe(false)   // 默认收起
  })
})

// 2026-07-18：语义归组 chip + 缺口卡会话上下文展开
describe('GapList — phrasings chip 与上下文展开', () => {
  const BASE_GAP = {
    question: '如何申请密钥？', asks: 5, last_days: 2, dept: 'it', kind: 'no_result',
    question_hash: 'hg1', source_message_id: 'mg1', has_pending_contribution: false,
  }
  function seedOne(extra: Record<string, unknown>) {
    withSession()
    const c = useContribute()
    c.gaps.value = [{ ...BASE_GAP, ...extra }] as never
    c.gapsSummary.value = { unanswered: 1, answered: 0, this_month: 0, contributors: 0 } as never
    return mount(GapList)
  }

  it('phrasings>1 → 显「N 种问法」chip；缺省/1（flag 关或老后端）→ 零节点', () => {
    let w = seedOne({ phrasings: 3 })
    expect(w.find('[data-testid="gap-phrasings"]').text()).toContain('3 种问法')
    w = seedOne({ phrasings: 1 })
    expect(w.find('[data-testid="gap-phrasings"]').exists()).toBe(false)
    w = seedOne({})
    expect(w.find('[data-testid="gap-phrasings"]').exists()).toBe(false)
  })

  it('仅 has_context=true 显「查看上下文」；点击懒加载并渲染前几轮（脱敏由服务端）', async () => {
    let w = seedOne({})
    expect(w.find('[data-testid="gap-ctx-toggle"]').exists()).toBe(false)   // 缺省不显
    w = seedOne({ has_context: true, representative_message_id: 'mg1' })
    const btn = w.find('[data-testid="gap-ctx-toggle"]')
    expect(btn.exists()).toBe(true)
    stubFetch({ items: [
      { question: '第一个问题', answer_status: 'SUCCESS', answer_excerpt: '答案节选', created_at: '2026-07-18 09:00' },
      { question: '第二个问题', answer_status: 'NO_RESULT', answer_excerpt: '', created_at: '2026-07-18 09:30' },
    ] })
    await btn.trigger('click')
    await new Promise((r) => setTimeout(r))
    await w.vm.$nextTick()
    const panel = w.find('[data-testid="gap-ctx-panel"]')
    expect(panel.exists()).toBe(true)
    expect(panel.text()).toContain('第一个问题')
    expect(panel.text()).toContain('答案节选')
    expect(panel.text()).toContain('未找到答案')      // NO_RESULT 轮显状态不显节选
    // 再点折叠
    await btn.trigger('click')
    expect(w.find('[data-testid="gap-ctx-panel"]').exists()).toBe(false)
  })

  it('上下文接口失败/空 → 显「无会话上文」，不打断认领流', async () => {
    const w = seedOne({ has_context: true, representative_message_id: 'mg1' })
    stubFetch({ items: [] })
    await w.find('[data-testid="gap-ctx-toggle"]').trigger('click')
    await new Promise((r) => setTimeout(r))
    await w.vm.$nextTick()
    expect(w.find('[data-testid="gap-ctx-panel"]').text()).toContain('无会话上文')
  })
})

// ── P2-11：loadMine 此前丢弃 has_more（>50 条的贡献者看不到旧稿）────────────────
describe('useContribute.loadMine 分页', () => {
  it('消费 has_more；offset>0 追加而非替换；offset=0 回首页替换', async () => {
    const calls: string[] = []
    const page = (ids: string[], has_more: boolean) => ({
      items: ids.map((id) => ({ contribution_id: id, question: id, state: 'pending', created_at: '' })),
      has_more,
    })
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      calls.push(String(url))
      const off = /offset=(\d+)/.exec(String(url))?.[1] ?? '0'
      return { ok: true, status: 200, json: async () => (off === '0' ? page(['a', 'b'], true) : page(['c'], false)) }
    }))
    const c = useContribute()
    await c.loadMine()
    expect(c.myContribs.value.map((x) => x.contribution_id)).toEqual(['a', 'b'])
    expect(c.mineHasMore.value).toBe(true)
    expect(calls[0]).toContain('offset=0')

    await c.loadMine(c.myContribs.value.length)
    expect(c.myContribs.value.map((x) => x.contribution_id)).toEqual(['a', 'b', 'c'])
    expect(c.mineHasMore.value).toBe(false)
    expect(calls[1]).toContain('offset=2')

    await c.loadMine()   // 回首页 → 替换
    expect(c.myContribs.value.map((x) => x.contribution_id)).toEqual(['a', 'b'])
  })

  it('翻页失败不清空已加载的前几页（只有首屏失败才清空）', async () => {
    let n = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      n += 1
      if (n === 1) return { ok: true, status: 200, json: async () => ({ items: [{ contribution_id: 'a', question: 'a', state: 'pending', created_at: '' }], has_more: true }) }
      return { ok: false, status: 500, json: async () => ({}), text: async () => 'boom' }
    }))
    const c = useContribute()
    await c.loadMine()
    await c.loadMine(1)
    expect(c.myContribs.value.map((x) => x.contribution_id)).toEqual(['a'])
  })
})

// ── P2-13：loadHeroes 此前静默 catch，HeroBoard 的 v-if="heroes.length" 会让整块消失 ──
describe('useContribute.loadHeroes 失败可见性', () => {
  it('加载失败 → 记 loadErrors["heroes"]（不再与「榜上无人」混同）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}), text: async () => 'boom' })))
    const c = useContribute()
    await c.loadHeroes()
    expect(c.heroes.value).toEqual([])
    expect(c.loadErrors.value['heroes']).toBeTruthy()
  })

  it('重试成功 → 错误清除', async () => {
    let n = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      n += 1
      return n === 1
        ? { ok: false, status: 500, json: async () => ({}), text: async () => 'boom' }
        : { ok: true, status: 200, json: async () => ({ items: [{ author_name: '张三', count: 3 }] }) }
    }))
    const c = useContribute()
    await c.loadHeroes()
    expect(c.loadErrors.value['heroes']).toBeTruthy()
    await c.loadHeroes()
    expect(c.loadErrors.value['heroes']).toBeFalsy()
    expect(c.heroes.value.length).toBe(1)
  })
})

// ── 贡献域 node 轴（schema/067，方案 M9/M11）────────────────────────────────
describe('提交端归属两轴 — my-depts / openModal / 提交载荷', () => {
  it('loadMyDepts：available=false ⇒ 回落组码轴（不是"没有可选部门"）', async () => {
    withSession()
    stubFetch({ items: [{ dept_id: 930001, name: '三级班组' }], available: false })
    const { loadMyDepts, myDepts, myDeptsReady, openModal, formDept, formDeptId } = useContribute()
    await loadMyDepts()
    expect(myDeptsReady.value).toBe(true)
    expect(myDepts.value).toEqual([])
    openModal({ dept: 'hr' })
    expect(formDeptId.value).toBeNull()
    expect(formDept.value).toBe('hr')     // 组码轴行为逐字节不变
  })

  it('node 轴：openModal 默认取 my-depts 第一项；原归属仍可选则沿用', async () => {
    withSession()
    stubFetch({ items: [{ dept_id: 930001, name: '三级班组' }, { dept_id: 930002, name: '另一班组' }] })
    const { loadMyDepts, openModal, formDept, formDeptId } = useContribute()
    await loadMyDepts()
    openModal()
    expect(formDeptId.value).toBe(930001)
    expect(formDept.value).toBe('')
    openModal({ deptId: 930002 })
    expect(formDeptId.value).toBe(930002)
  })

  it('🔴 legacy rejected 行重开：绝不从旧组码反推 node 归属，回落 my-depts 默认项', async () => {
    // 组码 → dept_id 是一对多（方案 M8/M11 明令）：若这里按 category_dept 猜一个 dept_id，
    // 就把 M8 禁止的推断性映射从前端绕了回来。
    withSession()
    stubFetch({ items: [{ dept_id: 930001, name: '三级班组' }] })
    const { loadMyDepts, openModal, formDeptId, formDept } = useContribute()
    await loadMyDepts()
    openModal({ question: '旧稿', content: '旧正文', dept: 'hr', deptId: null })
    expect(formDeptId.value).toBe(930001)
    expect(formDept.value).toBe('')
  })

  it('不在可选集里的原归属（管辖被收回/节点停用）同样回落默认项，不留死值', async () => {
    withSession()
    stubFetch({ items: [{ dept_id: 930001, name: '三级班组' }] })
    const { loadMyDepts, openModal, formDeptId } = useContribute()
    await loadMyDepts()
    openModal({ deptId: 999999 })
    expect(formDeptId.value).toBe(930001)
  })

  it('提交载荷两轴互斥：node 轴只发 category_dept_id，组码轴只发 category_dept', async () => {
    withSession()
    stubFetch({ items: [{ dept_id: 930001, name: '三级班组' }] })
    const { loadMyDepts, openModal, formQuestion, formContent, submitContribution } = useContribute()
    await loadMyDepts()
    openModal()
    formQuestion.value = '问题'; formContent.value = '答案'
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (_u: string, init: any) => {
      calls.push(JSON.parse(init.body))
      return { ok: true, status: 200, json: async () => ({ items: [], has_more: false }), text: async () => '{}' }
    }))
    await submitContribution()
    const body = calls[0]
    expect(body.category_dept_id).toBe(930001)
    expect('category_dept' in body).toBe(false)
  })

  it('GapList 的 g.dept 在 node 轴下不得决定归属（降为展示性建议）', async () => {
    withSession()
    stubFetch({ items: [{ dept_id: 930001, name: '三级班组' }] })
    const { loadMyDepts, openModal, formDeptId, formDept } = useContribute()
    await loadMyDepts()
    // GapList.onAnswer 的等价调用：只带组码建议，不带 deptId
    openModal({ question: '缺口问题', dept: 'production', gapQuery: '缺口问题' })
    expect(formDeptId.value).toBe(930001)
    expect(formDept.value).toBe('')
  })
})
