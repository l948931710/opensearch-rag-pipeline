<script setup lang="ts">
import { RotateCw, Headset, FileText } from 'lucide-vue-next'
import { useAsk, type ChatMessage } from '@/composables/useAsk'
import AnswerBlocks from './AnswerBlocks.vue'
import SourceList from './SourceList.vue'
import FeedbackBar from './FeedbackBar.vue'
import ThinkingDisclosure from './ThinkingDisclosure.vue'

// 一条消息：用户气泡 / AI 多态（思考过程披露条 · 加载骨架 · 错误重试 · 无结果卡 · 正常答案）。
const props = defineProps<{ message: ChatMessage }>()
const { retry, handoff, fillInput } = useAsk()
const m = props.message

// 等待态来源预览的档位点颜色（与 SourceList 同口径）。
const DOT: Record<string, string> = { high: 'bg-st-live', mid: 'bg-st-busy', low: 'bg-st-queue' }
</script>

<template>
  <!-- 用户 -->
  <div v-if="m.role === 'user'" class="msg-row flex justify-end">
    <div class="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-user-bubble px-4 py-2.5 text-[15px] text-foreground">
      {{ m.text }}
    </div>
  </div>

  <!-- AI（Atlas 式：左侧 30px 星标头像 + 内容列） -->
  <div v-else class="msg-row group/msg flex gap-3.5">
    <span class="mt-px grid size-[30px] shrink-0 place-items-center rounded-[9px] bg-accent-strong" aria-hidden="true">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="var(--primary-foreground)" aria-hidden="true" focusable="false"><path d="M12 2.5l1.7 6.1 6.1 1.7-6.1 1.7L12 18.1l-1.7-6.1L4.2 10.3l6.1-1.7z" /></svg>
    </span>
    <div class="min-w-0 flex-1 pt-0.5">
    <!-- 思考过程披露条（深度思考；仅当有 reasoning 时）：思考期独占显示，答案到来后收起置顶 -->
    <ThinkingDisclosure v-if="m.reasoning" :message="m" />

    <!-- 加载骨架（有据等待态：检索完成即预览"找到了哪些文档"，淡入浮现） -->
    <div v-if="m.loading" class="py-1">
      <div class="flex items-center gap-2 text-sm text-muted-foreground">
        <span class="flex gap-1">
          <i class="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:-0.2s]" />
          <i class="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:-0.1s]" />
          <i class="size-1.5 animate-bounce rounded-full bg-muted-foreground/60" />
        </span>
        {{ m.stageText }}
      </div>
      <ul v-if="m.sources && m.sources.length" class="mt-2 space-y-1">
        <li
          v-for="(s, i) in m.sources.slice(0, 4)" :key="s.idx"
          class="src-pop flex items-center gap-1.5 text-xs text-faint" :style="{ animationDelay: i * 60 + 'ms' }"
        >
          <span class="size-1.5 shrink-0 rounded-full" :class="DOT[s.level]" />
          <FileText :size="12" :stroke-width="1.7" class="shrink-0 text-faint" />
          <span class="min-w-0 truncate" :title="s.title">{{ s.title }}</span>
        </li>
        <li v-if="m.sources.length > 4" class="src-pop pl-[19px] text-xs text-faint" :style="{ animationDelay: '240ms' }">
          +{{ m.sources.length - 4 }} 篇
        </li>
      </ul>
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
            class="rounded-full border border-border bg-card px-2.5 py-1 text-xs text-foreground transition hover:border-ring hover:bg-secondary"
            @click="fillInput(r)"
          >
            {{ r }}
          </button>
        </div>
      </div>
      <button
        type="button"
        class="mt-3 flex h-7 items-center gap-1.5 rounded-md px-2 text-xs text-muted-foreground transition hover:bg-secondary hover:text-foreground disabled:opacity-60"
        :class="{ '!text-st-live': m.handoffDone }" :disabled="m.handoffDone"
        @click="handoff(m)"
      >
        <Headset :size="15" :stroke-width="1.75" /> {{ m.handoffDone ? '已转交管理员' : '转人工' }}
      </button>
    </div>

    <!-- 正常答案（答案有内容才渲染；思考独占阶段只显披露条，不露空答案区） -->
    <template v-else-if="m.html != null || m.viewBlocks">
      <div v-if="m.guard" class="mb-2 rounded-lg border border-st-busy/30 bg-st-busy/10 px-3 py-2 text-xs text-st-busy">
        ⚠️ 相关资料匹配度较低，以下回答仅供参考，请核对原文或转人工确认。
      </div>
      <AnswerBlocks :message="m" />
      <SourceList v-if="m.sources && m.sources.length" :sources="m.sources" />
      <FeedbackBar v-if="m.messageId" :message="m" />
    </template>

    <!-- 兜底：AI 消息既非加载/错误/无结果，又无可渲染内容（如"检索/思考中"被持久化后回灌的半截会话）
         → 优雅提示而非空白气泡；有原问句则给重试。 -->
    <template v-else>
      <div class="text-[15px] text-muted-foreground">这条回答没有内容（可能上次未生成完）。</div>
      <button
        v-if="m.question"
        type="button"
        class="mt-2 flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition hover:bg-secondary hover:text-foreground"
        @click="retry(m)"
      >
        <RotateCw :size="14" :stroke-width="1.75" /> 重试
      </button>
    </template>
    </div>
  </div>
</template>

<style scoped>
/* 来源预览淡入（逐条交错 → "正在它们之中检索"的浮现感）；尊重减弱动效。 */
.src-pop { animation: src-pop .26s ease-out both; }
@keyframes src-pop { from { opacity: 0; transform: translateY(2px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) { .src-pop { animation: none; } }
</style>
