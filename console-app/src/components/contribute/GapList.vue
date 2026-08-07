<script setup lang="ts">
import { computed, ref } from 'vue'
import { HelpCircle, Clock, ChevronRight } from '@lucide/vue'
import { deptLabel, fmtTs, fmtWindowDays, gapKindLabel } from '@/lib/kb'
import { useContribute, type GapItem } from '@/composables/useContribute'
import { useDialog } from '@/composables/useDialog'
import LoadError from '@/components/manage/LoadError.vue'
import TruncationNotice from '@/components/manage/TruncationNotice.vue'

// 「待回答」= 高频无人回答排行 Top 30（批次ε-4）：后端按询问次数降序，前端只取前 30、
// 不翻页；全集规模在卡头徽标（=summary.unanswered，与统计卡同口径）与截断尾注如实披露。
// 「回答」打开贡献弹窗并预填该问题；已有贡献待入库的标灰提示、不重复发起。
// 「忽略」（2026-07-15 拍板交 dept_admin）：仅 canManage 渲染（员工 DOM 零变化，抄
// MyContributions 重试钮范式）；成功后行内原地翻转「已忽略·撤销」——不重拉列表（误点
// 有当场回路；撤销窗口=至下次 loadGaps，刷新后被读侧排除属预期）。非破坏性可撤销软删
// → 弹窗不用 danger 红色态（区别于撤销授权类终态动作），原因可空、记录台账。
const { gaps, gapsSummary, loadingGaps, loadErrors, gapsWindowDays, loadGaps, toggleGapContext,
        openModal, canManage, isBusy, dismissGap, restoreGap,
        dismissedGaps, dismissedGapsTruncated, loadDismissed, restoreDismissed } = useContribute()
const { promptText } = useDialog()

// 「已忽略」折叠区（2026-07-15 拍板补齐）：刷新后撤销的唯一 UI 入口（行内「撤销」只覆盖
// 本会话即时误点）。默认收起、展开时才拉取（低频审计动线不抢主列表注意力）；仅 canManage。
const dismissedOpen = ref(false)
async function toggleDismissed() {
  dismissedOpen.value = !dismissedOpen.value
  if (dismissedOpen.value) await loadDismissed()   // 每次展开取现值（含刚忽略的行）
}

// 缺口种类是最需要一眼分开的信息：缺文档（红）要新建内容，没答好（琥珀）改进已有内容即可。
const KIND_PILL: Record<string, string> = {
  no_result: 'bg-st-fail/10 text-st-fail', refusal: 'bg-st-busy/10 text-st-busy',
}

// 全集数（=统计卡「待回答问题」同源）；截断时尾注披露，绝不静默
const totalUnanswered = computed(() => gapsSummary.value?.unanswered ?? gaps.value.length)
const truncated = computed(() => totalUnanswered.value > gaps.value.length)

function onAnswer(g: GapItem) {
  // 🔴 方案 M11：`g.dept` 自 2026-08-07 起**降为展示性建议**——它是「提问部门/命中文档部门」
  // 的组码推测，node 轴下绝不得用它决定归属（组码→dept_id 一对多）。openModal 在 node 轴
  // 会忽略 dept 并回落 my-depts 默认项；这里照传只为组码轴保持原行为。
  openModal({ question: g.question, dept: g.dept, sourceMessageId: g.source_message_id, gapQuery: g.question })
}

async function onDismiss(g: GapItem) {
  const reason = await promptText({
    title: '忽略此缺口',
    message: '将从「待回答」移除（可撤销，操作记录台账）。若只是暂时无人认领，请保留。',
    placeholder: '忽略原因（可空，记录于台账）', confirmText: '忽略',
  })
  if (reason === null) return   // 取消
  await dismissGap(g, reason.trim())
}
</script>

<template>
  <section class="overflow-hidden rounded-[15px] border border-border bg-card">
    <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3">
      <HelpCircle :size="16" :stroke-width="1.75" class="text-st-warn" />
      <span class="text-sm font-semibold text-foreground">待回答</span>
      <!-- 卡头徽标=全集数（与统计卡同口径）——列表只显 Top 30，若显当页条数会与尾注打架（ε-4 审计 2-d） -->
      <span v-if="totalUnanswered" class="rounded-full bg-panel px-2 py-px text-[11px] font-bold tabular-nums text-muted-foreground" data-testid="gap-total-badge">{{ totalUnanswered }}</span>
      <div class="flex-1" />
      <!-- 窗口标注（后端下发防漂移，ε-4=近一年）——超窗老缺口静默过期消失，这不是完整积压 -->
      <span class="hidden text-xs text-muted-foreground sm:inline" data-testid="gap-window-note">{{ fmtWindowDays(gapsWindowDays) }}检索未命中 / 低置信度回答的聚合 · 按询问次数排序</span>
    </div>

    <LoadError class="m-[18px]" :message="loadErrors['gaps']" @retry="loadGaps()" />

    <div v-if="gaps.length">
      <div
        v-for="(g, i) in gaps" :key="g.question_hash"
        class="flex items-center gap-3 border-t border-border px-[18px] py-3 transition-colors first:border-t-0 hover:bg-panel/50"
      >
        <!-- 名次（排行语义可视化；克制：素灰等宽数字，不做奖牌色——这是工作队列不是庆祝榜） -->
        <span class="grid size-6 shrink-0 place-items-center rounded-md bg-panel font-mono text-[11px] font-bold tabular-nums text-faint" data-testid="gap-rank">{{ i + 1 }}</span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13.5px] font-medium text-foreground">{{ g.question }}</div>
          <div class="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[11.5px] text-faint">
            <span><span class="font-semibold tabular-nums text-foreground">{{ g.asks }}</span> 次询问</span>
            <span v-if="g.dept">· {{ deptLabel(g.dept) }}</span>
            <span class="inline-flex items-center gap-1"><Clock :size="11" :stroke-width="2" /> {{ g.last_days }} 天未回答</span>
            <span v-if="gapKindLabel(g.kind)" class="rounded px-1.5 py-px text-[10.5px] font-medium" :class="KIND_PILL[g.kind] || 'bg-panel text-muted-foreground'">{{ gapKindLabel(g.kind) }}</span>
            <!-- 语义归组（RAG_QA_GAP_SEMANTIC）：>1 = 卡片背后归并了 N 种相似问法 -->
            <span
              v-if="(g.phrasings || 1) > 1" data-testid="gap-phrasings"
              class="rounded bg-panel px-1.5 py-px text-[10.5px] font-medium tabular-nums text-muted-foreground"
            >{{ g.phrasings }} 种问法</span>
            <!-- 会话上下文（仅 has_context 才渲染——追问型缺口单看问题看不懂，展开前几轮） -->
            <button
              v-if="g.has_context" type="button" data-testid="gap-ctx-toggle"
              class="inline-flex items-center gap-0.5 rounded px-1 py-px text-[10.5px] font-medium text-accent-text transition hover:bg-accent-soft"
              :aria-expanded="!!g.ctxOpen"
              @click="toggleGapContext(g)"
            >
              <ChevronRight :size="10" :stroke-width="2" class="transition-transform" :class="g.ctxOpen && 'rotate-90'" />
              查看上下文
            </button>
          </div>
          <!-- 上下文展开区（懒加载；他人问答已服务端脱敏） -->
          <div v-if="g.ctxOpen" class="mt-2 max-h-56 overflow-y-auto rounded-lg bg-panel/60 px-3 py-2" data-testid="gap-ctx-panel">
            <p v-if="g.ctxLoading" class="text-[11.5px] text-faint">上下文加载中…</p>
            <template v-else-if="g.ctxTurns && g.ctxTurns.length">
              <div v-for="(t, ti) in g.ctxTurns" :key="ti" class="py-1 text-[11.5px] leading-relaxed" :class="ti > 0 && 'border-t border-border/60'">
                <div class="text-foreground">问：{{ t.question }}</div>
                <div v-if="t.answer_excerpt" class="mt-0.5 truncate text-muted-foreground">答：{{ t.answer_excerpt }}</div>
                <div v-else class="mt-0.5 text-faint">（该轮{{ t.answer_status === 'NO_RESULT' ? '未找到答案' : t.answer_status === 'REFUSAL' ? '未答好' : '无回答快照' }}）</div>
              </div>
            </template>
            <p v-else class="text-[11.5px] text-faint">该提问无会话上文。</p>
          </div>
        </div>
        <!-- 已忽略态（仅本会话内出现）：末尾动作位整体切换——沿用本文件既有互斥切换惯例 -->
        <template v-if="g.dismissed">
          <span class="shrink-0 text-[12px] text-faint" data-testid="gap-dismissed-hint">已忽略</span>
          <button
            type="button" data-testid="gap-restore" :disabled="isBusy(`gap:${g.question_hash}`)"
            class="shrink-0 rounded-lg border border-border bg-transparent px-3 py-[7px] text-[12px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
            @click="restoreGap(g)"
          >撤销</button>
        </template>
        <template v-else>
          <span
            v-if="g.has_pending_contribution"
            class="shrink-0 rounded-lg bg-st-busy/10 px-3 py-[7px] text-[12px] font-medium text-st-busy"
          >已有贡献·待入库</span>
          <button
            v-else type="button"
            class="shrink-0 rounded-lg border border-border bg-transparent px-3.5 py-[7px] text-[12.5px] font-semibold text-accent-text transition hover:border-accent-strong hover:bg-accent-soft"
            @click="onAnswer(g)"
          >回答</button>
          <!-- 仅 canManage 渲染（员工不渲染任何占位节点——「员工视觉零变化」硬门断言基准） -->
          <button
            v-if="canManage" type="button" data-testid="gap-dismiss"
            :disabled="isBusy(`gap:${g.question_hash}`)"
            title="从待回答移除（可撤销，记录台账）"
            class="shrink-0 rounded-lg px-2.5 py-[7px] text-[12px] font-medium text-faint transition hover:bg-panel hover:text-muted-foreground disabled:opacity-50"
            @click="onDismiss(g)"
          >忽略</button>
        </template>
      </div>
    </div>

    <div v-else-if="loadingGaps" class="px-[18px] py-10 text-center text-sm text-muted-foreground">加载中…</div>
    <div v-else class="px-[18px] py-12 text-center">
      <!-- 空态两成因区分（ε-5 R2 文案方案；出窗计数需 qa hash 列=远期立项）：标题自带窗口限定
           （单独读标题不再是「全量已解决」的过度承诺），副题明说出窗语义 -->
      <p class="text-sm font-medium text-foreground">太棒了，{{ fmtWindowDays(gapsWindowDays) }}内暂无未答出的提问</p>
      <p class="mt-1 text-xs text-muted-foreground">更早的提问已超出统计窗口——不在此列表，不代表已解决。</p>
    </div>

    <!-- 截断尾注（批次ε-4）：仅在全集超出 Top 30 时出现，如实披露两口径——杜绝静默截断 -->
    <div
      v-if="truncated" data-testid="gap-top-note"
      class="border-t border-border px-[18px] py-2.5 text-center text-[11.5px] tabular-nums text-muted-foreground"
    >仅显示询问最多的前 {{ gaps.length }} 条 · 共 {{ totalUnanswered }} 条待回答</div>

    <!-- 「已忽略」折叠区（仅 canManage；员工零节点）：刷新后撤销的唯一 UI 入口。
         默认收起、展开即拉现值；恢复=restore + 移出本区 + 重拉缺口列表（回到待回答） -->
    <div v-if="canManage" class="border-t border-border">
      <button
        type="button" data-testid="gap-dismissed-toggle" :aria-expanded="dismissedOpen"
        class="flex w-full items-center gap-1.5 px-[18px] py-2.5 text-left text-[11.5px] font-medium text-faint transition hover:text-muted-foreground"
        @click="toggleDismissed()"
      >
        <ChevronRight :size="12" :stroke-width="2" class="transition-transform" :class="dismissedOpen && 'rotate-90'" />
        已忽略<template v-if="dismissedOpen">（{{ dismissedGaps.length }}）</template>
      </button>
      <div v-if="dismissedOpen">
        <LoadError class="mx-[18px] mb-2.5" :message="loadErrors['dismissed']" @retry="loadDismissed()" />
        <div
          v-for="d in dismissedGaps" :key="d.question_hash" data-testid="gap-dismissed-row"
          class="flex items-center gap-3 border-t border-border/60 px-[18px] py-2.5"
        >
          <div class="min-w-0 flex-1">
            <div class="truncate text-[12.5px] text-muted-foreground">{{ d.question_preview || '（未留问法快照）' }}</div>
            <div class="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-faint">
              <span v-if="d.dismissed_by_name">{{ d.dismissed_by_name }} 忽略</span>
              <span v-if="d.dismissed_at">· {{ fmtTs(d.dismissed_at) }}</span>
              <span v-if="d.reason" class="truncate">· {{ d.reason }}</span>
            </div>
          </div>
          <button
            type="button" data-testid="gap-dismissed-restore" :disabled="isBusy(`gap:${d.question_hash}`)"
            class="shrink-0 rounded-lg border border-border px-3 py-[6px] text-[12px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
            @click="restoreDismissed(d)"
          >恢复</button>
        </div>
        <p v-if="!dismissedGaps.length && !loadErrors['dismissed']" class="px-[18px] pb-3 pt-1 text-[11.5px] text-faint">没有被忽略的缺口。</p>
        <!-- P3-3：后端硬 LIMIT 100。本区是「撤销忽略」的**唯一入口** ⇒ 截断=更早的忽略撤不回来。 -->
        <TruncationNotice class="px-[18px] pb-3" :when="dismissedGapsTruncated" :cap="100" label="已忽略缺口" hint="更早的忽略暂时无法在此撤销" />
      </div>
    </div>
  </section>
</template>
