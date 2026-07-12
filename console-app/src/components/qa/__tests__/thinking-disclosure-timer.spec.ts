import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ThinkingDisclosure from '@/components/qa/ThinkingDisclosure.vue'
import type { ChatMessage } from '@/composables/useAsk'

// 回归守卫：思考中实时计时——整秒安静地走（<1s 不显）、答案开始即停表并换 reasoningMs
// 精确记录、卸载清定时器。时钟与 useAsk 同源（_reasoningT0=performance.now）。

function msg(extra: Record<string, unknown>): ChatMessage {
  return { id: 'a1', role: 'ai', sourcesOpen: false, voted: '', viewBlocks: null,
           ...extra } as ChatMessage
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'performance', 'Date'] })
})
afterEach(() => vi.useRealTimers())

describe('ThinkingDisclosure — 思考中实时计时', () => {
  it('未满 1s 不显秒数；随 interval 每秒推进', async () => {
    const t0 = performance.now()
    const w = mount(ThinkingDisclosure, {
      props: { message: msg({ streaming: true, reasoning: '想…', reasoningHtml: '<p>想…</p>',
                              _reasoningT0: t0, html: null }) },
    })
    expect(w.text()).toContain('正在思考')
    expect(w.text()).not.toMatch(/\d+s/)          // 0s 阶段留白，不显「0s」
    await vi.advanceTimersByTimeAsync(3000)
    expect(w.text()).toContain('· 3s')
    await vi.advanceTimersByTimeAsync(44000)
    expect(w.text()).toContain('· 47s')
    w.unmount()                                    // 卸载不留定时器（fake timers 下泄漏会让后续用例误走）
  })

  it('挂载时已思考多秒（重挂载/切会话回来）→ 立即显示绝对差，不从 0 重数', () => {
    const w = mount(ThinkingDisclosure, {
      props: { message: msg({ streaming: true, reasoning: '想…', reasoningHtml: '<p>想…</p>',
                              _reasoningT0: performance.now() - 12000, html: null }) },
    })
    expect(w.text()).toContain('· 12s')
    w.unmount()
  })

  it('答案开始（html 落定）→ 停表收起，显示 reasoningMs 精确耗时而非实时秒', () => {
    const w = mount(ThinkingDisclosure, {
      props: { message: msg({ reasoning: '想…', reasoningHtml: '<p>想…</p>',
                              reasoningMs: 4830, html: '<p>答案</p>' }) },
    })
    expect(w.text()).not.toContain('正在思考')
    expect(w.text()).toContain('思考过程')
    expect(w.text()).toContain('4.8s')
    w.unmount()
  })

  it('缺 _reasoningT0（异常/降级）→ 只显「正在思考」，不显坏数字', async () => {
    const w = mount(ThinkingDisclosure, {
      props: { message: msg({ streaming: true, reasoning: '想…', reasoningHtml: '<p>想…</p>',
                              html: null }) },
    })
    await vi.advanceTimersByTimeAsync(5000)
    expect(w.text()).toContain('正在思考')
    expect(w.text()).not.toMatch(/\d+s/)
    w.unmount()
  })
})
