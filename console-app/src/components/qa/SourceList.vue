<script setup lang="ts">
import { computed, ref } from 'vue'
import { FileText } from 'lucide-vue-next'
import type { SourceRow } from '@/composables/useAsk'

// Atlas 式来源：一排「来源」chip（文件图标 + 标题 + 章节 + 相关度点）；点击展开该来源详情卡
// （相关度条 + 档位/分数 + 正文省略版）。同一时间只展开一条。
const props = defineProps<{ sources: SourceRow[] }>()
// 防御：回灌历史/后端偶发可能塞进 null 或缺 idx 的坏来源，v-for 里解引用会整块渲染崩溃 → 先滤掉。
const rows = computed(() => (props.sources || []).filter((s): s is SourceRow => !!s && s.idx != null))
const openIdx = ref<number | null>(null)
function toggle(idx: number) { openIdx.value = openIdx.value === idx ? null : idx }

const DOT: Record<string, string> = { high: 'bg-st-live', mid: 'bg-st-busy', low: 'bg-st-queue' }
// 相关度条：宽度按服务端归一 relevance(0-1) 比例画，颜色按档位（量纲已由后端消化，前端不再硬编码桶宽）。
const FILL: Record<string, string> = { high: 'bg-st-live', mid: 'bg-st-busy', low: 'bg-st-queue' }
// 相关度分数去伪精度：四舍五入到 1 位并去尾随 0（8.40→8.4、8.00→8），非等宽（非代码片段）。
const fmtScore = (n: number) => (Math.round(n * 10) / 10).toString()
</script>

<template>
  <div class="mt-3.5">
    <div class="flex flex-wrap items-center gap-2">
      <span class="text-[11px] font-bold uppercase tracking-[0.06em] text-faint">来源</span>
      <button
        v-for="s in rows" :key="s.idx" type="button" data-testid="citation"
        class="inline-flex max-w-[20rem] items-center gap-2 rounded-full border px-2.5 py-1 text-xs transition hover:border-border-strong"
        :class="openIdx === s.idx ? 'border-accent-text bg-accent-soft' : 'border-border bg-surface'"
        :aria-expanded="openIdx === s.idx" @click="toggle(s.idx)"
      >
        <span class="size-1.5 shrink-0 rounded-full" :class="DOT[s.level]" />
        <FileText :size="13" :stroke-width="1.7" class="shrink-0 text-accent-text" />
        <span class="min-w-0 truncate font-medium text-foreground" :title="s.title">{{ s.title }}</span>
        <span v-if="s.section" class="shrink-0 text-faint">· {{ s.section }}</span>
      </button>
    </div>

    <!-- 来源详情（Atlas「Retrieved sources」面板内联版） -->
    <template v-for="s in rows" :key="'d' + s.idx">
      <div v-if="openIdx === s.idx" class="mt-2 rounded-[13px] border border-border bg-panel p-3.5">
        <!-- 标题不在卡内重复（上方选中态 chip 即标题）；卡内只给相关度 + 引文 + 说明 -->
        <div class="flex items-center gap-2.5">
          <span class="text-[10.5px] font-bold uppercase tracking-[0.03em] text-faint">相关度</span>
          <span class="h-[5px] flex-1 overflow-hidden rounded-full bg-track">
            <span class="block h-full rounded-full transition-[width]" :class="FILL[s.level]" :style="{ width: Math.max(6, Math.round(s.relevance * 100)) + '%' }" />
          </span>
          <span class="text-[11.5px] font-semibold text-accent-text">{{ s.levelLabel }} · {{ fmtScore(s.score) }}</span>
        </div>
        <p v-if="s.preview" class="mt-3 text-[12.5px] italic leading-relaxed text-muted-foreground">“{{ s.preview }}”</p>
        <p class="mt-2.5 text-[11px] leading-relaxed text-faint">该片段取自你已索引的文档、作为上下文喂给模型。</p>
      </div>
    </template>
  </div>
</template>
