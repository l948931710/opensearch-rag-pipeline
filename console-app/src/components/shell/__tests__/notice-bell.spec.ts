import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import NoticeBell from '@/components/shell/NoticeBell.vue'

/**
 * 文档升版提醒的 **UI 静默红线**（Sam 2026-08-04「不想太多通知影响 UIUX」）。
 *
 * 这些不是样式偏好，是需求的一部分 —— 通知的价值在"需要时找得到"，不在"跳出来打断"。
 * 故逐条钉死：无未读零装饰 / 有未读只出数字 / 不自动展开 / 点击才拉取 / 点外收起。
 */
// mock 必须返回**真 ref**：Vue 模板只对真正的 ref 自动解包，
// 普通 { value } 对象在模板里会被当作对象本身（items.length 恒 undefined，
// 于是永远走「加载中…」分支——第一版就栽在这里）。
const h = vi.hoisted(() => ({
  items: null as any,
  unread: null as any,
  capped: null as any,
  loading: null as any,
  loadCalls: 0,
  readCalls: [] as any[],
}))

vi.mock('@/composables/useNotices', async () => {
  const { ref } = await import('vue')
  h.items = ref<any[]>([])
  h.unread = ref(0)
  h.capped = ref(false)
  h.loading = ref(false)
  return {
    useNotices: () => ({
      items: h.items,
      unreadCount: h.unread,
      unreadCapped: h.capped,
      loading: h.loading,
      loaded: ref(true),
      load: async () => { h.loadCalls += 1 },
      markRead: async (ids?: number[]) => { h.readCalls.push(ids ?? 'all') },
    }),
  }
})

beforeEach(() => {
  vi.restoreAllMocks()
  h.items.value = []
  h.unread.value = 0
  h.capped.value = false
  h.loading.value = false
  h.loadCalls = 0
  h.readCalls = []
  setActivePinia(createTestingPinia({ createSpy: vi.fn, initialState: { session: { token: 't', ready: true } } }))
})

function bellButton(w: ReturnType<typeof mount>) {
  return w.findAll('button').find((b) => (b.attributes('aria-label') || '').startsWith('通知'))!
}

describe('通知铃铛：静默红线', () => {
  it('无未读 = 零装饰（不出红点、不出数字角标）', async () => {
    const w = mount(NoticeBell)
    await flushPromises()
    // 红点与数字角标都不该存在——视觉上与"没有这个功能"一致
    expect(w.html()).not.toContain('bg-st-busy')
    expect(bellButton(w).attributes('aria-label')).toBe('通知')
  })

  it('有未读 = 出数字角标，且 99+ 封顶', async () => {
    h.unread.value = 3
    let w = mount(NoticeBell)
    await flushPromises()
    expect(w.text()).toContain('3')
    expect(bellButton(w).attributes('aria-label')).toContain('3 条未读')

    h.capped.value = true
    h.unread.value = 99
    w = mount(NoticeBell)
    await flushPromises()
    expect(w.text()).toContain('99+')
  })

  it('挂载后**不自动展开**面板，也不发任何弹出', async () => {
    h.unread.value = 5
    const w = mount(NoticeBell)
    await flushPromises()
    expect(w.find('[role="dialog"]').exists()).toBe(false)
  })

  it('点击才展开并现拉一次；再点收起', async () => {
    const w = mount(NoticeBell)
    await flushPromises()
    expect(h.loadCalls).toBe(0)

    await bellButton(w).trigger('click')
    await flushPromises()
    expect(w.find('[role="dialog"]').exists()).toBe(true)
    expect(h.loadCalls).toBe(1)

    await bellButton(w).trigger('click')
    await flushPromises()
    expect(w.find('[role="dialog"]').exists()).toBe(false)
  })

  it('空列表展示「暂无通知」而不是报错或空白', async () => {
    const w = mount(NoticeBell)
    await bellButton(w).trigger('click')
    await flushPromises()
    expect(w.text()).toContain('暂无通知')
  })

  it('列表渲染标题与版本；点条目标记该条已读，点「全部已读」标记全部', async () => {
    h.unread.value = 2
    h.items.value = [
      { id: 11, doc_id: 'D1', title: '作业指导书', version_no: 3, state: 'PENDING', created_at: '2026-08-04 07:30:00' },
      { id: 12, doc_id: 'D2', title: '安全规范', version_no: 2, state: 'READ', created_at: null },
    ]
    const w = mount(NoticeBell)
    await bellButton(w).trigger('click')
    await flushPromises()
    expect(w.text()).toContain('作业指导书')
    expect(w.text()).toContain('v3')

    const rows = w.findAll('li button')
    await rows[0].trigger('click')
    expect(h.readCalls).toEqual([[11]])

    const allBtn = w.findAll('button').find((b) => b.text() === '全部已读')!
    await allBtn.trigger('click')
    expect(h.readCalls).toEqual([[11], 'all'])
  })

  it('无未读时不渲染「全部已读」（没有未读就没有该动作）', async () => {
    const w = mount(NoticeBell)
    await bellButton(w).trigger('click')
    await flushPromises()
    expect(w.findAll('button').some((b) => b.text() === '全部已读')).toBe(false)
  })
})
