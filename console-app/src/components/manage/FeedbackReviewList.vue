<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ThumbsDown, Check, Ban, RotateCcw, CheckCircle2, MessageSquareText, AlertTriangle, RotateCw, ArrowDownWideNarrow } from '@lucide/vue'
import { deptLabel } from '@/lib/kb'
import { useKb, type FeedbackReviewItem } from '@/composables/useKb'
import LoadError from './LoadError.vue'
import QueuePager from './QueuePager.vue'

// 差评联动复核（看板卡片）：引用了本作用域文档的回答收到 👎 —— 逐条列出脱敏提问 +
// 点踩原因 + 用户补充说明 + 涉及文档，并可一键处置（已修复/忽略/重开）。这是「文档质量 →
// 答案质量」最直接的改进线索：看清用户嫌哪儿不对 → 修文档或去知识贡献补充 → 标记闭环。
// 头部语义（设计稿 2026-07-19 §1）：红头卡（st-fail 7% 底 + st-fail 计数 + 卡边框 st-fail 混合）。
const {
  feedbackReview, loadFeedbackReview, loadErrors,
  showResolvedFeedback, toggleShowResolvedFeedback, resolveFeedback, feedbackResolveBusy,
} = useKb()

// 显式降级（staging 2026-07-11 P1 教训）：接口真错误时绝不渲染「已清空/无差评」快乐空态——
// 无数据可显 → 错误占位卡（含重试）；有旧数据 → 顶部错误条 + 保留旧列表。
const loadFailed = computed(() => !!loadErrors.value['feedbackReview'])
const hasRows = computed(() => !!feedbackReview.value?.length)
function busy(id: string) { return feedbackResolveBusy.value.has(id) }

// ── 时间范围下拉（设计稿 §2/§4：近 7/30/90 天/全部，默认近 30 天；按反馈时间 created_at 前端过滤）──
const RANGES = [
  { key: '7', label: '近 7 天', days: 7 },
  { key: '30', label: '近 30 天', days: 30 },
  { key: '90', label: '近 90 天', days: 90 },
  { key: 'all', label: '全部时间', days: null },
] as const
const rangeKey = ref<string>('30')
function tsOf(it: FeedbackReviewItem): number | null {
  const t = Date.parse((it.created_at || '').replace(' ', 'T'))
  return Number.isFinite(t) ? t : null
}
const ranged = computed(() => {
  const rows = feedbackReview.value || []
  const days = RANGES.find((r) => r.key === rangeKey.value)?.days ?? null
  if (days === null) return rows
  const cutoff = Date.now() - days * 86400000
  // 缺失/坏时间戳的行不因过滤而消失（graceful degradation：宁多显勿静默丢差评）
  return rows.filter((it) => { const t = tsOf(it); return t === null || t >= cutoff })
})
function onRangeChange() { pageReq.value = 1 }

// ── 前端分页 + 时间排序（设计稿 §2：2 条/页；默认新→旧；切排序/范围回第 1 页）──
// 页码经 computed 收敛：处置后列表变短，当前页自动回落不越界。
const PER_PAGE = 2
const sortDesc = ref(true)
const pageReq = ref(1)
const sorted = computed(() =>
  [...ranged.value].sort((a, b) => {
    const cmp = (a.created_at || '').localeCompare(b.created_at || '')
    return sortDesc.value ? -cmp : cmp
  }))
const pages = computed(() => Math.max(1, Math.ceil(sorted.value.length / PER_PAGE)))
const page = computed(() => Math.min(pageReq.value, pages.value))
const paged = computed(() => sorted.value.slice((page.value - 1) * PER_PAGE, page.value * PER_PAGE))
function toggleSort() { sortDesc.value = !sortDesc.value; pageReq.value = 1 }
function onToggleResolved() { pageReq.value = 1; toggleShowResolvedFeedback() }

// 「差评 → 修文档」补断层：涉及文档 chip 点击 = 切「文档管理」tab + 台账按标题定位。
// 走 URL（tab+q）→ ManageView 路由 watcher 切 tab → DocTable 挂载时从 URL 恢复搜索,零新机制。
const router = useRouter()
const route = useRoute()
function gotoDoc(d: { doc_id: string; title?: string }) {
  void router?.replace({ query: { ...(route?.query || {}), tab: 'docs', q: d.title || d.doc_id } })
  scrollWhenReady('kb-sec-ledger')
}
/** 等目标元素真挂载后再滚（切 tab 后 DocTable 渲染时序不定，固定 400ms 延时慢机会静默落空）。
 *  rAF 逐帧探测，1.5s 截止后放弃（元素仍不存在 = tab 没切成，滚了也没意义）。 */
function scrollWhenReady(id: string, deadlineMs = 1500) {
  const t0 = performance.now()
  const tick = () => {
    const el = document.getElementById(id)
    if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); return }
    if (performance.now() - t0 < deadlineMs) requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
}
</script>

<template>
  <div>
    <!-- 刷新失败但有旧数据：顶部错误条（自带重试），旧列表照常保留在下方 -->
    <LoadError v-if="hasRows" class="mb-2.5" :message="loadErrors['feedbackReview']" @retry="loadFeedbackReview()" />
    <!-- 加载失败且无数据可显：显式错误占位卡——差评可能存在但当前不可见，绝不伪装成「无差评」 -->
    <div
      v-if="loadFailed && !hasRows" role="alert" data-testid="feedback-review-error"
      class="rounded-[14px] border border-st-fail/30 bg-st-fail/5 p-5"
    >
      <div class="flex items-center gap-1.5 text-[12.5px] font-semibold text-st-fail">
        <AlertTriangle :size="14" :stroke-width="1.75" /> 差评复核加载失败
      </div>
      <p class="mt-1 text-[12px] text-muted-foreground">
        列表暂时拉取不到（服务端错误）——差评可能存在但当前不可见，请勿当作「无差评」。可重试；反复失败请联系管理员查
        <code class="font-mono text-[11px]">/api/kb/feedback-review</code> 服务端日志。
      </p>
      <button
        type="button"
        class="mt-2.5 inline-flex items-center gap-1 rounded-md border border-st-fail/40 px-2.5 py-1 text-[11.5px] font-medium text-st-fail transition hover:bg-st-fail/10"
        @click="loadFeedbackReview()"
      ><RotateCw :size="12" :stroke-width="1.75" /> 重试</button>
    </div>
    <div v-else-if="feedbackReview === null" class="rounded-[14px] border border-dashed border-border bg-card/60 p-5 text-[12.5px] text-muted-foreground">
      差评复核拉取中…
    </div>
    <!-- 红头卡（qcard-fail）：st-fail 30% 混合边框 + 头/计数/工具条一体 -->
    <div v-else class="overflow-hidden rounded-[14px] border border-st-fail/30 bg-card">
      <div class="flex flex-wrap items-center gap-x-2.5 gap-y-2 border-b border-border bg-st-fail/[0.07] px-4 py-3">
        <ThumbsDown :size="16" :stroke-width="1.75" class="shrink-0 text-st-fail" />
        <span class="text-sm font-semibold text-foreground">差评复核</span>
        <span class="rounded-full bg-st-fail px-2 py-px text-[11px] font-bold text-white">{{ sorted.length }}</span>
        <button
          type="button" data-testid="queue-sort"
          class="inline-flex items-center gap-1 rounded-full border border-border px-2.5 py-0.5 text-[11px] font-medium text-muted-foreground transition hover:bg-panel"
          title="按反馈时间排序，点击切换" @click="toggleSort"
        ><ArrowDownWideNarrow :size="11" :stroke-width="1.75" /> <span class="tabular-nums">{{ sortDesc ? '新→旧' : '旧→新' }}</span></button>
        <select
          v-model="rangeKey" data-testid="feedback-range" aria-label="时间范围"
          class="ui-select rounded-md border border-border bg-card px-2 py-0.5 text-[11px] text-muted-foreground focus:border-ring focus:outline-none"
          @change="onRangeChange"
        >
          <option v-for="r in RANGES" :key="r.key" :value="r.key">{{ r.label }}</option>
        </select>
        <button
          type="button"
          class="rounded-full border border-border px-2.5 py-0.5 text-[11px] font-medium text-muted-foreground transition hover:bg-panel hover:text-foreground"
          @click="onToggleResolved"
        >{{ showResolvedFeedback ? '只看未处理' : '显示已处理' }}</button>
        <div class="flex-1" />
        <span class="hidden text-xs text-muted-foreground lg:inline">用户对答案的差评，定位到文档修复</span>
      </div>

      <!-- 空态：真无数据沿用原文案；仅被时间范围滤空时如实说明（可放宽） -->
      <div v-if="!feedbackReview.length" class="px-4 py-5 text-[12.5px] text-muted-foreground">
        {{ showResolvedFeedback ? '近期无「引用本部门文档且被点踩」的回答 —— 保持。' : '未处理的差评已清空 —— 干得漂亮。' }}
      </div>
      <div v-else-if="!sorted.length" class="px-4 py-5 text-[12.5px] text-muted-foreground">
        当前时间范围内无记录 —— 可切换时间范围查看更早的差评。
      </div>
      <template v-else>
        <div
          v-for="it in paged" :key="it.message_id"
          class="border-b border-border px-4 py-3 last:border-b-0"
          :data-handled="it.handled ? '1' : '0'"
          :class="it.handled ? 'bg-panel/40' : ''"
        >
          <div class="flex items-start gap-3">
            <span class="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg" :class="it.handled ? 'bg-st-live/10 text-st-live' : 'bg-st-fail/10 text-st-fail'">
              <CheckCircle2 v-if="it.handled" :size="13" :stroke-width="1.75" />
              <ThumbsDown v-else :size="13" :stroke-width="1.75" />
            </span>
            <div class="min-w-0 flex-1">
              <div class="text-[13px] font-medium text-foreground" :class="it.handled ? 'line-through decoration-faint/60' : ''">{{ it.question || '（无提问文本）' }}</div>
              <!-- 点踩原因（用户多选）：一眼看清「嫌哪儿不对」 -->
              <div v-if="it.reasons.length" class="mt-1 flex flex-wrap items-center gap-1">
                <span
                  v-for="r in it.reasons" :key="r"
                  class="rounded bg-st-fail/10 px-1.5 py-0.5 text-[10.5px] font-medium text-st-fail"
                >{{ r }}</span>
              </div>
              <!-- 用户补充说明（已脱敏）：修文档最关键的一手线索 -->
              <div v-if="it.comment" class="mt-1.5 flex items-start gap-1.5 rounded-md bg-panel px-2 py-1.5 text-[11.5px] text-muted-foreground">
                <MessageSquareText :size="12" :stroke-width="1.75" class="mt-0.5 shrink-0 text-faint" />
                <span class="min-w-0">{{ it.comment }}</span>
              </div>
              <!-- 涉及文档 chips：点击直达台账该文档（修文档是差评闭环的下一步，原先是死文本要自己去搜） -->
              <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
                <button
                  v-for="d in it.docs" :key="d.doc_id" type="button"
                  class="rounded-full border border-border bg-panel px-2 py-0.5 text-[10.5px] text-muted-foreground transition hover:border-accent-strong hover:bg-accent-soft hover:text-accent-text"
                  :title="`在文档台账中定位（${d.doc_id}）`"
                  @click="gotoDoc(d)"
                >{{ d.title || d.doc_id }} · {{ deptLabel(d.owner_dept) }}</button>
              </div>
            </div>
            <span class="shrink-0 font-mono text-[10.5px] tabular-nums text-faint">{{ (it.created_at || '').slice(0, 16) }}</span>
          </div>
          <!-- 处置动作条：闭环最后一步 -->
          <div class="mt-2 flex items-center justify-end gap-1.5 pl-10">
            <template v-if="!it.handled">
              <button
                type="button" :disabled="busy(it.message_id)"
                class="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground disabled:opacity-50"
                @click="resolveFeedback(it.message_id, 'dismiss')"
              ><Ban :size="11" :stroke-width="1.75" /> 忽略</button>
              <button
                type="button" :disabled="busy(it.message_id)"
                class="inline-flex items-center gap-1 rounded-md bg-st-live/10 px-2.5 py-1 text-[11px] font-semibold text-st-live transition hover:bg-st-live/15 disabled:opacity-50"
                @click="resolveFeedback(it.message_id, 'resolve')"
              ><Check :size="11" :stroke-width="2.5" /> 标记已处理</button>
            </template>
            <template v-else>
              <span class="text-[10.5px] text-faint">{{ it.handled_status === 'DISMISSED' ? '已忽略' : '已处理' }}</span>
              <button
                type="button" :disabled="busy(it.message_id)"
                class="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground disabled:opacity-50"
                @click="resolveFeedback(it.message_id, 'reopen')"
              ><RotateCcw :size="11" :stroke-width="1.75" /> 重开</button>
            </template>
          </div>
        </div>
        <!-- 翻页脚（单页自隐） -->
        <QueuePager :total="sorted.length" :page="page" :per-page="PER_PAGE" @update:page="pageReq = $event" />
      </template>
    </div>
    <p class="ml-0.5 mt-2 text-[11.5px] text-faint">
      这些回答实际引用了本部门文档却被用户点踩——按原因/补充说明核对文档是否过时/表述不清；改好后点「标记已处理」；确有缺口可
      <RouterLink to="/contribute" class="font-semibold text-accent-text transition hover:underline">去知识贡献补充 →</RouterLink>
    </p>
  </div>
</template>
