import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import Sidebar from '@/components/shell/Sidebar.vue'
import { useAsk, __resetAsk } from '@/composables/useAsk'
import { useDialog } from '@/composables/useDialog'

/**
 * P2-13：删除会话是**不可逆**的（本地 splice + 落盘 + 向服务端 DELETE
 * /api/conversations/<id>），此前点垃圾桶即刻执行。侧栏列表密集、删除图标紧挨标题，
 * 误触无从挽回 ⇒ 必须走与「批量退役」同款的破坏性确认框。
 */
beforeEach(() => {
  vi.restoreAllMocks(); __resetAsk()
  setActivePinia(createTestingPinia({ createSpy: vi.fn, initialState: { session: { token: 't', ready: true } } }))
})

async function mountWithOneConv() {
  const api = useAsk()
  api.newConversation()
  const conv = api.conversations.value[0]
  conv.messages.push({ id: 'u1', role: 'user', text: '你好' } as never)
  conv.title = '年假问题'
  const w = mount(Sidebar, { global: { stubs: { RouterLink: true } } })
  await flushPromises()
  return { api, w, id: conv.id }
}

function delButton(w: ReturnType<typeof mount>) {
  return w.findAll('button').find((b) => (b.attributes('aria-label') || '').startsWith('删除会话'))
}

describe('侧栏删除会话：破坏性操作必须确认', () => {
  it('点删除 → 先弹确认框，未确认前**不删**', async () => {
    const { api, w, id } = await mountWithOneConv()
    const btn = delButton(w)
    expect(btn, '应能找到删除按钮').toBeTruthy()
    void btn!.trigger('click')
    await flushPromises()
    const { dialog } = useDialog()
    expect(dialog.value.open).toBe(true)
    expect(dialog.value.danger).toBe(true)
    expect(api.conversations.value.some((c) => c.id === id)).toBe(true)   // 还没删
  })

  it('确认 → 真正删除；取消 → 保留', async () => {
    const a = await mountWithOneConv()
    void delButton(a.w)!.trigger('click')
    await flushPromises()
    useDialog().onCancel()
    await flushPromises()
    expect(a.api.conversations.value.some((c) => c.id === a.id)).toBe(true)

    __resetAsk()
    const b = await mountWithOneConv()
    void delButton(b.w)!.trigger('click')
    await flushPromises()
    useDialog().onConfirm()
    await flushPromises()
    expect(b.api.conversations.value.some((c) => c.id === b.id)).toBe(false)
  })
})
