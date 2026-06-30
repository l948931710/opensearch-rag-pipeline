<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ArrowDown } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { useAsk } from '@/composables/useAsk'
import Thread from '@/components/qa/Thread.vue'
import Composer from '@/components/qa/Composer.vue'

const { identity } = storeToRefs(useSession())
const name = computed(() => identity.value?.name || '')

const { messages, asking, draft, thinking, hotQuestions, ask, stop, loadHotQuestions } = useAsk()
function toggleThinking() { thinking.value = !thinking.value }

const scroller = ref<HTMLElement | null>(null)
const atBottom = ref(true)          // 用户是否贴近底部（决定流式是否跟随；上滚阅读时停跟随）
const NEAR_PX = 80                  // 贴底判定阈值

function refreshAtBottom() {
  const el = scroller.value
  if (el) atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_PX
}
function scrollToBottom(smooth = false) {
  const el = scroller.value
  if (!el) return
  el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  atBottom.value = true
}

// 流式逐 token / 状态变化：仅当用户本就贴底才跟随到底，否则不动（不把正在上翻阅读的人拽回去）。
watch(messages, () => { if (atBottom.value) nextTick(() => scrollToBottom(false)) }, { deep: true })

// 发起提问：用户期望立刻看到自己的问句与作答 → 强制贴底跟随。
function send(preset?: string) { atBottom.value = true; void ask(preset) }

onMounted(() => { if (!hotQuestions.value.length) void loadHotQuestions() })
</script>

<template>
  <div class="flex h-full flex-col">
    <!-- 有消息：线程滚动区 + 底部固定输入（新会话/历史在侧栏） -->
    <template v-if="messages.length">
      <div class="relative min-h-0 flex-1">
        <div ref="scroller" class="h-full overflow-y-auto" @scroll.passive="refreshAtBottom">
          <Thread :messages="messages" />
        </div>
        <!-- 上翻阅读时浮现「回到最新」（贴底时隐藏）；流式跟随不再劫持滚动 -->
        <Transition name="jump">
          <button
            v-if="!atBottom"
            type="button"
            class="absolute bottom-3 left-1/2 z-10 flex -translate-x-1/2 items-center gap-1 rounded-full border border-border bg-card/95 px-3 py-1.5 text-xs text-muted-foreground shadow-sm backdrop-blur transition hover:border-border-strong hover:text-foreground"
            @click="scrollToBottom(true)"
          >
            <ArrowDown :size="13" :stroke-width="2" /> 回到最新
          </button>
        </Transition>
      </div>
      <div class="shrink-0 border-t border-border/60 py-3">
        <Composer v-model="draft" :asking="asking" :has-messages="true" :thinking="thinking" @submit="send()" @stop="stop" @toggle-thinking="toggleThinking" />
      </div>
    </template>

    <!-- 空态：居中问候 + 输入 + 热门问题 -->
    <div v-else class="flex flex-1 flex-col items-center justify-center px-4 pb-20">
      <div class="mb-7 flex items-center gap-3">
        <span class="grid size-9 place-items-center rounded-[10px] bg-accent-strong">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="var(--primary-foreground)" aria-hidden="true" focusable="false"><path d="M12 2.5l1.7 6.1 6.1 1.7-6.1 1.7L12 18.1l-1.7-6.1L4.2 10.3l6.1-1.7z" /></svg>
        </span>
        <span class="font-serif text-[34px] leading-none tracking-tight text-foreground">你好{{ name ? '，' + name : '，同事' }}</span>
      </div>
      <Composer v-model="draft" :asking="asking" :has-messages="false" :thinking="thinking" @submit="send()" @stop="stop" @toggle-thinking="toggleThinking" />
      <div v-if="hotQuestions.length" class="mt-5 flex max-w-2xl flex-wrap justify-center gap-2">
        <button
          v-for="(h, i) in hotQuestions" :key="i"
          type="button"
          class="rounded-full border border-border bg-card px-3.5 py-1.5 text-sm text-foreground transition hover:border-ring hover:bg-panel"
          @click="send(h)"
        >
          {{ h }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 「回到最新」淡入淡出（仅透明度，避免与 -translate-x-1/2 的 transform 冲突）。 */
.jump-enter-active, .jump-leave-active { transition: opacity .18s ease; }
.jump-enter-from, .jump-leave-to { opacity: 0; }
@media (prefers-reduced-motion: reduce) { .jump-enter-active, .jump-leave-active { transition: none; } }
</style>
