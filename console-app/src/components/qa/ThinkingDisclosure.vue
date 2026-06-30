<script setup lang="ts">
import { computed } from 'vue'
import { Brain, ChevronDown } from 'lucide-vue-next'
import type { ChatMessage } from '@/composables/useAsk'

// 「深度思考」思考过程披露条：思考阶段（reasoning 帧流入、答案未开始）默认展开、显"正在深度思考…"+
// 流式光标，文本经思考通道匀速显现；答案一开始自动收起为可点开的"思考过程"。仅当消息带 reasoning 时渲染。
const props = defineProps<{ message: ChatMessage }>()
const m = props.message

const streaming = computed(() =>
  !!m.reasoning && m.html == null && !m.viewBlocks && !m.error && !m.noResult)
const open = computed(() => m.reasoningOpen !== false)
function toggle() { m.reasoningOpen = !open.value }
</script>

<template>
  <section class="mb-2.5 overflow-hidden rounded-[12px] border border-border bg-secondary/30">
    <button
      type="button"
      class="flex w-full items-center gap-2 px-3 py-2 text-left transition hover:bg-secondary/60"
      :aria-expanded="open"
      @click="toggle"
    >
      <Brain :size="14" :stroke-width="1.85" :class="streaming ? 'text-accent-text animate-pulse' : 'text-faint'" />
      <span class="text-[12.5px] font-medium" :class="streaming ? 'text-foreground' : 'text-muted-foreground'">
        {{ streaming ? '正在深度思考…' : '思考过程' }}
      </span>
      <span v-if="!streaming" class="text-[11px] text-faint">{{ open ? '点击收起' : '点击展开' }}</span>
      <div class="flex-1" />
      <ChevronDown :size="14" :stroke-width="2" class="text-faint transition-transform duration-200" :class="open ? 'rotate-180' : ''" />
    </button>
    <div v-show="open" class="border-t border-border px-3 py-2">
      <div
        class="md md-reason max-h-60 overflow-y-auto text-[12.5px] leading-relaxed text-muted-foreground"
        :class="{ 'is-streaming': streaming }"
        v-html="m.reasoningHtml"
      />
    </div>
  </section>
</template>

<style scoped>
/* 思考正文比答案更暗更紧凑（区别于正式答案）；段距收紧。 */
.md-reason :deep(p) { margin: 0 0 7px; }
.md-reason :deep(*:last-child) { margin-bottom: 0; }
</style>
