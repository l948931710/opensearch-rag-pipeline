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

function fb(id: string, over: Partial<FeedbackReviewItem> = {}): FeedbackReviewItem {
  return {
    message_id: id, question: `模具冷却水路怎么排查-${id}`, created_at: '2026-07-10 10:00',
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

  it('批次α-①：转人工/入库复审有未处理 → 各自计数 chip 出现（只计未关单）', () => {
    const pinia = activePinia(identity())
    const kb = useKb()
    kb.feedbackReview.value = []                                  // 差评清零：chip 条仍因另两队列出现
    kb.escalations.value = [
      { ticket_id: 'e1', message_id: 'm1', question: '首件检验做哪些项目？', ai_answer_excerpt: '',
        user_name: '王强', user_dept: '生产部', created_at: '2026-07-10 09:00', age_days: 4,
        status: 'PENDING', closed: false, expert_answer: '', assigned_user_name: '', docs: [] },
      { ticket_id: 'e2', message_id: 'm2', question: '已结单', ai_answer_excerpt: '',
        user_name: '李敏', user_dept: '外贸部', created_at: '2026-07-01 09:00', age_days: 13,
        status: 'RESOLVED', closed: true, expert_answer: 'done', assigned_user_name: '张', docs: [] },
    ] as never
    kb.reviewTasks.value = [
      { task_id: 't1', doc_id: 'D9', title: '员工薪酬发放办法', review_type: 'PERM_MISMATCH',
        review_reason: '实时权限比 LLM 建议更宽松', owner_dept: 'hr', suggested_permission_level: 'restricted',
        age_days: 9, status: 'PENDING', closed: false, reviewer_name: '' },
    ] as never
    const w = mount(KbAdminDashboard, { global: { plugins: [pinia], stubs } })
    expect(w.text()).toContain('待你处理')
    expect(w.text()).not.toContain('差评未处理')                   // 差评 0 → 差评 chip 不出现
    expect(w.find('[data-testid="escalation-open-chip"]').text()).toContain('1')   // 只计未关单
    expect(w.find('[data-testid="review-task-open-chip"]').text()).toContain('1')
  })
})
