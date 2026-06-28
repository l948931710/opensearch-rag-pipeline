import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import KbAdminDashboard from '@/components/manage/KbAdminDashboard.vue'
import DeptDashboard from '@/components/manage/DeptDashboard.vue'
import StatusDistBar from '@/components/manage/StatusDistBar.vue'
import BarList from '@/components/manage/BarList.vue'
import { useKb, __resetKb, type KbInsights, type KbGovernance } from '@/composables/useKb'

// useKb 是模块级单例 store（非 pinia）——每例重置，避免跨例污染 kbStats/insights/governance。
beforeEach(() => { vi.restoreAllMocks(); __resetKb() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '张三', role: 'kb_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'], ...over }
}

// 先激活 pinia（useKb→useSession 需要）→ 注入测试态 → 再 mount（渲染即读到真实数）。
function mountWith(comp: any, id: Identity, inject?: { stats?: any; insights?: KbInsights; gov?: KbGovernance }) {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 't', ready: true } } })
  setActivePinia(pinia)
  const kb = useKb() as any
  if (inject?.stats) kb.kbStats.value = inject.stats
  if (inject?.insights) kb.kbInsights.value = inject.insights
  if (inject?.gov) kb.kbGovernance.value = inject.gov
  return mount(comp, { global: { plugins: [pinia] } })
}

const INSIGHTS: KbInsights = {
  scope: 'global', window_days: 30,
  questions: 186, askers: 40, success: 143, refusal: 43, cited: 130, effective_rate: 0.769,
  top_docs: [{ title: '下达销售订单作业指导书', owner_dept: 'marketing', hits: 64 }],
  gap_queries: [{ query: '2ozpp杯在龙盛机上的速度', count: 2, avg_top: 0.729 }],
}
const GOV: KbGovernance = {
  window_days: 30, docs_active: 1618, docs_in_index: 1475, dual_version_docs: 0,
  avg_latency_ms: 14035, p50_latency_ms: 8106, p95_latency_ms: 54994, avg_retrieval_ms: 1538, avg_llm_ms: 12428,
  embed_runs: [{ bizdate: '2026-06-23', embedded: 117, failed: 0, fail_rate: 0 }],
  pii_redacted_docs: 475, pii_quarantined_docs: 3,
  answer_total: 902, answer_success: 790, answer_refusal: 112, answer_no_result: 15, answer_error: 25,
  effective_rate: 0.876,
  feedback_up: 64, feedback_down: 44, feedback_total: 108, helpful_rate: 0.593, escalations: 19,
  dept_coverage: [{ owner_dept: 'production', docs: 800, qa_hits: 980 }, { owner_dept: 'it', docs: 36, qa_hits: 2640 }],
}

describe('KbAdminDashboard — 全库真实口径，无造数', () => {
  it('治理数据未到 → 资产卡渲染 + 如实「加载中」占位（不造数）', () => {
    const w = mountWith(KbAdminDashboard, identity({ role: 'kb_admin' }),
      { stats: { total: 1796, active: 1478, retired: 318, by_badge: { 已上线: 1478, 已退役: 318, 处理中: 12 } } })
    expect(w.text()).toContain('全库资产概览')
    expect(w.text()).toContain('1796')
    expect(w.text()).toContain('318')
    expect(w.findComponent(StatusDistBar).exists()).toBe(true)
    expect(w.text()).toContain('数据加载中')          // 无 governance/insights → 诚实占位
    expect(w.text()).not.toContain('已索引文档')       // 真实健康卡未渲染
    expect(w.text()).not.toContain('近期入库批次')
  })

  it('治理 + 洞察就绪 → 渲染运行健康/治理风险/部门覆盖/知识效果真实数', () => {
    const w = mountWith(KbAdminDashboard, identity({ role: 'kb_admin' }),
      { stats: { total: 1618, active: 1618, retired: 0, by_badge: { 已上线: 1475 } }, gov: GOV, insights: INSIGHTS })
    expect(w.text()).toContain('运行健康')
    expect(w.text()).toContain('1475')               // 已索引文档
    expect(w.text()).toContain('55.0s')              // p95 端到端延迟（54994ms）
    expect(w.text()).toContain('治理风险')
    expect(w.text()).toContain('475')                // PII 已脱敏文档
    expect(w.text()).toContain('19')                 // 转人工
    expect(w.text()).toContain('59.3%')              // 好评率 helpful_rate
    expect(w.text()).toContain('部门覆盖 / 使用失衡')
    expect(w.text()).toContain('知识效果')
    expect(w.text()).toContain('下达销售订单作业指导书')  // 最常被使用（insights.top_docs）
    expect(w.text()).not.toContain('数据加载中')      // 已就绪 → 不再占位
  })

  it('治理缺失但洞察就绪 → 效果卡隐藏（不显伪造「0 赞 / 0 反馈」），仅列表渲染', () => {
    const w = mountWith(KbAdminDashboard, identity({ role: 'kb_admin' }),
      { stats: { total: 1, active: 1, retired: 0, by_badge: {} }, insights: INSIGHTS })  // gov 缺失
    expect(w.text()).toContain('知识效果')
    expect(w.text()).toContain('下达销售订单作业指导书')   // insights 列表照常
    expect(w.text()).not.toContain('好评率')             // governance 效果卡不渲染
    expect(w.text()).not.toContain('0 赞')               // 关键：不显伪造零反馈
  })
})

describe('DeptDashboard — 本部门口径', () => {
  it('待审核取 by_badge；洞察未到 → 如实占位', () => {
    const w = mountWith(DeptDashboard, identity({ role: 'dept_admin', managedOwnerDepts: ['marketing'] }),
      { stats: { total: 42, active: 40, retired: 2, by_badge: { 已上线: 38, 待审核: 3, 处理中: 1 } } })
    expect(w.text()).toContain('概览')
    expect(w.text()).toContain('42')
    expect(w.text()).toContain('待审核')
    expect(w.text()).toContain('3')
    expect(w.text()).not.toContain('全库资产概览')
    expect(w.text()).toContain('数据加载中')          // 无 insights → 诚实占位
  })

  it('洞察就绪 → 使用成效卡 + 最常被检索 + 知识缺口真实数', () => {
    const w = mountWith(DeptDashboard, identity({ role: 'dept_admin', managedOwnerDepts: ['marketing'] }),
      { stats: { total: 42, active: 40, retired: 2, by_badge: { 已上线: 38 } }, insights: INSIGHTS })
    expect(w.text()).toContain('使用成效')
    expect(w.text()).toContain('186')                // 被提问数
    expect(w.text()).toContain('76.9%')              // 有效回答率
    expect(w.text()).toContain('下达销售订单作业指导书')  // 最常被检索
    expect(w.text()).toContain('知识缺口')
    expect(w.text()).toContain('2ozpp杯在龙盛机上的速度')  // 未答好的提问
    expect(w.text()).not.toContain('数据加载中')
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

describe('BarList', () => {
  it('空 → empty 文案；有数据 → 标签 + 值 + k 缩写', () => {
    const empty = mount(BarList, { props: { items: [], empty: '暂无记录。' } })
    expect(empty.text()).toContain('暂无记录。')
    const filled = mount(BarList, { props: { items: [{ label: '员工手册', sub: '人力资源', value: 2640 }], unit: ' 次' } })
    expect(filled.text()).toContain('员工手册')
    expect(filled.text()).toContain('人力资源')
    expect(filled.text()).toContain('2.6k')          // ≥1000 → k 缩写
  })
})
