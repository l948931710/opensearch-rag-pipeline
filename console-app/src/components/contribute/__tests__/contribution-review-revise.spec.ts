import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import ContributionReviewQueue from '@/components/contribute/ContributionReviewQueue.vue'
import { useContribute, __resetContribute, CONTRIB_DEPT_OPTS, type ContributionItem } from '@/composables/useContribute'
import { useDialog } from '@/composables/useDialog'

// 批次ε-1「贡献审核体验」回归守卫：
//   B1 全文展开（溢出才显控件、纯前端零请求）
//   B2 采纳前修订（只下发变更字段、空文本禁采纳、取消弃改、归属下拉按角色收敛、失败不丢稿）
//   B3 缺口溯源徽标（字段缺省自隐）
//   B4 待审队列 has_more（此前被静默丢弃 → 假满员）
const LONG_CONTENT = '标准周期为每生产 30 万模次做一级保养（清洁流道、检查顶针）。\n100 万模次做二级保养（拆模检查型腔磨损、更换密封件）。\n夏季高温连续生产时一级保养提前到 25 万模次。\n保养记录填在《模具保养台账》并由当班班长签字确认。'
const PENDING: ContributionItem = {
  contribution_id: 'c1', question: '模具保养周期是多久？', content: LONG_CONTENT, category_dept: 'production',
  author_id: 'u9', author_name: '王伟', review_status: 'pending', ingestion_status: 'none',
  state: 'pending', doc_id: null, review_note: '', created_at: '2026-06-29', reviewed_at: null,
}

beforeEach(() => { vi.restoreAllMocks(); __resetContribute() })
afterEach(() => {
  // 展开测试对 HTMLElement.prototype 的几何 stub 必须清理，避免污染同文件其它用例
  delete (HTMLElement.prototype as { scrollHeight?: number }).scrollHeight
  delete (HTMLElement.prototype as { clientHeight?: number }).clientHeight
})

function activate(over: Record<string, unknown> = {}) {
  const pinia = createTestingPinia({
    createSpy: vi.fn,
    initialState: { session: { identity: { userId: 'a', name: '管理员', role: 'dept_admin', aclGroups: ['production'], canManage: true, managedOwnerDepts: ['production'], ...over }, token: 't', ready: true } },
  })
  setActivePinia(pinia)
  return pinia
}

/** jsdom 无布局引擎：用原型 getter 冒充内容区几何（scrollH>clientH=溢出）。 */
function stubGeometry(scrollH: number, clientH: number) {
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get: () => scrollH })
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => clientH })
}

function recordFetch(responder?: (url: string) => { status?: number; json?: unknown }) {
  const calls: Array<{ url: string; body: Record<string, unknown> | null }> = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : null
    calls.push({ url: String(url), body })
    const r = responder?.(String(url)) || {}
    const json = r.json ?? { items: [], summary: null, has_more: false }
    const status = r.status ?? 200
    return { ok: status < 400, status, json: async () => json, text: async () => JSON.stringify(json) }
  }))
  return calls
}

async function mountQueue(items: ContributionItem[], over: Record<string, unknown> = {}) {
  const pinia = activate(over)
  ;(useContribute() as { pendingContribs: { value: ContributionItem[] } }).pendingContribs.value = items
  const w = mount(ContributionReviewQueue, { global: { plugins: [pinia] } })
  await flushPromises(); await nextTick()
  return w
}

describe('B1 全文展开', () => {
  it('内容溢出 → 显「展开全文」；点击移除 clamp、可收起；纯前端零请求', async () => {
    stubGeometry(120, 30)
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    const w = await mountQueue([PENDING])
    const btn = w.find('[data-testid="contrib-expand"]')
    expect(btn.exists()).toBe(true)
    expect(w.find('[data-testid="contrib-content"]').classes()).toContain('line-clamp-2')
    await btn.trigger('click')
    expect(w.find('[data-testid="contrib-content"]').classes()).not.toContain('line-clamp-2')
    expect(w.find('[data-testid="contrib-content"]').text()).toBe(LONG_CONTENT)
    expect(w.find('[data-testid="contrib-expand"]').text()).toBe('收起')
    await w.find('[data-testid="contrib-expand"]').trigger('click')
    expect(w.find('[data-testid="contrib-content"]').classes()).toContain('line-clamp-2')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('短内容不溢出 → 不显展开控件（零噪声）', async () => {
    stubGeometry(30, 30)
    const w = await mountQueue([{ ...PENDING, content: '短答案。' }])
    expect(w.find('[data-testid="contrib-expand"]').exists()).toBe(false)
  })
})

describe('B3 缺口溯源徽标', () => {
  it('带 gap_query → 显「来自缺口」且 tooltip=原提问；缺字段（老后端）→ 自隐', async () => {
    const w = await mountQueue([
      { ...PENDING, contribution_id: 'g1', gap_query: '保养周期原问', source_message_id: 'm1' },
      { ...PENDING, contribution_id: 'g2' },
    ])
    const badges = w.findAll('[data-testid="contrib-from-gap"]')
    expect(badges.length).toBe(1)
    expect(badges[0].attributes('title')).toBe('保养周期原问')
  })
})

describe('B2 采纳前修订', () => {
  it('预填原值；只下发实际变更的字段（未动的 question 不进请求体）', async () => {
    const calls = recordFetch()
    const w = await mountQueue([PENDING], { managedOwnerDepts: ['production', 'quality'] })
    await w.find('[data-testid="contrib-revise"]').trigger('click')
    expect((w.find('[data-testid="contrib-revise-q"]').element as HTMLInputElement).value).toBe(PENDING.question)
    expect((w.find('[data-testid="contrib-revise-c"]').element as HTMLTextAreaElement).value).toBe(LONG_CONTENT)
    await w.find('[data-testid="contrib-revise-c"]').setValue('修订后的答案正文。')
    await w.find('[data-testid="contrib-revise-dept"]').setValue('quality')
    expect(w.find('[data-testid="contrib-accept-revised"]').text()).toContain('采纳到')
    await w.find('[data-testid="contrib-accept-revised"]').trigger('click')
    await flushPromises()
    const accept = calls.find((x) => x.url.includes('/accept'))
    expect(accept, 'accept 请求必须发出').toBeTruthy()
    expect(accept!.body).toMatchObject({ permission_level: 'dept_internal', content: '修订后的答案正文。', category_dept: 'quality' })
    expect('question' in accept!.body!).toBe(false)
  })

  it('浏览态直接采纳（未修订）→ 请求体只有 permission_level（后端「缺省=保留原值」契约）', async () => {
    const calls = recordFetch()
    const w = await mountQueue([PENDING])
    const acceptBtn = w.findAll('button').find((b) => b.text() === '采纳')!
    await acceptBtn.trigger('click')
    await flushPromises()
    const accept = calls.find((x) => x.url.includes('/accept'))
    expect(Object.keys(accept!.body!)).toEqual(['permission_level'])
  })

  it('清空内容 → 采纳按钮禁用（后端 strip 后空文本会 400，前置拦截）', async () => {
    const w = await mountQueue([PENDING])
    await w.find('[data-testid="contrib-revise"]').trigger('click')
    await w.find('[data-testid="contrib-revise-c"]').setValue('   ')
    expect(w.find('[data-testid="contrib-accept-revised"]').attributes('disabled')).toBeDefined()
  })

  it('取消弃改：再次打开修订 → 字段还原为原值（不残留半改状态）', async () => {
    const w = await mountQueue([PENDING])
    await w.find('[data-testid="contrib-revise"]').trigger('click')
    await w.find('[data-testid="contrib-revise-q"]').setValue('被改坏的问题')
    await w.find('[data-testid="contrib-revise-cancel"]').trigger('click')
    expect(w.find('[data-testid="contrib-revise-q"]').exists()).toBe(false)
    await w.find('[data-testid="contrib-revise"]').trigger('click')
    expect((w.find('[data-testid="contrib-revise-q"]').element as HTMLInputElement).value).toBe(PENDING.question)
  })

  it('归属下拉按角色收敛：dept_admin=管辖∪原归属；kb_admin=全量（孤儿兜底终审者）', async () => {
    const w = await mountQueue([{ ...PENDING, category_dept: 'marketing' }], { managedOwnerDepts: ['production', 'quality'] })
    await w.find('[data-testid="contrib-revise"]').trigger('click')
    const ids = w.find('[data-testid="contrib-revise-dept"]').findAll('option').map((o) => o.attributes('value'))
    expect(new Set(ids)).toEqual(new Set(['production', 'quality', 'marketing']))
    __resetContribute()
    const w2 = await mountQueue([PENDING], { role: 'kb_admin', managedOwnerDepts: [] })
    await w2.find('[data-testid="contrib-revise"]').trigger('click')
    expect(w2.find('[data-testid="contrib-revise-dept"]').findAll('option').length).toBe(CONTRIB_DEPT_OPTS.length)
  })

  it('采纳失败（403）→ 走既有 notice(danger) 通道，且编辑态保留不丢稿', async () => {
    recordFetch((url) => (url.includes('/accept') ? { status: 403, json: { detail: '无权采纳到部门「quality」' } } : {}))
    const w = await mountQueue([PENDING], { managedOwnerDepts: ['production', 'quality'] })
    await w.find('[data-testid="contrib-revise"]').trigger('click')
    await w.find('[data-testid="contrib-revise-c"]').setValue('改过的内容')
    await w.find('[data-testid="contrib-accept-revised"]').trigger('click')
    await flushPromises()
    expect(useDialog().dialog.value.title).toBe('采纳失败')
    expect(w.find('[data-testid="contrib-revise-c"]').exists()).toBe(true)   // 不丢稿
    expect((w.find('[data-testid="contrib-revise-c"]').element as HTMLTextAreaElement).value).toBe('改过的内容')
  })

  it('P2-16 回归：requires_kb_admin_approval → 「已采纳，等待放行」提示不被修订流程吞掉', async () => {
    recordFetch((url) => (url.includes('/accept') ? { json: { requires_kb_admin_approval: true } } : {}))
    const w = await mountQueue([PENDING])
    const acceptBtn = w.findAll('button').find((b) => b.text() === '采纳')!
    await acceptBtn.trigger('click')
    await flushPromises()
    expect(useDialog().dialog.value.title).toBe('已采纳，等待放行')
  })

  it('处理中（isBusy）→ 修订入口同样禁用（与 采纳/驳回 同一把互斥锁）', async () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => { /* 永不 resolve */ })))
    const w = await mountQueue([PENDING])
    const acceptBtn = w.findAll('button').find((b) => b.text() === '采纳')!
    await acceptBtn.trigger('click')
    await nextTick()
    expect(w.find('[data-testid="contrib-revise"]').attributes('disabled')).toBeDefined()
  })
})

describe('B4 待审队列 has_more', () => {
  it('pendingHasMore → 计数徽标带 +、显「加载更多」；点击按当前条数发 offset', async () => {
    const calls = recordFetch()
    const w = await mountQueue([PENDING])
    ;(useContribute() as { pendingHasMore: { value: boolean } }).pendingHasMore.value = true
    await nextTick()
    expect(w.text()).toContain('1+')
    const more = w.find('[data-testid="contrib-load-more"]')
    expect(more.exists()).toBe(true)
    await more.trigger('click')
    await flushPromises()
    expect(calls.some((x) => x.url.includes('/api/kb/contributions/pending') && x.url.includes('offset=1'))).toBe(true)
  })

  it('无更多 → 不显加载更多（回归，避免过度展示）', async () => {
    const w = await mountQueue([PENDING])
    expect(w.find('[data-testid="contrib-load-more"]').exists()).toBe(false)
  })
})
