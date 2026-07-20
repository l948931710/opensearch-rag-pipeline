<script setup lang="ts">
import { computed, ref } from 'vue'
import { ShieldCheck, FileText, Loader2, Hourglass, ArrowDownWideNarrow } from '@lucide/vue'
import { deptLabel, permLabel } from '@/lib/kb'
import { useKb, type AccessGrantItem } from '@/composables/useKb'
import LoadError from './LoadError.vue'
import QueuePager from './QueuePager.vue'
import { useDialog } from '@/composables/useDialog'

// 已授权清单（审批人侧）：本部门文档现行有效（approved 存量）的跨部门检索授权，可撤销（approved→revoked）。
// 与「授权申请」（pending 待审批）区分：此处是已放行的存量，活跃态调（st-live）。空时整块不渲染。
const { accessGrants, isBusy, revokeAccess, loadAccessGrants, loadErrors } = useKb()
const { promptText } = useDialog()

// requester_depts 为逗号分隔组码（多部门管理员可一次授予多组）→ 逐个 deptLabel 再拼。
const reqLabel = (csv: string) => csv.split(',').map((c) => deptLabel(c.trim())).filter(Boolean).join('、')

// ── 授权老化治理：授权是永久的（无到期列），至少让"放出去多久了"可见、可筛 ──
// >STALE_DAYS 天未复核 → 「建议复核」amber 徽章；头部一键只看陈旧授权（定期复核的最小闭环，零 DDL）。
const STALE_DAYS = 90
const staleOnly = ref(false)
function grantAgeDays(g: AccessGrantItem): number | null {
  const t = Date.parse((g.decided_at || '').replace(' ', 'T'))
  return Number.isFinite(t) ? Math.floor((Date.now() - t) / 86400000) : null
}
const isStale = (g: AccessGrantItem) => (grantAgeDays(g) ?? 0) > STALE_DAYS
const staleCount = computed(() => accessGrants.value.filter(isStale).length)
const shown = computed(() => (staleOnly.value ? accessGrants.value.filter(isStale) : accessGrants.value))
const ageText = (g: AccessGrantItem) => {
  const d = grantAgeDays(g)
  return d === null ? '' : d < 1 ? '今天' : `${d} 天`
}
function toggleStaleOnly() { staleOnly.value = !staleOnly.value; pageReq.value = 1 }

// ── 前端分页 + 时间排序（设计稿 2026-07-19 §2：队列 2 条/页；按授权时间 decided_at，默认新→旧）──
// 分页作用于「待复核」过滤后的 shown；撤销/切筛选后页码经 computed 收敛不越界。
const PER_PAGE = 2
const sortDesc = ref(true)
const pageReq = ref(1)
const sorted = computed(() =>
  [...shown.value].sort((a, b) => {
    const cmp = (a.decided_at || '').localeCompare(b.decided_at || '')
    return sortDesc.value ? -cmp : cmp
  }))
const pages = computed(() => Math.max(1, Math.ceil(sorted.value.length / PER_PAGE)))
const page = computed(() => Math.min(pageReq.value, pages.value))
const paged = computed(() => sorted.value.slice((page.value - 1) * PER_PAGE, page.value * PER_PAGE))
function toggleSort() { sortDesc.value = !sortDesc.value; pageReq.value = 1 }

// 统一撤销授权确认模式（P2）：原为 confirm+prompt 两段弹窗（此处）/行内二段确认（权限弹窗）
// 两套并存；现统一为与「驳回」同构的单个 danger 原因弹窗——一步说明影响面 + 采集审计原因。
async function onRevoke(g: AccessGrantItem) {
  const reason = await promptText({
    title: '撤销授权', confirmText: '确认撤销', danger: true,
    message: `撤销「${reqLabel(g.requester_dept)}」对《${g.doc_title}》的检索授权？\n撤销后该部门将不再能检索此文档（即时生效），申请人可重新申请。`,
    placeholder: '撤销原因（可空，记录于审计）',
  })
  if (reason === null) return   // 取消
  void revokeAccess(g, reason || 'revoked')
}
</script>

<template>
  <!-- 卡头已带图标+标题+计数，不再另设分区眉标 -->
  <section v-if="accessGrants.length || loadErrors['accessGrants']">
    <LoadError class="mb-2.5" :message="loadErrors['accessGrants']" @retry="loadAccessGrants()" />
    <div v-if="accessGrants.length" class="overflow-hidden rounded-[15px] border border-border bg-card">
      <!-- 活跃态头（st-live，与待处理的橙头区分：这里是已放行存量） -->
      <div class="flex flex-wrap items-center gap-x-2.5 gap-y-2 border-b border-border bg-st-live/10 px-[18px] py-3">
        <ShieldCheck :size="16" :stroke-width="1.75" class="text-st-live" />
        <span class="text-sm font-semibold text-foreground">已授权</span>
        <span class="rounded-full bg-st-live px-2 py-px text-[11px] font-bold text-white">{{ accessGrants.length }}</span>
        <button
          type="button" data-testid="queue-sort"
          class="inline-flex items-center gap-1 rounded-full border border-border px-2.5 py-0.5 text-[11px] font-medium text-muted-foreground transition hover:bg-panel"
          title="按授权时间排序，点击切换" @click="toggleSort"
        ><ArrowDownWideNarrow :size="11" :stroke-width="1.75" /> <span class="tabular-nums">{{ sortDesc ? '新→旧' : '旧→新' }}</span></button>
        <button
          v-if="staleCount"
          type="button"
          class="inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition"
          :class="staleOnly ? 'border-st-warn bg-st-warn/15 text-st-warn' : 'border-border text-muted-foreground hover:bg-panel'"
          :title="`授权超过 ${STALE_DAYS} 天未复核`"
          @click="toggleStaleOnly"
        ><Hourglass :size="11" :stroke-width="1.75" /> 待复核 {{ staleCount }}</button>
        <div class="flex-1" />
        <span class="hidden text-xs text-muted-foreground sm:inline">本部门文档已放行的跨部门检索授权，可撤销</span>
      </div>
      <!-- 行（当前页切片） -->
      <div
        v-for="g in paged" :key="g.id"
        class="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <span class="grid size-8 shrink-0 place-items-center rounded-lg bg-st-live/10 text-st-live">
          <FileText :size="16" :stroke-width="1.75" />
        </span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13.5px] font-semibold text-foreground">
            <span class="text-st-live">{{ reqLabel(g.requester_dept) }}</span> 可检索《{{ g.doc_title }}》
          </div>
          <div class="truncate text-[11.5px] text-faint">
            归属 {{ deptLabel(g.owner_dept) }} · {{ permLabel(g.permission_level) }} · 申请人 {{ g.requester_name }}
            <span v-if="g.decided_at"> · 授权于 {{ g.decided_at }}<template v-if="ageText(g)">（{{ ageText(g) }}）</template></span>
            <span
              v-if="isStale(g)"
              class="ml-1 whitespace-nowrap rounded border border-st-warn/40 bg-st-warn/10 px-1.5 py-px text-[10px] font-medium text-st-warn"
              :title="`已授权超过 ${STALE_DAYS} 天：请确认对方是否仍需检索本文档，不需要即撤销`"
            >建议复核</span>
          </div>
          <div v-if="g.reason" class="mt-1 line-clamp-2 text-[12px] text-muted-foreground">“{{ g.reason }}”</div>
        </div>
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="isBusy(`grant:${g.id}`)" @click="onRevoke(g)"
        ><Loader2 v-if="isBusy(`grant:${g.id}`)" :size="13" :stroke-width="2" class="animate-spin" />{{ isBusy(`grant:${g.id}`) ? '撤销中…' : '撤销' }}</button>
      </div>
      <!-- 翻页脚（单页自隐） -->
      <QueuePager :total="sorted.length" :page="page" :per-page="PER_PAGE" @update:page="pageReq = $event" />
    </div>
  </section>
</template>
