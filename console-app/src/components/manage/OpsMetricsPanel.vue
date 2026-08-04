<script setup lang="ts">
import { computed } from 'vue'
import { Activity, Coins, Gauge, RefreshCw, Users } from '@lucide/vue'
import { useKb } from '@/composables/useKb'
import { admissionReasonLabel, deptLabel } from '@/lib/kb'
import { SECTION, ZONE_HEAD, ZONE_TICK, SUBHEAD, GRID, SPLIT } from '@/lib/section'
import LoadError from './LoadError.vue'
import StatCard from './StatCard.vue'
import BarList from './BarList.vue'
import TrendChart, { type TrendSeries } from './TrendChart.vue'

// 运营指标 tab（批次γ D1-D3，kb_admin 专属）：LLM 用量/成本底座 + SLO 日趋势 + 限流准入。
// 口径纪律（审计 1c：同名指标两套算法）——本页所有比率来自 qa_daily_metrics（按北京日物化、
// 含 Agent 调用）；概览看板的「问答 API 成功率」是实时 30 天滑窗且【排除】Agent 行。两处数字
// 天然对不上，各卡就地标注口径，绝不让同名数字裸奔。
const { kbOpsMetrics, loadOpsMetrics, loadErrors } = useKb()

const m = computed(() => kbOpsMetrics.value)
const fmtN = (n?: number | null) => (n || 0).toLocaleString('en-US')
/** token 数缩写（3.4M / 861K）：明细 tooltip 有全量，卡面别十位数糊脸。 */
function fmtTok(n?: number | null): string {
  const v = n || 0
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M'
  if (v >= 1_000) return (v / 1_000).toFixed(0) + 'K'
  return String(v)
}
const ms2s = (ms?: number | null) => (ms ? (ms / 1000).toFixed(1) + 's' : '—')

// 分区视觉常量已抽到 @/lib/section（此前三个看板各写一遍）。

// ── D1 LLM 用量 ──
const llmErrRate = computed(() => {
  const x = m.value
  return x && x.llm_total_calls > 0 ? x.llm_error_calls / x.llm_total_calls : null
})
const llmDailySeries = computed<TrendSeries[]>(() => [{
  key: 'tok', label: 'token/日', color: 'var(--st-queue)',
  points: (m.value?.llm_daily || []).map((r) => ({ d: r.d, v: r.tokens_total })),
}])
const llmModelItems = computed(() => (m.value?.llm_by_model || []).map((r) => ({
  label: r.model,
  sub: `错误 ${r.error_calls} · 均延迟 ${fmtN(r.avg_latency_ms)}ms · token ${fmtTok(r.tokens_prompt + r.tokens_completion)}`,
  value: r.calls,
})))
const llmDeptItems = computed(() => (m.value?.llm_by_dept || []).map((r) => ({
  label: r.key === '未归集' ? '未归集' : deptLabel(r.key),
  sub: `token ${fmtTok(r.tokens_total)}`,
  value: r.calls,
})))
const llmCategoryItems = computed(() => (m.value?.llm_by_category || []).map((r) => ({
  label: r.key, sub: `token ${fmtTok(r.tokens_total)}`, value: r.calls,
})))

// ── D2 SLO 日趋势 ──
const sloSeries = computed<TrendSeries[]>(() => {
  const days = m.value?.slo_daily || []
  const pick = (f: (r: (typeof days)[number]) => number | null) => days.map((r) => ({ d: r.d, v: f(r) }))
  return [
    { key: 'ans', label: '答问率', color: 'var(--st-live)', points: pick((r) => r.answer_rate) },
    { key: 'nores', label: '无答率', color: 'var(--st-busy)', points: pick((r) => r.no_result_rate) },
    { key: 'err', label: '错误率', color: 'var(--st-fail)', points: pick((r) => r.error_rate) },
  ]
})
const breachDays = computed(() => (m.value?.slo_daily || []).filter((r) => r.slo_ok === false))
/** rollup 停摆哨兵（审计风险 3：available=true 分不清「没违约」和「rollup 没跑」）：
 *  最新一天距今 >2 天 → 亮黄提示。日期按 UTC 日粒度比较，避免时区半天误差。 */
const sloStaleDays = computed(() => {
  const days = m.value?.slo_daily || []
  if (!days.length) return null
  const last = Date.parse(days[days.length - 1].d + 'T00:00:00Z')
  if (isNaN(last)) return null
  return Math.floor((Date.now() - last) / 86400000)
})

// ── D3 限流准入 ──
const admissionSeries = computed<TrendSeries[]>(() => {
  const days = m.value?.admission_daily || []
  return [
    { key: 'adm', label: '准入', color: 'var(--st-live)', points: days.map((r) => ({ d: r.d, v: r.admitted })) },
    { key: 'rej', label: '拒绝', color: 'var(--st-fail)', points: days.map((r) => ({ d: r.d, v: r.rejected })) },
  ]
})
const admissionTotals = computed(() => {
  const days = m.value?.admission_daily || []
  return {
    admitted: days.reduce((s, r) => s + r.admitted, 0),
    rejected: days.reduce((s, r) => s + r.rejected, 0),
  }
})
const admissionReasonItems = computed(() => (m.value?.admission_reasons || []).map((r) => ({
  label: admissionReasonLabel(r.reason), sub: r.reason, value: r.count,
})))
</script>

<template>
  <div class="space-y-5">
    <div class="flex items-center justify-between gap-3">
      <p class="text-[12.5px] text-muted-foreground">
        近 {{ m?.window_days || 30 }} 天运营口径：SLO 按北京日物化（含 Agent 调用），与概览看板的实时滑窗快照非同一口径。
      </p>
      <button
        type="button" data-testid="ops-refresh" title="服务端聚合缓存约 60 秒，刷新拿到的可能是同一份"
        class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border px-3 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong"
        @click="loadOpsMetrics()"
      ><RefreshCw :size="13" :stroke-width="1.75" /> 刷新</button>
    </div>

    <LoadError :message="loadErrors['opsMetrics']" @retry="loadOpsMetrics()" />

    <!-- D1 LLM 用量（成本底座） -->
    <section :class="SECTION" data-testid="ops-llm">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>LLM 用量 · 成本底座</header>
      <template v-if="m && m.llm_available">
        <div :class="GRID" class="mb-3">
          <StatCard label="调用次数" :value="fmtN(m.llm_total_calls)" :icon="Activity" hint="全部模型调用（问答/分类/rerank）" />
          <StatCard
            label="token 消耗" :value="fmtTok(m.llm_tokens_prompt + m.llm_tokens_completion)" :icon="Coins" tone="text-st-queue"
            :hint="`输入 ${fmtTok(m.llm_tokens_prompt)} · 输出 ${fmtTok(m.llm_tokens_completion)}`"
            :sub-value="m.llm_cost_estimate == null ? '价表未配' : '¥' + m.llm_cost_estimate.toFixed(2)"
            :sub-label="m.llm_cost_estimate == null ? '成本待回算（不编造单价）' : '估算成本'"
          />
          <StatCard
            label="调用错误率" :value="llmErrRate == null ? '—' : (llmErrRate * 100).toFixed(1) + '%'" :icon="Gauge"
            :tone="llmErrRate != null && llmErrRate > 0.05 ? 'text-st-fail' : 'text-st-live'" :hint="`错误 ${fmtN(m.llm_error_calls)} 次`"
          />
          <StatCard label="调用延迟 p95" :value="ms2s(m.llm_p95_latency_ms)" :icon="Activity" :hint="`p50 ${ms2s(m.llm_p50_latency_ms)} · 模型调用粒度，非问答端到端`" />
        </div>
        <div :class="SPLIT">
          <div class="p-[15px]">
            <p :class="SUBHEAD">逐日 token 消耗</p>
            <TrendChart :series="llmDailySeries" fill-zero empty="窗口内无 LLM 调用记录。" />
          </div>
          <div class="p-[15px]">
            <p :class="SUBHEAD">按模型（调用次数）</p>
            <BarList :items="llmModelItems" unit=" 次" empty="窗口内无调用。" />
          </div>
        </div>
        <div :class="SPLIT" class="mt-3">
          <div class="p-[15px]">
            <p :class="SUBHEAD">按部门归集（成本口径 dept_group）</p>
            <BarList :items="llmDeptItems" unit=" 次" empty="暂无部门归集数据（dept_group 未落）。" />
          </div>
          <div class="p-[15px]">
            <p :class="SUBHEAD">按调用类别</p>
            <BarList :items="llmCategoryItems" unit=" 次" empty="暂无类别数据。" />
          </div>
        </div>
      </template>
      <p v-else-if="m && !m.llm_available" class="rounded-[14px] border border-dashed border-border bg-surface/60 p-5 text-[12.5px] text-muted-foreground">
        LLM 用量查询失败或账本表未建（llm_call_log，schema/023）——数量未知，非零调用。
      </p>
      <p v-else class="rounded-[14px] border border-dashed border-border bg-surface/60 p-5 text-[12.5px] text-muted-foreground">数据加载中…</p>
    </section>

    <!-- D2 SLO 日趋势 -->
    <section :class="SECTION" data-testid="ops-slo">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>SLO 日趋势 · 按北京日物化</header>
      <template v-if="m && m.slo_available">
        <div
          v-if="sloStaleDays != null && sloStaleDays > 2"
          class="mb-3 rounded-xl border border-st-warn/40 bg-st-warn/[0.06] px-4 py-2.5 text-[12.5px] text-st-warn" data-testid="ops-slo-stale"
        >
          最新数据是 {{ (m.slo_daily[m.slo_daily.length - 1] || {}).d }}（{{ sloStaleDays }} 天前）——qa_rollup 可能未调度或停摆，近几日 SLO 未知（非全绿）。
        </div>
        <template v-if="m.slo_daily.length">
          <div :class="GRID" class="mb-3">
            <StatCard label="SLO 违约天数" :value="m.slo_breach_days" :icon="Gauge"
              :tone="m.slo_breach_days ? 'text-st-fail' : 'text-st-live'" :hint="`近 ${m.window_days} 天内 slo_ok=false 的天数`"
              :box="m.slo_breach_days ? 'border-st-fail/45 bg-st-fail/[0.06]' : ''" />
            <StatCard label="最新答问率" :value="((m.slo_daily[m.slo_daily.length - 1].answer_rate ?? 0) * 100).toFixed(1) + '%'" :icon="Activity"
              :hint="`${m.slo_daily[m.slo_daily.length - 1].d} · 含 Agent 调用`" />
            <StatCard label="最新独立用户" :value="m.slo_daily[m.slo_daily.length - 1].distinct_users" :icon="Users" hint="当日去重提问人数" />
            <StatCard label="当日被限流" :value="m.slo_daily[m.slo_daily.length - 1].rejected_count ?? '—'" :icon="Gauge"
              hint="NULL=017 前历史行（未知）" />
          </div>
          <p :class="SUBHEAD">答问率 / 无答率 / 错误率（缺日=rollup 未跑，折线断开不臆造）</p>
          <TrendChart :series="sloSeries" percent empty="窗口内无物化数据。" />
          <div v-if="breachDays.length" class="mt-3 space-y-1" data-testid="ops-slo-breaches">
            <p :class="SUBHEAD">违约明细</p>
            <p v-for="b in breachDays" :key="b.d" class="font-mono text-[11.5px] text-st-fail">
              {{ b.d }} · {{ b.breaches.length ? b.breaches.join(' / ') : 'slo_ok=false（明细缺失）' }}
            </p>
          </div>
        </template>
        <p v-else class="rounded-[14px] border border-dashed border-border bg-surface/60 p-5 text-[12.5px] text-muted-foreground" data-testid="ops-slo-empty">
          qa_daily_metrics 无行——qa_rollup 从未跑过或窗口外；不是「全部达标」，是「未知」。
        </p>
      </template>
      <p v-else-if="m && !m.slo_available" class="rounded-[14px] border border-dashed border-border bg-surface/60 p-5 text-[12.5px] text-muted-foreground">
        SLO 查询失败（qa_daily_metrics）——趋势未知。
      </p>
      <p v-else class="rounded-[14px] border border-dashed border-border bg-surface/60 p-5 text-[12.5px] text-muted-foreground">数据加载中…</p>
    </section>

    <!-- D3 限流准入 -->
    <section :class="SECTION" data-testid="ops-admission">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>限流准入 · offered vs admitted</header>
      <template v-if="m && m.admission_available">
        <!-- 审计风险 4：区分两种「零」——有准入行+零拒绝=健康；整表空=统计未启用/未知 -->
        <template v-if="m.admission_daily.length">
          <div :class="SPLIT">
            <div class="p-[15px]">
              <p :class="SUBHEAD">逐日 准入 / 拒绝（被拒请求不进问答日志，此处是唯一落点）</p>
              <TrendChart :series="admissionSeries" fill-zero empty="窗口内无准入统计。" />
              <p class="mt-2 text-[11.5px] text-faint">
                近 {{ m.window_days }} 天准入 <b class="font-mono tabular-nums text-foreground">{{ fmtN(admissionTotals.admitted) }}</b> 次 ·
                拒绝 <b class="font-mono tabular-nums" :class="admissionTotals.rejected ? 'text-st-fail' : 'text-st-live'">{{ fmtN(admissionTotals.rejected) }}</b> 次{{ admissionTotals.rejected === 0 ? '（限流未触发，健康）' : '' }}
              </p>
            </div>
            <div class="p-[15px]">
              <p :class="SUBHEAD">拒绝原因（窗口合计）</p>
              <BarList :items="admissionReasonItems" unit=" 次" empty="窗口内零拒绝——没有请求被限流挡下。" />
            </div>
          </div>
        </template>
        <p v-else class="rounded-[14px] border border-dashed border-border bg-surface/60 p-5 text-[12.5px] text-muted-foreground" data-testid="ops-admission-empty">
          qa_admission_reject 无行——准入统计未启用或窗口外（与「零拒绝」不同：零拒绝会有 admitted 伪行）。
        </p>
      </template>
      <p v-else-if="m && !m.admission_available" class="rounded-[14px] border border-dashed border-border bg-surface/60 p-5 text-[12.5px] text-muted-foreground">
        准入统计查询失败（qa_admission_reject）——未知，非零拒绝。
      </p>
      <p v-else class="rounded-[14px] border border-dashed border-border bg-surface/60 p-5 text-[12.5px] text-muted-foreground">数据加载中…</p>
    </section>
  </div>
</template>
