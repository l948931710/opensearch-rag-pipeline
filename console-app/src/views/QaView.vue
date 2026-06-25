<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { SquarePen } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { useAsk } from '@/composables/useAsk'
import Thread from '@/components/qa/Thread.vue'
import Composer from '@/components/qa/Composer.vue'

const { identity } = storeToRefs(useSession())
const name = computed(() => identity.value?.name || '')

const { messages, asking, draft, hotQuestions, ask, stop, resetThread, loadHotQuestions } = useAsk()

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
    <!-- 有消息：顶部「新会话」+ 线程滚动区 + 底部固定输入 -->
    <template v-if="messages.length">
      <div class="flex shrink-0 items-center justify-end border-b border-border/60 px-4 py-2">
        <button
          type="button"
          class="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs text-muted-foreground transition hover:bg-secondary hover:text-foreground"
          title="新会话" @click="resetThread()"
        >
          <SquarePen :size="14" :stroke-width="1.75" /> 新会话
        </button>
      </div>
      <div ref="scroller" class="min-h-0 flex-1 overflow-y-auto">
        <Thread :messages="messages" />
      </div>
      <div class="shrink-0 border-t border-border/60 py-3">
        <Composer v-model="draft" :asking="asking" :has-messages="true" @submit="ask()" @stop="stop" />
      </div>
    </template>

    <!-- 空态：居中问候 + 输入 + 热门问题 -->
    <div v-else class="flex flex-1 flex-col items-center justify-center px-4 pb-20">
      <div class="mb-7 flex items-center gap-2.5 text-2xl font-extrabold tracking-tight text-foreground">
        <span class="text-primary">✳</span> 你好{{ name ? '，' + name : '，同事' }}
      </div>
      <Composer v-model="draft" :asking="asking" :has-messages="false" @submit="ask()" @stop="stop" />
      <div v-if="hotQuestions.length" class="mt-5 flex max-w-2xl flex-wrap justify-center gap-2">
        <button
          v-for="(h, i) in hotQuestions" :key="i"
          type="button"
          class="rounded-full border border-border bg-card px-3.5 py-1.5 text-sm text-foreground transition hover:border-ring hover:bg-secondary"
          @click="ask(h)"
        >
          {{ h }}
        </button>
      </div>
    </div>
  </div>
</template>
