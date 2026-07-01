import { mount } from '@vue/test-utils'
import { h, ref, nextTick } from 'vue'
import { describe, it, expect, vi } from 'vitest'
import ErrorBoundary from '../ErrorBoundary.vue'

describe('ErrorBoundary', () => {
  it('正常子组件照常渲染，无兜底', () => {
    const Ok = { render: () => h('div', 'ok-content') }
    const wrapper = mount(ErrorBoundary, { slots: { default: () => h(Ok) } })
    expect(wrapper.text()).toContain('ok-content')
    expect(wrapper.text()).not.toContain('这个页面出了点问题')
  })

  it('子组件渲染抛错 → 显示兜底(含错误细节)而非抛穿(整页白屏)', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const boom = ref(false)
    const Child = { render() { if (boom.value) throw new Error('boom-xyz'); return h('div', 'fine') } }
    const wrapper = mount(ErrorBoundary, { slots: { default: () => h(Child) } })
    expect(wrapper.text()).toContain('fine')
    boom.value = true
    await nextTick()
    expect(wrapper.text()).toContain('这个页面出了点问题')
    expect(wrapper.text()).toContain('boom-xyz')   // 错误细节透出，便于定位根因
  })

  it('resetSignal 变化后清错、重新渲染子组件(切路由/会话自愈)', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const boom = ref(false)
    const Child = { render() { if (boom.value) throw new Error('e'); return h('div', 'recovered') } }
    const wrapper = mount(ErrorBoundary, { props: { resetSignal: 'a' }, slots: { default: () => h(Child) } })
    expect(wrapper.text()).toContain('recovered')
    boom.value = true
    await nextTick()
    expect(wrapper.text()).toContain('这个页面出了点问题')
    boom.value = false                            // 底层数据修好
    await wrapper.setProps({ resetSignal: 'b' })  // 切信号 → 清错重渲
    await nextTick()
    expect(wrapper.text()).toContain('recovered')
  })
})
