<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ArrowDown, Sprout } from '@lucide/vue'
import { useSession } from '@/stores/session'
import { useAsk } from '@/composables/useAsk'
import Thread from '@/components/qa/Thread.vue'
import Composer from '@/components/qa/Composer.vue'

const { identity } = storeToRefs(useSession())
const name = computed(() => identity.value?.name || '')
// 问候随时段（挂载时算一次即可，不需要跨小时反应式刷新）。
const h = new Date().getHours()
const daypart = h < 5 ? '晚上好' : h < 11 ? '早上好' : h < 13 ? '中午好' : h < 18 ? '下午好' : '晚上好'

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
// H#71：不再 deep watch 整棵 messages（流式期每 tick 深遍历所有消息只为滚动跟随）。高度变化只由
// 【最后一条】消息的可见字段驱动（流式 html/思考披露条/加载骨架+来源预览/定稿图文/错误/无结果/
// 展开态）+ 消息条数（含切换会话时 messages 整体换源）→ 拼成窄信号串，变了才跟随。
watch(
  () => {
    const list = messages.value
    const l = list[list.length - 1]
    if (!l) return '0'
    return list.length + ':' + (l.html?.length || 0) + ':' + (l.reasoningHtml?.length || 0)
      + ':' + (l.viewBlocks?.length || 0) + ':' + (l.sources?.length || 0)
      + ':' + (l.stageText || '') + ':' + (l.loading ? 1 : 0) + (l.error ? 1 : 0) + (l.noResult ? 1 : 0)
      + (l.sourcesOpen ? 1 : 0) + (l.reasoningOpen ? 1 : 0)
  },
  () => { if (atBottom.value) nextTick(() => scrollToBottom(false)) },
)

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

    <!-- 空态：居中问候（与登录屏同款竖排 lockup）+ 输入 + 热门问题 -->
    <div v-else class="flex flex-1 flex-col items-center justify-center px-4 pb-14">
      <div class="mb-3 grid size-11 place-items-center rounded-[13px] bg-accent-strong shadow-lg shadow-[color-mix(in_srgb,var(--accent)_28%,transparent)]">
        <Sprout :size="24" :stroke-width="1.75" class="text-primary-foreground" aria-hidden="true" />
      </div>
      <h1 class="font-serif text-[34px] leading-none tracking-tight text-foreground">{{ daypart }}{{ name ? '，' + name : '' }}</h1>
      <p class="mb-8 mt-3 text-[13px] text-muted-foreground">答案来自富岭内部文档；可见范围按你的部门权限过滤。</p>
      <Composer v-model="draft" :asking="asking" :has-messages="false" :thinking="thinking" @submit="send()" @stop="stop" @toggle-thinking="toggleThinking" />
      <div v-if="hotQuestions.length" class="mt-7 w-full max-w-3xl px-4 xl:max-w-4xl">
        <p class="mb-2.5 text-center text-[11px] font-bold tracking-[0.08em] text-faint">大家在问</p>
        <div class="flex flex-wrap justify-center gap-2">
          <button
            v-for="(q, i) in hotQuestions" :key="i"
            type="button"
            class="rounded-full border border-border bg-card px-3.5 py-1.5 text-sm text-foreground transition hover:border-ring hover:bg-panel"
            @click="send(q)"
          >
            {{ q }}
          </button>
        </div>
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
