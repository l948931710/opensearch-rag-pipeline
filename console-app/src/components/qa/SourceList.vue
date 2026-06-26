<script setup lang="ts">
import { ref } from 'vue'
import { FileText } from 'lucide-vue-next'
import type { SourceRow } from '@/composables/useAsk'

// Atlas 式来源：一排「来源」chip（文件图标 + 标题 + 章节 + 相关度点）；点击展开该来源详情卡
// （相关度条 + 档位/分数 + 正文省略版）。同一时间只展开一条。
defineProps<{ sources: SourceRow[] }>()
const openIdx = ref<number | null>(null)
function toggle(idx: number) { openIdx.value = openIdx.value === idx ? null : idx }

const DOT: Record<string, string> = { high: 'bg-st-live', mid: 'bg-st-busy', low: 'bg-st-queue' }
// 相关度条：宽度按服务端归一 relevance(0-1) 比例画，颜色按档位（量纲已由后端消化，前端不再硬编码桶宽）。
const FILL: Record<string, string> = { high: 'bg-st-live', mid: 'bg-st-busy', low: 'bg-st-queue' }
</script>

<template>
  <div class="mt-3.5">
    <div class="flex flex-wrap items-center gap-2">
      <span class="text-[11px] font-bold uppercase tracking-[0.06em] text-faint">来源</span>
      <button
        v-for="s in sources" :key="s.idx" type="button"
        class="inline-flex max-w-[20rem] items-center gap-2 rounded-lg border bg-surface px-2.5 py-1.5 text-xs transition hover:border-border-strong"
        :class="openIdx === s.idx ? 'border-border-strong' : 'border-border'"
        :aria-expanded="openIdx === s.idx" @click="toggle(s.idx)"
      >
        <span class="size-1.5 shrink-0 rounded-full" :class="DOT[s.level]" />
        <FileText :size="13" :stroke-width="1.7" class="shrink-0 text-accent-text" />
        <span class="truncate font-medium text-foreground">{{ s.title }}</span>
        <span v-if="s.section" class="shrink-0 text-faint">· {{ s.section }}</span>
      </button>
    </div>

    <!-- 来源详情（Atlas「Retrieved sources」面板内联版） -->
    <template v-for="s in sources" :key="'d' + s.idx">
      <div v-if="openIdx === s.idx" class="mt-2 rounded-[13px] border border-border bg-panel p-3.5">
        <div class="flex items-center gap-2.5">
          <span class="grid size-7 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent-text">
            <FileText :size="14" :stroke-width="1.7" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="truncate text-[13px] font-semibold text-foreground">{{ s.title }}</div>
            <div v-if="s.section" class="truncate text-[11.5px] text-faint">{{ s.section }}</div>
          </div>
        </div>
        <div class="mt-3 flex items-center gap-2.5">
          <span class="text-[10.5px] font-bold uppercase tracking-[0.03em] text-faint">相关度</span>
          <span class="h-[5px] flex-1 overflow-hidden rounded-full bg-border">
            <span class="block h-full rounded-full transition-[width]" :class="FILL[s.level]" :style="{ width: Math.max(6, Math.round(s.relevance * 100)) + '%' }" />
          </span>
          <span class="font-mono text-[11.5px] font-semibold text-accent-text">{{ s.levelLabel }} · {{ s.score.toFixed(2) }}</span>
        </div>
        <p v-if="s.preview" class="mt-3 text-[12.5px] italic leading-relaxed text-muted-foreground">“{{ s.preview }}”</p>
        <p class="mt-2.5 text-[11px] leading-relaxed text-faint">该片段取自你已索引的文档、作为上下文喂给模型；按部门权限过滤。</p>
      </div>
    </template>
  </div>
</template>
