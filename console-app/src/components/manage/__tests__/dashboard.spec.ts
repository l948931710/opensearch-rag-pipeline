import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import KbAdminDashboard from '@/components/manage/KbAdminDashboard.vue'
import DeptDashboard from '@/components/manage/DeptDashboard.vue'
import StatusDistBar from '@/components/manage/StatusDistBar.vue'
import { useKb, __resetKb } from '@/composables/useKb'

// useKb 是模块级单例 store（非 pinia）——每例重置，避免跨例污染 kbStats/approvals。
beforeEach(() => { vi.restoreAllMocks(); __resetKb() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '张三', role: 'kb_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'], ...over }
}

// 先激活 pinia（useKb→useSession 需要）→ 注入 kbStats 测试态 → 再 mount（渲染即读到真实数）。
function mountWith(comp: any, id: Identity, stats?: any) {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 't', ready: true } } })
  setActivePinia(pinia)
  if (stats) (useKb() as any).kbStats.value = stats
  return mount(comp, { global: { plugins: [pinia] } })
}

describe('KbAdminDashboard — 全库真实口径，无造数', () => {
  it('用 kbStats 渲染全库资产卡 + 状态分布；未接入看板如实占位', () => {
    const w = mountWith(KbAdminDashboard, identity({ role: 'kb_admin' }),
      { total: 1796, active: 1478, retired: 318, by_badge: { 已上线: 1478, 已退役: 318, 处理中: 12 } })
    expect(w.text()).toContain('全库资产概览')
    expect(w.text()).toContain('1796')                 // 文档总数（真实）
    expect(w.text()).toContain('318')                  // 已退役（真实）
    expect(w.findComponent(StatusDistBar).exists()).toBe(true)
    expect(w.text()).toContain('看板建设中')             // 不造数：未接入指标如实占位
  })
})

describe('DeptDashboard — 本部门口径；修「待审核恒 0」误导', () => {
  it('待审核取 by_badge（我提交待放行），而非恒空的 approvals', () => {
    const w = mountWith(DeptDashboard, identity({ role: 'dept_admin', managedOwnerDepts: ['marketing'] }),
      { total: 42, active: 40, retired: 2, by_badge: { 已上线: 38, 待审核: 3, 处理中: 1 } })
    expect(w.text()).toContain('概览')
    expect(w.text()).toContain('42')                   // 本部门文档总数
    expect(w.text()).toContain('待审核')
    expect(w.text()).toContain('3')                    // 来自 by_badge['待审核']，非 0
    expect(w.text()).not.toContain('全库资产概览')       // 不是 kb_admin 看板
    expect(w.text()).toContain('看板建设中')
  })
})

describe('StatusDistBar', () => {
  it('无数据 → 空态；有数据 → 画分段', () => {
    const empty = mount(StatusDistBar, { props: { byBadge: {} } })
    expect(empty.text()).toContain('暂无文档数据')
    const filled = mount(StatusDistBar, { props: { byBadge: { 已上线: 10, 已退役: 5 } } })
    expect(filled.text()).toContain('已上线')
    expect(filled.text()).toContain('10')
    expect(filled.text()).not.toContain('暂无文档数据')
  })
})
