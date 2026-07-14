import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import OpsMetricsPanel from '@/components/manage/OpsMetricsPanel.vue'
import TrendChart from '@/components/manage/TrendChart.vue'
import { useKb, __resetKb, type KbOpsMetrics } from '@/composables/useKb'

// 批次γ D1-D3：运营指标 tab。契约固化：
//  · loadOpsMetrics 单门（非 kb_admin 零网络）；5xx → loadErrors['opsMetrics'] 可重试
//  · SLO 停摆哨兵：最新一天距今 >2 天 → 亮黄（available=true ≠ rollup 在跑）
//  · admission 双零语义：有行+零拒绝=健康文案；整表空=「统计未启用」（两种零不同文案）
//  · TrendChart：比率图缺日断线不臆造 0；fillZero 计数图补 0 连线
//  · cost_estimate NULL → 「价表未配」，绝不显 ¥0

beforeEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); __resetKb() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '管理员', role: 'kb_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'], ...over }
}
function activePinia(id: Identity) {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 'TKN', ready: true } } })
  setActivePinia(pinia)
  return pinia
}
function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}

/** 今天的 UTC 日串（哨兵测试要「新鲜」数据用）。 */
function today(offsetDays = 0): string {
  return new Date(Date.now() + offsetDays * 86400000).toISOString().slice(0, 10)
}

function opsFixture(over: Partial<KbOpsMetrics> = {}): KbOpsMetrics {
  return {
    window_days: 30,
    llm_available: true, llm_total_calls: 100, llm_error_calls: 2,
    llm_tokens_prompt: 1_200_000, llm_tokens_completion: 300_000, llm_cost_estimate: null,
    llm_p50_latency_ms: 900, llm_p95_latency_ms: 4200,
    llm_by_model: [{ model: 'qwen3.7-plus', calls: 100, error_calls: 2, tokens_prompt: 1_200_000, tokens_completion: 300_000, avg_latency_ms: 1100 }],
    llm_by_category: [{ key: 'default', calls: 100, tokens_total: 1_500_000 }],
    llm_by_dept: [{ key: 'marketing', calls: 60, tokens_total: 900_000 }, { key: '未归集', calls: 40, tokens_total: 600_000 }],
    llm_daily: [{ d: today(-1), calls: 50, tokens_total: 700_000 }, { d: today(), calls: 50, tokens_total: 800_000 }],
    slo_available: true,
    slo_daily: [
      { d: today(-2), total: 100, answer_rate: 0.9, no_result_rate: 0.05, error_rate: 0.01, p95_latency_ms: 50_000, distinct_users: 40, slo_ok: true, breaches: [], rejected_count: 0 },
      { d: today(), total: 90, answer_rate: 0.7, no_result_rate: 0.2, error_rate: 0.06, p95_latency_ms: 60_000, distinct_users: 35, slo_ok: false, breaches: ['answer_rate_min'], rejected_count: 4 },
    ],
    slo_breach_days: 1,
    admission_available: true,
    admission_daily: [{ d: today(-1), admitted: 100, rejected: 0 }, { d: today(), admitted: 90, rejected: 0 }],
    admission_reasons: [],
    ...over,
  }
}

function mountPanel(m: KbOpsMetrics | null) {
  const pinia = activePinia(identity())
  const kb = useKb()
  kb.kbOpsMetrics.value = m
  return mount(OpsMetricsPanel, { global: { plugins: [pinia] } })
}

describe('useKb.loadOpsMetrics — 单门加载', () => {
  it('非 kb_admin → 零网络、置 null（dept_admin 无入口也无请求）', async () => {
    activePinia(identity({ role: 'dept_admin' }))
    const spy = vi.fn()
    vi.stubGlobal('fetch', spy)
    const kb = useKb()
    await kb.loadOpsMetrics()
    expect(spy).not.toHaveBeenCalled()
    expect(kb.kbOpsMetrics.value).toBeNull()
  })

  it('kb_admin 200 → 数据落地；5xx → loadErrors.opsMetrics 置错（LoadError 可重试）', async () => {
    activePinia(identity())
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp(opsFixture())))
    const kb = useKb()
    await kb.loadOpsMetrics()
    expect(kb.kbOpsMetrics.value?.llm_total_calls).toBe(100)
    expect(kb.loadErrors.value['opsMetrics']).toBeUndefined()

    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ detail: 'boom' }, { ok: false, status: 500 })))
    await kb.loadOpsMetrics()
    expect(kb.loadErrors.value['opsMetrics']).toBeTruthy()
  })
})

describe('OpsMetricsPanel — 诚实态', () => {
  it('满数据 → 三分区渲染；cost NULL 显「价表未配」绝不显 ¥0；违约明细列出 slo 名', () => {
    const w = mountPanel(opsFixture())
    expect(w.find('[data-testid="ops-llm"]').text()).toContain('调用次数')
    expect(w.text()).toContain('价表未配')
    expect(w.text()).not.toContain('¥0')
    expect(w.find('[data-testid="ops-slo-breaches"]').text()).toContain('answer_rate_min')
    expect(w.text()).toContain('限流未触发，健康')      // 有行+零拒绝=健康文案
  })

  it('SLO 停摆哨兵：最新一天距今 >2 天 → 亮黄提示 rollup 可能停摆', () => {
    const stale = opsFixture({
      slo_daily: [{ d: '2026-06-30', total: 10, answer_rate: 0.9, no_result_rate: 0.05, error_rate: 0.01, p95_latency_ms: 1, distinct_users: 5, slo_ok: true, breaches: [], rejected_count: 0 }],
      slo_breach_days: 0,
    })
    const w = mountPanel(stale)
    expect(w.find('[data-testid="ops-slo-stale"]').exists()).toBe(true)
    expect(w.find('[data-testid="ops-slo-stale"]').text()).toContain('qa_rollup')
    // 新鲜数据（今天有行）→ 哨兵不亮
    const fresh = mountPanel(opsFixture())
    expect(fresh.find('[data-testid="ops-slo-stale"]').exists()).toBe(false)
  })

  it('admission 双零语义：整表空 → 「统计未启用」而非「零拒绝」；slo 无行 → 「未知非全绿」', () => {
    const w = mountPanel(opsFixture({ admission_daily: [], admission_reasons: [], slo_daily: [], slo_breach_days: 0 }))
    expect(w.find('[data-testid="ops-admission-empty"]').text()).toContain('未启用')
    expect(w.find('[data-testid="ops-slo-empty"]').text()).toContain('未知')
    expect(w.text()).not.toContain('限流未触发，健康')
  })

  it('某块 available=false → 「查询失败/未知」文案，绝不 all-zeros 伪装健康', () => {
    const w = mountPanel(opsFixture({ llm_available: false, slo_available: false, admission_available: false }))
    expect(w.find('[data-testid="ops-llm"]').text()).toContain('数量未知')
    expect(w.find('[data-testid="ops-slo"]').text()).toContain('趋势未知')
    expect(w.find('[data-testid="ops-admission"]').text()).toContain('非零拒绝')
    expect(w.text()).not.toContain('调用次数')            // 失败态不渲染大数卡
  })
})

describe('TrendChart — 缺日语义', () => {
  it('比率图（无 fillZero）：缺日断线成两段；fillZero 计数图补 0 连成一段', () => {
    const series = [{
      key: 'a', label: 'A', color: 'red',
      points: [{ d: '2026-07-01', v: 0.9 }, { d: '2026-07-03', v: 0.8 }],   // 缺 07-02
    }]
    const rate = mount(TrendChart, { props: { series, percent: true } })
    expect(rate.findAll('polyline').length).toBe(2)      // 两段（断开）
    const count = mount(TrendChart, { props: { series, fillZero: true } })
    expect(count.findAll('polyline').length).toBe(1)     // 补 0 连成一段
    expect(count.text()).toContain('07-01')              // tick 渲染
  })

  it('空序列 → 空态文案', () => {
    const w = mount(TrendChart, { props: { series: [{ key: 'a', label: 'A', color: 'red', points: [] }], empty: '暂无' } })
    expect(w.text()).toContain('暂无')
  })
})
