<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ChevronRight } from '@lucide/vue'
import { deptLabel } from '@/lib/kb'
import type { KbDeptCoverage } from '@/composables/useKb'
import { fetchOrgSnapshot, type OrgSnapshot } from '@/composables/useOrgSnapshot'
import { resolveOwnerBucket, rollupCoverageRows, type CoverageTreeRow } from '@/lib/orgTree'

/**
 * 组织覆盖树表（2026-08-03 看板重设计签名区,codex v3 共识）：合并原「各部门文档数
 * ColumnChart + 各部门使用量 ColumnChart + DeptTable」三件套——同一行直读「覆盖多≠用得多」。
 *  · 中心行 = node 桶沿父链卷到 depth<=1 中心;只求和文档类可加指标;
 *    使用量/无答案率等**桶内提问去重**指标恒 null 显「—」（跨桶相加会重复计数,绝不造数）。
 *  · legacy 组码 / unknown / 快照缺链 node = 顶层独立行;快照 unavailable ⇒ 平铺不装树。
 *  · 7/30 使用量切换（δ-2 能力迁移,testid 契约原样保留）;无答案率恒 30 天口径（列头标注）。
 */
const props = defineProps<{ rows: KbDeptCoverage[] }>()

const snap = ref<OrgSnapshot | null>(null)
onMounted(async () => { try { snap.value = await fetchOrgSnapshot() } catch { /* 平铺回退 */ } })
const byId = computed(() => new Map((snap.value?.nodes ?? []).map((n) => [n.dept_id, n])))
const treeUsable = computed(() =>
  !!snap.value && snap.value.status !== 'unavailable' && byId.value.size > 0)

const tree = computed<CoverageTreeRow[]>(() => {
  const rows = props.rows ?? []
  if (treeUsable.value) return rollupCoverageRows(rows, byId.value, deptLabel)
  // 快照不可用：平铺（名称仍走 kind 契约,owner_label 兜底）
  return rows.map((r) => {
    const res = resolveOwnerBucket(r.owner_dept, r.owner_label, byId.value, deptLabel)
    return {
      key: r.owner_dept, label: res.label, kind: res.kind,
      docs: r.docs, new_month: r.new_month, pii_docs: r.pii_docs, wow_net: r.wow_net ?? null,
      qa_hits: r.qa_hits, qa_hits_7d: r.qa_hits_7d ?? null,
      qa_wow_net: r.qa_wow_net ?? null, qa_wow: r.qa_wow ?? null,
      no_answer_rate: r.no_answer_rate, children: [],
    } as CoverageTreeRow
  }).sort((a, b) => b.docs - a.docs)
})

// 展开态（中心行）
const openSet = ref<Set<string>>(new Set())
function toggleOpen(k: string) {
  const s = new Set(openSet.value); s.has(k) ? s.delete(k) : s.add(k); openSet.value = s
}
const visibleRows = computed(() => {
  const out: (CoverageTreeRow & { depth: number })[] = []
  for (const r of tree.value) {
    out.push(Object.assign({ depth: 0 }, r))
    if (r.children.length && openSet.value.has(r.key)) {
      for (const c of r.children) out.push(Object.assign({ depth: 1 }, c))
    }
  }
  return out
})

// ── 7/30 使用量窗口（δ-2 迁移:默认 30;7 天口径未知禁切,绝不把 null 画成 0）──
const usageWin = ref<7 | 30>(30)
const usage7dAvailable = computed(() => (props.rows ?? []).some((d) => d.qa_hits_7d != null))
const winEff = computed(() => (usageWin.value === 7 && !usage7dAvailable.value ? 30 : usageWin.value))
const usageOf = (r: CoverageTreeRow): number | null =>
  winEff.value === 7 ? r.qa_hits_7d : r.qa_hits

// 内联量条：各按列内桶行最大值归一（中心行文档条参与;使用量中心行为 null 不画）
const maxDocs = computed(() => Math.max(1, ...visibleRows.value.map((r) => r.docs)))
const maxUsage = computed(() =>
  Math.max(1, ...visibleRows.value.map((r) => usageOf(r) ?? 0)))

// 覆盖条：顶层行按 docs 分段（可加口径）
const totalDocs = computed(() => tree.value.reduce((s, r) => s + r.docs, 0))
const segments = computed(() => tree.value.filter((r) => r.docs > 0).map((r, i) => ({
  key: r.key, label: r.label, docs: r.docs,
  sharePct: totalDocs.value ? (r.docs / totalDocs.value) * 100 : 0,
  // 单色阶（accent 递减）;未归属/缺失节点用中性色——不只靠颜色,段内/图例都有文字
  style: r.kind === 'unknown' || r.kind === 'node_missing'
    ? 'background: var(--border-strong)'
    : `background: color-mix(in srgb, var(--accent) ${Math.max(16, 58 - i * 12)}%, var(--panel))`,
})))

const pct = (x: number) => (x * 100).toFixed(0) + '%'
const naTone = (x: number) => (x >= 0.2 ? 'text-st-fail' : x >= 0.1 ? 'text-st-busy' : 'text-muted-foreground')
const riskTone = (n: number) => (n >= 100 ? 'text-st-busy' : n > 0 ? 'text-muted-foreground' : 'text-faint')
const wowBadge = (n: number | null | undefined) =>
  n == null || n === 0 ? '' : n > 0 ? `▲+${n}` : `▼${n}`
</script>

<template>
  <div data-testid="dept-usage-block">
    <!-- 覆盖条：全库文档在组织上的分布（按中心,可加口径） -->
    <div v-if="segments.length" class="mb-1.5 flex h-5 w-full overflow-hidden rounded-md" role="img"
         :aria-label="`文档分布：${segments.map((s) => `${s.label} ${s.docs} 篇`).join('，')}`">
      <div v-for="s in segments" :key="s.key" class="flex items-center overflow-hidden"
           :style="`width:${Math.max(s.sharePct, 1.5)}%; ${s.style}`" :title="`${s.label} · ${s.docs} 篇`">
        <span v-if="s.sharePct >= 12" class="truncate px-1.5 text-[10.5px] text-foreground/80">{{ s.label }}</span>
      </div>
    </div>
    <p v-if="segments.length" class="mb-2.5 text-[11px] text-faint">
      {{ segments.slice(0, 4).map((s) => `${s.label} ${s.sharePct.toFixed(0)}%`).join(' · ') }}<template
        v-if="segments.length > 4"> · 其余 {{ segments.length - 4 }} 组</template>
    </p>

    <div class="mb-1.5 flex flex-wrap items-center justify-between gap-2">
      <span class="text-[12.5px] font-medium text-muted-foreground">部门覆盖与失衡</span>
      <span class="flex items-center gap-2.5">
        <span data-testid="dept-usage-title" class="text-[11px] text-faint">使用量口径：近 {{ winEff }} 天</span>
        <span class="flex gap-0.5 rounded-lg border border-border bg-panel p-0.5">
          <button v-for="w in ([7, 30] as const)" :key="w" type="button" :data-testid="`dept-usage-win-${w}`"
                  class="rounded-md px-2.5 py-1 text-[11.5px] font-medium transition"
                  :class="winEff === w ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'"
                  :disabled="w === 7 && !usage7dAvailable"
                  :title="w === 7 && !usage7dAvailable ? '7 天口径未知（环比子查询失败），暂不可切' : ''"
                  @click="usageWin = w">近 {{ w }} 天</button>
        </span>
      </span>
    </div>

    <div class="overflow-x-auto rounded-[14px] border border-border bg-card">
      <table class="w-full min-w-[560px] border-collapse text-[12.5px]">
        <thead>
          <tr class="border-b border-border text-[11px] uppercase tracking-wide text-faint">
            <th class="px-3.5 py-2.5 text-left font-semibold">部门</th>
            <th class="px-3 py-2.5 text-right font-semibold">文档</th>
            <th class="px-3 py-2.5 text-right font-semibold">本月新增</th>
            <th class="px-3 py-2.5 text-right font-semibold">使用量<span class="ml-1 font-normal text-faint">{{ winEff }} 天</span></th>
            <th class="px-3 py-2.5 text-right font-semibold" title="固定近 30 天口径">无答案率</th>
            <th class="px-3.5 py-2.5 text-right font-semibold">风险</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in visibleRows" :key="r.key" class="border-b border-border/60 last:border-0"
              :class="r.depth ? 'bg-panel/40' : ''">
            <td class="px-3.5 py-2" :class="r.depth ? 'pl-9' : ''">
              <button v-if="r.children.length" type="button"
                      class="mr-1 inline-flex size-4 items-center justify-center rounded text-muted-foreground transition hover:text-foreground"
                      :aria-expanded="openSet.has(r.key)"
                      :aria-label="`${openSet.has(r.key) ? '收起' : '展开'} ${r.label} 的下级部门`"
                      @click="toggleOpen(r.key)">
                <ChevronRight :size="13" class="transition-transform" :class="{ 'rotate-90': openSet.has(r.key) }" />
              </button>
              <span class="font-medium" :class="r.depth ? 'text-muted-foreground' : 'text-foreground'">{{ r.label }}</span>
            </td>
            <td class="px-3 py-2 text-right">
              <span class="mr-1.5 inline-block h-1.5 w-14 overflow-hidden rounded-full bg-panel align-middle">
                <span class="block h-full rounded-full bg-accent/55" :style="`width:${(r.docs / maxDocs) * 100}%`" />
              </span>
              <span class="font-mono tabular-nums text-muted-foreground">{{ r.docs }}</span>
              <span v-if="wowBadge(r.wow_net)" class="ml-1 text-[10.5px]"
                    :class="(r.wow_net ?? 0) > 0 ? 'text-accent-text' : 'text-st-busy'">{{ wowBadge(r.wow_net) }}</span>
            </td>
            <td class="px-3 py-2 text-right font-mono tabular-nums" :class="r.new_month ? 'text-accent-text' : 'text-faint'">
              {{ r.new_month ? '+' + r.new_month : '—' }}</td>
            <td class="px-3 py-2 text-right">
              <template v-if="usageOf(r) != null">
                <span class="mr-1.5 inline-block h-1.5 w-14 overflow-hidden rounded-full bg-panel align-middle">
                  <span class="block h-full rounded-full bg-st-warn/50" :style="`width:${((usageOf(r) || 0) / maxUsage) * 100}%`" />
                </span>
                <span class="font-mono tabular-nums text-muted-foreground">{{ usageOf(r) }}</span>
                <span v-if="wowBadge(r.qa_wow_net)" class="ml-1 text-[10.5px]"
                      :class="(r.qa_wow_net ?? 0) > 0 ? 'text-accent-text' : 'text-st-busy'">{{ wowBadge(r.qa_wow_net) }}</span>
              </template>
              <span v-else class="text-faint" title="跨部门提问去重仅部门级口径——展开查看各部门">—</span>
            </td>
            <td class="px-3 py-2 text-right font-mono font-semibold tabular-nums"
                :class="r.no_answer_rate == null ? 'text-faint' : naTone(r.no_answer_rate)">
              <span v-if="r.no_answer_rate == null" title="跨部门提问去重仅部门级口径——展开查看各部门">—</span>
              <template v-else>{{ pct(r.no_answer_rate) }}</template>
            </td>
            <td class="px-3.5 py-2 text-right font-mono tabular-nums" :class="riskTone(r.pii_docs)">{{ r.pii_docs }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!visibleRows.length" class="px-4 py-6 text-center text-sm text-muted-foreground">暂无部门数据。</p>
    </div>
    <p class="mb-1 ml-0.5 mt-1.5 text-[11.5px] text-faint">
      覆盖多≠用得多：同一行对照「文档 vs 使用量」找失衡；「无答案率」高 = 被问到却答不好（30 天口径），「风险」= 含敏感信息文档数。<template
        v-if="!treeUsable"> 组织快照暂不可用——本表按原始桶平铺，未做中心卷积。</template>
    </p>
  </div>
</template>
