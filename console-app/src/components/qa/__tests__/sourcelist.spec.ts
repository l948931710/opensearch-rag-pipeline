import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import SourceList from '@/components/qa/SourceList.vue'
import type { SourceRow } from '@/composables/useAsk'

// 来源 chips：长引文溢出修复（章节段 min-w-0+truncate）+ 多来源原位折叠（非菜单）。

function src(i: number, over: Partial<SourceRow> = {}): SourceRow {
  return {
    idx: i, title: `文档${i}.docx`, section: `第${i}节`, levelLabel: '高',
    level: 'high', score: 8, relevance: 0.9, preview: '片段…', ...over,
  }
}
const LONG = '第六条 婚假、产假、年休假需提前一周申请，凡请、休假必须事先合理安排所属岗位的工作衔接。'

describe('SourceList — 溢出与折叠', () => {
  it('章节段可收缩可截断（min-w-0+truncate，不再 shrink-0）+ title 悬停补全', () => {
    const w = mount(SourceList, { props: { sources: [src(1, { section: LONG })] } })
    const sec = w.find(`span[title="${LONG}"]`)
    expect(sec.exists()).toBe(true)
    expect(sec.classes()).toContain('truncate')
    expect(sec.classes()).toContain('min-w-0')
    expect(sec.classes()).not.toContain('shrink-0')
    // chip 本体也可被父级收缩（min-width:auto 不再压过 max-w）
    expect(w.find('[data-testid="citation"]').classes()).toContain('min-w-0')
  })

  it('≤4 条全铺、无折叠开关', () => {
    const w = mount(SourceList, { props: { sources: [1, 2, 3, 4].map((i) => src(i)) } })
    expect(w.findAll('[data-testid="citation"]').length).toBe(4)
    expect(w.find('[data-testid="src-expand"]').exists()).toBe(false)
  })

  it('>4 条折叠为前 3 + 「+N 篇」；原位展开、可收起', async () => {
    const w = mount(SourceList, { props: { sources: [1, 2, 3, 4, 5, 6, 7].map((i) => src(i)) } })
    expect(w.findAll('[data-testid="citation"]').length).toBe(3)
    const expand = w.find('[data-testid="src-expand"]')
    expect(expand.text()).toContain('+4 篇')
    await expand.trigger('click')
    expect(w.findAll('[data-testid="citation"]').length).toBe(7)
    expect(w.find('[data-testid="src-expand"]').exists()).toBe(false)
    await w.find('[data-testid="src-collapse"]').trigger('click')
    expect(w.findAll('[data-testid="citation"]').length).toBe(3)
  })

  it('收起时挂在被折叠 chip 上的详情卡一并关闭；挂在前排的保留', async () => {
    const w = mount(SourceList, { props: { sources: [1, 2, 3, 4, 5, 6].map((i) => src(i)) } })
    await w.find('[data-testid="src-expand"]').trigger('click')
    await w.findAll('[data-testid="citation"]')[4].trigger('click')      // 展开第 5 条详情
    expect(w.text()).toContain('相关度')
    await w.find('[data-testid="src-collapse"]').trigger('click')
    expect(w.text()).not.toContain('相关度')                              // 孤儿卡不残留
    await w.findAll('[data-testid="citation"]')[0].trigger('click')      // 前排详情正常
    expect(w.text()).toContain('相关度')
  })
})
