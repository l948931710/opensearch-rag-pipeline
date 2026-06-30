import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { Database } from 'lucide-vue-next'
import StatCard from '@/components/manage/StatCard.vue'

// 回归守卫：概览指标卡的加载态。修复「stats 返回前闪 0」——loading=true 显骨架而非 0；
// loading 缺省/false 时照常显主值（含 pill/子值/说明）。
describe('StatCard — loading skeleton (防 0 闪烁)', () => {
  it('loading=true → 显骨架条，不渲染主值（不闪 0）', () => {
    const w = mount(StatCard, { props: { label: '文档总数', value: 0, icon: Database, loading: true } })
    expect(w.find('.animate-pulse').exists()).toBe(true)   // 骨架占位
    expect(w.text()).toContain('文档总数')                  // label 仍在
    expect(w.text()).toContain('加载中')                    // sr-only 无障碍标注
    expect(w.text()).not.toContain('0')                     // 关键：不闪 "0"
  })

  it('loading=true → pill / 子值 / 说明也不渲染（避免 "0" 子值泄漏）', () => {
    const w = mount(StatCard, {
      props: { label: '文档总数', value: 0, icon: Database, loading: true, subValue: '0', subLabel: '已索引分块', hint: '我管理范围内' },
    })
    expect(w.text()).not.toContain('已索引分块')
    expect(w.text()).not.toContain('我管理范围内')
  })

  it('loading=false → 渲染主值，无骨架', () => {
    const w = mount(StatCard, { props: { label: '文档总数', value: 42, icon: Database, loading: false } })
    expect(w.find('.animate-pulse').exists()).toBe(false)
    expect(w.text()).toContain('42')
  })

  it('loading 缺省 → 当作未加载完成的反面（照常显主值，向后兼容）', () => {
    const w = mount(StatCard, { props: { label: '已上线', value: 7, icon: Database } })
    expect(w.find('.animate-pulse').exists()).toBe(false)
    expect(w.text()).toContain('7')
  })
})
