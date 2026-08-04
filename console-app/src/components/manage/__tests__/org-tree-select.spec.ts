import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import OrgTreeSelect from '../OrgTreeSelect.vue'

/**
 * 折叠式组织树选择器（2026-08-03 改版）：收起态摘要/防呆、展开交互、
 * restrictToManaged 三态、disabled 全链、allowedRoots 归一化（经真 OrgTreePicker 渲染验证）。
 */

// 生产形状（org_sync 契约：中心=depth 1、无公司根行；选择颗粒度默认止于二级）
const NODES = [
  { dept_id: 2, parent_id: 1, name: '生产中心', depth: 1, staff_count: 80, direct_staff_count: 4 },
  { dept_id: 3, parent_id: 2, name: '注塑事业部', depth: 2, staff_count: 30, direct_staff_count: 8 },
  { dept_id: 4, parent_id: 1, name: '空壳部门', depth: 1, staff_count: 0, direct_staff_count: 0 },
]

let mockSnap: any
const fetchMock = vi.fn(async () => {
  if (mockSnap instanceof Error) throw mockSnap
  return mockSnap
})
vi.mock('@/composables/useOrgSnapshot', () => ({
  fetchOrgSnapshot: (...a: unknown[]) => fetchMock(...(a as [])),
  invalidateOrgSnapshot: () => {},
}))

function freshSnap(over: Record<string, unknown> = {}) {
  return { nodes: NODES, stale: false, synced_at: 'x', status: 'fresh', my_managed_node_roots: [2], ...over }
}

async function mountSel(props: Record<string, unknown> = {}) {
  const w = mount(OrgTreeSelect, {
    props: { modelValue: [], mode: 'visibility', ...props },
  })
  await nextTick(); await nextTick(); await new Promise((r) => setTimeout(r, 0)); await nextTick()
  return w
}

beforeEach(() => {
  mockSnap = freshSnap()
  fetchMock.mockClear()
})

describe('OrgTreeSelect — 收起/展开', () => {
  it('默认收起：树不可见；点击触发器展开且 aria-expanded 翻转；无障碍名带字段语境', async () => {
    const w = await mountSel()
    const trigger = w.get('button[aria-expanded]')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    // 同页两个选择器仅凭「已选 N 个节点」无法区分——无障碍名必须带字段名（ux-review 残留 2）
    expect(trigger.attributes('aria-label')).toContain('可见范围：')
    const w2 = await mountSel({ mode: 'owner' })
    expect(w2.get('button[aria-expanded]').attributes('aria-label')).toContain('归属部门：')
    expect(w.find('.op-tree').exists() && w.find('.op-tree').isVisible()).toBe(false)
    await trigger.trigger('click')
    await nextTick()
    expect(trigger.attributes('aria-expanded')).toBe('true')
    expect(w.find('.op-tree').isVisible()).toBe(true)
  })

  it('owner 模式：树内选中即收起并回填名称；收起态无「含下级」字样', async () => {
    const w = await mountSel({ mode: 'owner' })
    await w.get('button[aria-expanded]').trigger('click')
    await nextTick()
    await w.get('input[aria-label="注塑事业部，子树 30 人"]').trigger('change')
    expect(w.emitted('update:modelValue')?.at(-1)?.[0]).toEqual([{ dept_id: 3, subtree: true }])
    await w.setProps({ modelValue: [{ dept_id: 3, subtree: true }] })
    await nextTick()
    expect(w.get('button[aria-expanded]').attributes('aria-expanded')).toBe('false')
    expect(w.get('button[aria-expanded]').text()).toContain('注塑事业部')
    expect(w.get('button[aria-expanded]').text()).not.toContain('含下级')
  })

  it('visibility 模式：chips 显示节点名+勾法，× 移除；0 人 chip 标红；约 N 人可见收起态可见', async () => {
    const w = await mountSel({
      modelValue: [{ dept_id: 2, subtree: true }, { dept_id: 4, subtree: true }],
    })
    expect(w.text()).toContain('生产中心')
    expect(w.text()).toContain('含下级')
    expect(w.text()).toContain('约 80 人可见')
    const zeroChip = w.findAll('span').find((s) => s.classes().includes('text-destructive') && s.text().includes('空壳部门'))
    expect(zeroChip).toBeTruthy()
    await w.get('button[aria-label="移除 生产中心"]').trigger('click')
    expect(w.emitted('update:modelValue')?.at(-1)?.[0]).toEqual([{ dept_id: 4, subtree: true }])
  })

  it('直挂遗漏警告在收起态可见（勾了子部门、父有直挂）', async () => {
    const w = await mountSel({ modelValue: [{ dept_id: 3, subtree: true }] })
    expect(w.text()).toContain('「生产中心」直挂的 4 人不在可见范围内')
  })

  it('disabled：触发器/chip 删除均禁用', async () => {
    const w = await mountSel({ modelValue: [{ dept_id: 2, subtree: true }], disabled: true })
    expect(w.get('button[aria-expanded]').attributes('disabled')).toBeDefined()
    expect(w.get('button[aria-label="移除 生产中心"]').attributes('disabled')).toBeDefined()
    await w.get('button[aria-expanded]').trigger('click')
    expect(w.get('button[aria-expanded]').attributes('aria-expanded')).toBe('false')
  })
})

describe('OrgTreeSelect — 快照状态与管辖过滤', () => {
  it('restrictToManaged + fresh + roots=[] ⇒ 零授权占位（不渲染触发器）', async () => {
    mockSnap = freshSnap({ my_managed_node_roots: [] })
    const w = await mountSel({ mode: 'owner', restrictToManaged: true })
    expect(w.text()).toContain('暂无组织树管辖授权')
    expect(w.find('button[aria-expanded]').exists()).toBe(false)
  })

  it('roots=null（旧后端）⇒ 不过滤不占位，全树可用', async () => {
    mockSnap = freshSnap({ my_managed_node_roots: null })
    const w = await mountSel({ mode: 'owner', restrictToManaged: true })
    await w.get('button[aria-expanded]').trigger('click')
    await nextTick()
    expect(w.text()).toContain('生产中心')
  })

  it('unavailable ⇒ 「组织数据不可用」+ 重试真发请求；stale ⇒ 收起态 ⚠️ 提示', async () => {
    mockSnap = freshSnap({ status: 'unavailable', nodes: [] })
    const w = await mountSel()
    expect(w.text()).toContain('组织数据不可用')
    const before = fetchMock.mock.calls.length
    mockSnap = freshSnap()
    await w.get('button').trigger('click')          // 重试按钮
    await new Promise((r) => setTimeout(r, 0)); await nextTick()
    expect(fetchMock.mock.calls.length).toBeGreaterThan(before)   // 重试是真请求（失败态不落缓存）
    expect(w.find('button[aria-expanded]').exists()).toBe(true)

    mockSnap = freshSnap({ status: 'stale', stale: true })
    const w2 = await mountSel()
    expect(w2.text()).toContain('组织数据可能已过期')
  })

  it('restrict + roots=[2,3]（祖先+后代）⇒ 归一化后仅生产中心为顶层、注塑只出现一次', async () => {
    mockSnap = freshSnap({ my_managed_node_roots: [2, 3] })
    const w = await mountSel({ mode: 'owner', restrictToManaged: true })
    await w.get('button[aria-expanded]').trigger('click')
    await nextTick()
    expect(w.text()).not.toContain('空壳部门')      // 未授权中心被过滤
    expect(w.findAll('input[aria-label="注塑事业部，子树 30 人"]')).toHaveLength(1)
  })

  it('restrict + 根全不在快照 ⇒ 展开态显式报「管辖节点不在组织快照中」', async () => {
    mockSnap = freshSnap({ my_managed_node_roots: [99] })
    const w = await mountSel({ mode: 'owner', restrictToManaged: true })
    await w.get('button[aria-expanded]').trigger('click')
    await nextTick()
    expect(w.text()).toContain('管辖节点不在组织快照中')
  })
})
