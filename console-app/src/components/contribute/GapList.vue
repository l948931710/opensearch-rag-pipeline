<script setup lang="ts">
import { computed } from 'vue'
import { HelpCircle, Clock } from 'lucide-vue-next'
import { deptLabel, fmtWindowDays, gapKindLabel } from '@/lib/kb'
import { useContribute, type GapItem } from '@/composables/useContribute'
import LoadError from '@/components/manage/LoadError.vue'

// 「待回答」= 高频无人回答排行 Top 30（批次ε-4）：后端按询问次数降序，前端只取前 30、
// 不翻页；全集规模在卡头徽标（=summary.unanswered，与统计卡同口径）与截断尾注如实披露。
// 「回答」打开贡献弹窗并预填该问题；已有贡献待入库的标灰提示、不重复发起。
const { gaps, gapsSummary, loadingGaps, loadErrors, gapsWindowDays, loadGaps, openModal } = useContribute()

// 缺口种类是最需要一眼分开的信息：缺文档（红）要新建内容，没答好（琥珀）改进已有内容即可。
const KIND_PILL: Record<string, string> = {
  no_result: 'bg-st-fail/10 text-st-fail', refusal: 'bg-st-busy/10 text-st-busy',
}

// 全集数（=统计卡「待回答问题」同源）；截断时尾注披露，绝不静默
const totalUnanswered = computed(() => gapsSummary.value?.unanswered ?? gaps.value.length)
const truncated = computed(() => totalUnanswered.value > gaps.value.length)

function onAnswer(g: GapItem) {
  openModal({ question: g.question, dept: g.dept, sourceMessageId: g.source_message_id, gapQuery: g.question })
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
          </div>
        </div>
        <span
          v-if="g.has_pending_contribution"
          class="shrink-0 rounded-lg bg-st-busy/10 px-3 py-[7px] text-[12px] font-medium text-st-busy"
        >已有贡献·待入库</span>
        <button
          v-else type="button"
          class="shrink-0 rounded-lg border border-border bg-transparent px-3.5 py-[7px] text-[12.5px] font-semibold text-accent-text transition hover:border-accent-strong hover:bg-accent-soft"
          @click="onAnswer(g)"
        >回答</button>
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
  </section>
</template>
