import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import FeedbackReviewList from '@/components/manage/FeedbackReviewList.vue'
import KbAdminDashboard from '@/components/manage/KbAdminDashboard.vue'
import { useKb, __resetKb, type FeedbackReviewItem } from '@/composables/useKb'

// 差评复核显式降级（staging 2026-07-11 P1 教训）：/api/kb/feedback-review 500 时曾静默
// 伪装成「无差评」——快乐空态照显、「待你处理」chip 全隐，管理员无从知晓差评存在或功能已坏。
// 本组断言固化契约：真错误（5xx/网络）→ 错误占位卡（含重试）+ 置顶「加载失败」chip；
// 404（端点未上线）→ 才允许如实兜底空；旧数据遇刷新失败 → 保留列表 + 顶部错误条。

beforeEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); __resetKb() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '张三', role: 'kb_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'], ...over }
}

// 先激活 pinia（useKb→useSession 需要）→ 组件 mount 时读到同一份（与 dashboard.spec 同约定）。
function activePinia(id: Identity) {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 'TKN', ready: true } } })
  setActivePinia(pinia)
  return pinia
}

function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}

// 反馈时间用「相对现在」生成：组件默认按近 30 天过滤（设计稿 2026-07-19 §4），
// 写死日期会随时间流逝被滤掉导致测试腐烂。
function tsAgo(days: number): string {
  const d = new Date(Date.now() - days * 86400000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function fb(id: string, over: Partial<FeedbackReviewItem> = {}): FeedbackReviewItem {
  return {
    message_id: id, question: `模具冷却水路怎么排查-${id}`, created_at: tsAgo(1),
    reasons: ['不准确'], comment: '', handled: false, handled_status: 'PENDING', docs: [],
    ...over,
  }
}

const stubs = { RouterLink: { props: ['to'], template: '<a><slot /></a>' } }

describe('FeedbackReviewList — 接口失败显式降级，绝不伪装成「无差评」', () => {
  it('500 → 错误占位卡（含重试）；快乐空态/加载中均不渲染；数据保持 null 而非兜成 []', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ detail: 'boom' }, { ok: false, status: 500 })))
    const pinia = activePinia(identity())
    const kb = useKb()
    await kb.loadFeedbackReview()

    expect(kb.feedbackReview.value).toBeNull()                        // 数据层：失败 ≠ 空
    expect(kb.loadErrors.value['feedbackReview']).toBeTruthy()

    const w = mount(FeedbackReviewList, { global: { plugins: [pinia], stubs } })
    expect(w.find('[data-testid="feedback-review-error"]').exists()).toBe(true)
    expect(w.text()).toContain('差评复核加载失败')
    expect(w.text()).toContain('重试')
    expect(w.text()).not.toContain('干得漂亮')                        // 快乐空态绝不与故障同现
    expect(w.text()).not.toContain('近期无「引用本部门文档且被点踩」')
    expect(w.text()).not.toContain('拉取中')
  })

  it('错误占位卡点「重试」→ 后端恢复后列表渲染、错误清除', async () => {
    let calls = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      calls += 1
      return calls === 1 ? jsonResp({ detail: 'boom' }, { ok: false, status: 500 }) : jsonResp({ items: [fb('m1')] })
    }))
    const pinia = activePinia(identity())
    const kb = useKb()
    await kb.loadFeedbackReview()

    const w = mount(FeedbackReviewList, { global: { plugins: [pinia], stubs } })
    await w.find('[data-testid="feedback-review-error"] button').trigger('click')
    await vi.waitFor(() => expect(w.text()).toContain('模具冷却水路怎么排查-m1'))
    expect(w.find('[data-testid="feedback-review-error"]').exists()).toBe(false)
    expect(kb.loadErrors.value['feedbackReview']).toBeUndefined()
  })

  it('404（端点未上线）→ 如实兜底空 + 快乐空态，不显错误占位', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ detail: 'nf' }, { ok: false, status: 404 })))
    const pinia = activePinia(identity())
    const kb = useKb()
    await kb.loadFeedbackReview()

    expect(kb.feedbackReview.value).toEqual([])
    expect(kb.loadErrors.value['feedbackReview']).toBeUndefined()

    const w = mount(FeedbackReviewList, { global: { plugins: [pinia], stubs } })
    expect(w.text()).toContain('干得漂亮')
    expect(w.find('[data-testid="feedback-review-error"]').exists()).toBe(false)
    expect(w.text()).not.toContain('差评复核加载失败')
  })

  it('已有旧数据 + 刷新 500 → 旧列表保留 + 顶部错误条（不整体替换成错误卡）', async () => {
    let calls = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      calls += 1
      return calls === 1 ? jsonResp({ items: [fb('m1')] }) : jsonResp({ detail: 'boom' }, { ok: false, status: 500 })
    }))
    const pinia = activePinia(identity())
    const kb = useKb()
    await kb.loadFeedbackReview()
    await kb.loadFeedbackReview()                                     // 第二次失败

    expect(kb.feedbackReview.value).toHaveLength(1)                   // 旧值保留
    const w = mount(FeedbackReviewList, { global: { plugins: [pinia], stubs } })
    expect(w.text()).toContain('模具冷却水路怎么排查-m1')
    expect(w.text()).toContain('加载失败，请重试')                     // LoadError 错误条
    expect(w.find('[data-testid="feedback-review-error"]').exists()).toBe(false)
  })
})

describe('FeedbackReviewList — 红头卡 + 时间范围 + 分页/排序（设计稿 2026-07-19 §1/§2/§4）', () => {
  function mountList(items: FeedbackReviewItem[]) {
    const pinia = activePinia(identity())
    const kb = useKb()
    kb.feedbackReview.value = items
    return { w: mount(FeedbackReviewList, { global: { plugins: [pinia], stubs } }), kb }
  }

  it('红头语义：卡边框 st-fail 混合 + 头 st-fail 底 + 计数 st-fail', () => {
    const { w } = mountList([fb('m1')])
    expect(w.text()).toContain('差评复核')
    expect(w.find('[class*="border-st-fail/30"][class*="bg-card"]').exists()).toBe(true)   // 卡边框
    expect(w.find('[class*="bg-st-fail/"][class*="border-b"]').exists()).toBe(true)         // 红头底
    expect(w.find('span[class*="rounded-full"][class*="bg-st-fail"]').text()).toBe('1')     // 计数
  })

  it('时间范围下拉：默认近 30 天滤掉 40 天前的行；切「全部时间」找回', async () => {
    const { w } = mountList([fb('recent', { created_at: tsAgo(2) }), fb('old', { created_at: tsAgo(40) })])
    expect(w.text()).toContain('模具冷却水路怎么排查-recent')
    expect(w.text()).not.toContain('模具冷却水路怎么排查-old')
    await w.find('[data-testid="feedback-range"]').setValue('all')
    expect(w.text()).toContain('模具冷却水路怎么排查-old')
    // 缺时间戳的行不因过滤而消失（graceful degradation）
    const { w: w2 } = mountList([fb('nots', { created_at: '' })])
    expect(w2.text()).toContain('模具冷却水路怎么排查-nots')
  })

  it('分页：3 条 → 2 条/页；默认新→旧；排序切换回第 1 页', async () => {
    const { w } = mountList([fb('m1', { created_at: tsAgo(1) }), fb('m2', { created_at: tsAgo(2) }), fb('m3', { created_at: tsAgo(3) })])
    expect(w.text()).toContain('模具冷却水路怎么排查-m1')
    expect(w.text()).toContain('模具冷却水路怎么排查-m2')
    expect(w.text()).not.toContain('模具冷却水路怎么排查-m3')
    expect(w.find('[data-testid="pager-info"]').text()).toBe('第 1–2 条 · 共 3 条')
    await w.find('[aria-label="下一页"]').trigger('click')
    expect(w.text()).toContain('模具冷却水路怎么排查-m3')
    // 排序切旧→新 → 回第 1 页，最旧在前
    await w.find('[data-testid="queue-sort"]').trigger('click')
    expect(w.find('[data-testid="queue-sort"]').text()).toContain('旧→新')
    expect(w.text()).toContain('模具冷却水路怎么排查-m3')
    expect(w.text()).not.toContain('模具冷却水路怎么排查-m1')
  })
})

describe('KbAdminDashboard「待你处理」chip — 区分「0 条」与「加载失败」', () => {
  it('差评复核加载失败 → 置顶「加载失败 · 数量未知」chip（而非整条消失）', () => {
    const pinia = activePinia(identity())
    const kb = useKb()
    kb.loadErrors.value['feedbackReview'] = '加载失败，请重试'          // feedbackReview 保持 null
    const w = mount(KbAdminDashboard, { global: { plugins: [pinia], stubs } })
    expect(w.text()).toContain('待你处理')
    expect(w.find('[data-testid="feedback-load-failed-chip"]').exists()).toBe(true)
    expect(w.text()).toContain('差评复核加载失败')
    expect(w.text()).toContain('数量未知')
    expect(w.text()).not.toContain('差评未处理')                       // 未知数量不冒充计数
  })

  it('成功且 0 条 → 置顶条整体不出现（真·无差评）', () => {
    const pinia = activePinia(identity())
    const kb = useKb()
    kb.feedbackReview.value = []
    const w = mount(KbAdminDashboard, { global: { plugins: [pinia], stubs } })
    expect(w.text()).not.toContain('待你处理')
    expect(w.find('[data-testid="feedback-load-failed-chip"]').exists()).toBe(false)
  })

  it('有未处理差评 → 计数 chip 照常（回归）', () => {
    const pinia = activePinia(identity())
    const kb = useKb()
    kb.feedbackReview.value = [fb('m1'), fb('m2', { handled: true, handled_status: 'RESOLVED' })]
    const w = mount(KbAdminDashboard, { global: { plugins: [pinia], stubs } })
    expect(w.text()).toContain('待你处理')
    expect(w.text()).toContain('差评未处理')
    expect(w.find('[data-testid="feedback-load-failed-chip"]').exists()).toBe(false)
  })
})


// ── B8（Sam 2026-08-04 选 c）：截断必须如实告知，且**不给**「加载更多」────────────
describe('FeedbackReviewList — 截断如实告知（B8）', () => {
  function mountWithTruncated(truncated: boolean) {
    const pinia = activePinia(identity())
    const kb = useKb()
    kb.feedbackReview.value = [fb('m1')]
    ;(kb as unknown as { feedbackReviewTruncated: { value: boolean } })
      .feedbackReviewTruncated.value = truncated
    return mount(FeedbackReviewList, { global: { plugins: [pinia], stubs } })
  }

  it('后端报截断 → 显示提示，且**不出现**「加载更多」', () => {
    const w = mountWithTruncated(true)
    expect(w.find('[data-testid="feedback-truncated"]').exists()).toBe(true)
    expect(w.text()).toContain('结果已截断')
    // ⚠️ 后端 offset 作用在**原始 join 行**上、与按 message_id 去重聚合后的条目不对齐
    // ⇒ 给「加载更多」会漏消息/重消息。这里的"没有按钮"是**刻意**的，不是遗漏。
    expect(w.text()).not.toContain('加载更多')
  })

  it('未截断 → 不显示提示（不制造无谓告警）', () => {
    const w = mountWithTruncated(false)
    expect(w.find('[data-testid="feedback-truncated"]').exists()).toBe(false)
  })
})
