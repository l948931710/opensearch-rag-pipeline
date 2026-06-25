<script setup lang="ts">
import { RotateCw, Headset } from 'lucide-vue-next'
import { useAsk, type ChatMessage } from '@/composables/useAsk'
import AnswerBlocks from './AnswerBlocks.vue'
import SourceList from './SourceList.vue'
import FeedbackBar from './FeedbackBar.vue'

// 一条消息：用户气泡 / AI 四态（加载骨架·错误重试·无结果卡·正常答案）。
const props = defineProps<{ message: ChatMessage }>()
const { retry, handoff, fillInput } = useAsk()
const m = props.message
</script>

<template>
  <!-- 用户 -->
  <div v-if="m.role === 'user'" class="flex justify-end">
    <div class="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-[15px] text-accent-foreground">
      {{ m.text }}
    </div>
  </div>

  <!-- AI -->
  <div v-else class="max-w-full">
    <!-- 加载骨架 -->
    <div v-if="m.loading" class="flex items-center gap-2 py-1 text-sm text-muted-foreground">
      <span class="flex gap-1">
        <i class="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:-0.2s]" />
        <i class="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:-0.1s]" />
        <i class="size-1.5 animate-bounce rounded-full bg-muted-foreground/60" />
      </span>
      {{ m.stageText }}
    </div>

    <!-- 错误 + 重试 -->
    <template v-else-if="m.error">
      <div class="text-[15px] text-foreground">{{ m.errorText }}</div>
      <button
        type="button"
        class="mt-2 flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition hover:bg-secondary hover:text-foreground"
        @click="retry(m)"
      >
        <RotateCw :size="14" :stroke-width="1.75" /> 重试
      </button>
    </template>

    <!-- 无结果卡 -->
    <div v-else-if="m.noResult" class="rounded-xl border border-border bg-secondary/40 p-4">
      <div class="text-sm font-semibold text-foreground">未找到相关内容</div>
      <div class="mt-1.5 text-sm text-muted-foreground">{{ m.answer }}</div>
      <div v-if="m.rephrase && m.rephrase.length" class="mt-3">
        <div class="mb-1.5 text-xs text-muted-foreground">试试这样问</div>
        <div class="flex flex-wrap gap-1.5">
          <button
            v-for="(r, i) in m.rephrase" :key="i"
            type="button"
            class="rounded-full border border-border bg-card px-3 py-1 text-xs text-foreground transition hover:border-ring hover:bg-secondary"
            @click="fillInput(r)"
          >
            {{ r }}
          </button>
        </div>
      </div>
      <button
        type="button"
        class="mt-3 flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition hover:bg-secondary hover:text-foreground disabled:opacity-60"
        :class="{ '!text-st-live': m.handoffDone }" :disabled="m.handoffDone"
        @click="handoff(m)"
      >
        <Headset :size="14" :stroke-width="1.75" /> {{ m.handoffDone ? '已转交管理员' : '转人工' }}
      </button>
    </div>

    <!-- 正常答案 -->
    <template v-else>
      <div v-if="m.guard" class="mb-2 rounded-lg border border-st-busy/30 bg-st-busy/10 px-3 py-2 text-xs text-st-busy">
        ⚠️ 相关资料匹配度较低，以下回答仅供参考，请核对原文或转人工确认。
      </div>
      <AnswerBlocks :message="m" />
      <SourceList v-if="m.sources && m.sources.length" :sources="m.sources" />
      <FeedbackBar v-if="m.messageId" :message="m" />
    </template>
  </div>
</template>
