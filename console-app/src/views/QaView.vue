<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useSession } from '@/stores/session'
import { useAsk } from '@/composables/useAsk'
import Thread from '@/components/qa/Thread.vue'
import Composer from '@/components/qa/Composer.vue'

const { identity } = storeToRefs(useSession())
const name = computed(() => identity.value?.name || '')

const { messages, asking, draft, thinking, hotQuestions, ask, stop, loadHotQuestions } = useAsk()
function toggleThinking() { thinking.value = !thinking.value }

const scroller = ref<HTMLElement | null>(null)
// 流式更新时跟随滚动到底（深 watch 覆盖逐 token 追加 + 状态切换）。
watch(messages, () => nextTick(() => {
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}), { deep: true })

onMounted(() => { if (!hotQuestions.value.length) void loadHotQuestions() })
</script>

<template>
  <div class="flex h-full flex-col">
    <!-- 有消息：线程滚动区 + 底部固定输入（新会话/历史在侧栏） -->
    <template v-if="messages.length">
      <div ref="scroller" class="min-h-0 flex-1 overflow-y-auto">
        <Thread :messages="messages" />
      </div>
      <div class="shrink-0 border-t border-border/60 py-3">
        <Composer v-model="draft" :asking="asking" :has-messages="true" :thinking="thinking" @submit="ask()" @stop="stop" @toggle-thinking="toggleThinking" />
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
      <Composer v-model="draft" :asking="asking" :has-messages="false" :thinking="thinking" @submit="ask()" @stop="stop" @toggle-thinking="toggleThinking" />
      <div v-if="hotQuestions.length" class="mt-5 flex max-w-2xl flex-wrap justify-center gap-2">
        <button
          v-for="(h, i) in hotQuestions" :key="i"
          type="button"
          class="rounded-full border border-border bg-card px-3.5 py-1.5 text-sm text-foreground transition hover:border-ring hover:bg-panel"
          @click="ask(h)"
        >
          {{ h }}
        </button>
      </div>
    </div>
  </div>
</template>
