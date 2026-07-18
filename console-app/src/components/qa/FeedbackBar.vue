<script setup lang="ts">
import { ThumbsUp, ThumbsDown, Copy, Check } from '@lucide/vue'
import { useAsk, type ChatMessage } from '@/composables/useAsk'

// 内嵌式反馈条：复制、赞/踩（一次性互斥），全部常驻可见。统一调 /api/feedback，靠 message_id 关联。
// （转人工已下线 2026-07：语料缺口由知识贡献系统兜底。）
const props = defineProps<{ message: ChatMessage }>()
const { vote, copyAns } = useAsk()
const m = props.message
</script>

<template>
  <div class="mt-2 flex items-center gap-0.5 text-muted-foreground">
    <button
      type="button" class="grid size-7 place-items-center rounded-md transition hover:bg-secondary hover:text-foreground"
      :class="{ '!text-st-live': m.copied }" :title="m.copied ? '已复制' : '复制'" aria-label="复制"
      @click="copyAns(m)"
    >
      <Check v-if="m.copied" :size="15" :stroke-width="2" />
      <Copy v-else :size="15" :stroke-width="1.75" />
    </button>

    <span class="mx-1 h-3.5 w-px bg-border" />
    <button
      type="button" class="grid size-7 place-items-center rounded-md transition hover:bg-secondary hover:text-foreground disabled:opacity-100"
      :class="{ '!text-st-live': m.voted === 'up' }" :disabled="!!m.voted" title="有用" aria-label="有用"
      @click="vote(m, 'upvote')"
    >
      <ThumbsUp :size="15" :stroke-width="1.75" />
    </button>
    <button
      type="button" class="grid size-7 place-items-center rounded-md transition hover:bg-secondary hover:text-foreground disabled:opacity-100"
      :class="{ '!text-st-fail': m.voted === 'down' }" :disabled="!!m.voted" title="没用" aria-label="没用"
      @click="vote(m, 'downvote')"
    >
      <ThumbsDown :size="15" :stroke-width="1.75" />
    </button>
  </div>
</template>
