<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Brain, ChevronRight } from 'lucide-vue-next'
import type { ChatMessage } from '@/composables/useAsk'

// 「深度思考」思考过程 —— 设计立意：这是模型在你的 SOP 之间推敲的【内心独白】，应当轻盈、流动、转瞬：
// 思考中是一束顺着「思绪轨」流下、顶部渐隐的念头流（最新一念在底、随光标书写）；答案一旦开始，便
// 收束为一行安静的记录（含真实思考耗时），可点开重读。仅当消息带 reasoning 时渲染。
const props = defineProps<{ message: ChatMessage }>()
const m = props.message

const streaming = computed(() =>
  !!m.reasoning && m.html == null && !m.viewBlocks && !m.error && !m.noResult)
const open = computed(() => m.reasoningOpen !== false)
function toggle() { m.reasoningOpen = !open.value }
const durationText = computed(() => (m.reasoningMs ? (m.reasoningMs / 1000).toFixed(1) + 's' : ''))

// 思绪流：新念头到达即滚到底（最新一念始终在视野；配合顶部渐隐 = 旧念头上浮消散）。
const streamEl = ref<HTMLElement | null>(null)
const topFaded = ref(false)
function syncFade() { const el = streamEl.value; if (el) topFaded.value = el.scrollTop > 4 }
watch(() => m.reasoningHtml, () => {
  if (!streaming.value) return
  nextTick(() => { const el = streamEl.value; if (el) { el.scrollTop = el.scrollHeight; syncFade() } })
})
</script>

<template>
  <section class="mb-2.5">
    <!-- 思考中：内心独白流（左思绪轨 + 顶部渐隐 + 末尾光标） -->
    <div v-if="streaming" class="rounded-[12px] bg-secondary/30 py-2.5 pl-3 pr-3">
      <div class="mb-1.5 flex items-center gap-2 pl-2.5">
        <span class="think-orb" aria-hidden="true" />
        <span class="text-[11px] font-medium tracking-[0.04em] text-accent-text">正在思考</span>
      </div>
      <div
        ref="streamEl"
        class="think-stream md md-reason is-streaming max-h-44 overflow-y-auto pl-2.5 text-[12.5px] leading-[1.7] text-muted-foreground"
        :class="{ 'think-faded': topFaded }"
        @scroll.passive="syncFade"
        v-html="m.reasoningHtml"
      />
    </div>

    <!-- 思考完成：退为安静的一行记录（可展开重读），不与答案争注意力 -->
    <template v-else>
      <button
        type="button"
        class="think-toggle group flex w-full items-center gap-1.5 rounded-[9px] py-1 pl-1 pr-2 text-left transition hover:bg-secondary/40"
        :aria-expanded="open"
        @click="toggle"
      >
        <Brain :size="13" :stroke-width="1.85" class="text-faint transition-colors group-hover:text-accent-text" />
        <span class="text-[12px] text-muted-foreground">思考过程</span>
        <span v-if="durationText" class="text-[11px] tabular-nums text-faint">· {{ durationText }}</span>
        <span class="text-faint transition-transform duration-200" :class="open ? 'rotate-90' : ''">
          <ChevronRight :size="13" :stroke-width="2" />
        </span>
      </button>
      <div
        v-show="open"
        class="think-record md md-reason mt-1 pl-3 text-[12.5px] leading-[1.7] text-muted-foreground"
        v-html="m.reasoningHtml"
      />
    </template>
  </section>
</template>

<style scoped>
/* 左「思绪轨」：思考正文靠一条细竖线收束，读作旁白/内心独白而非正文块（思考中偏 accent、记录态偏静）。 */
.think-stream { border-left: 2px solid color-mix(in srgb, var(--accent-text) 30%, transparent); }
.think-record { border-left: 2px solid var(--border); }
/* 顶部渐隐：仅当上方有内容（已滚动）时启用 —— 旧念头在上缘消散，配合自动滚动 = 思绪上浮流动的签名感。 */
.think-faded {
  -webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 18%);
  mask-image: linear-gradient(to bottom, transparent 0, #000 18%);
}
/* 思考「呼吸」灯：比加载三点更安静、更"在想"（缓慢明灭 + 微光晕）。 */
.think-orb {
  width: 7px; height: 7px; border-radius: 9999px; background: var(--accent-text);
  animation: think-breathe 1.8s ease-in-out infinite;
}
@keyframes think-breathe {
  0%, 100% { opacity: .42; transform: scale(.82); box-shadow: 0 0 0 0 transparent; }
  50% { opacity: 1; transform: scale(1); box-shadow: 0 0 7px 1px color-mix(in srgb, var(--accent-text) 28%, transparent); }
}
/* 思考正文段距更紧（区别于正式答案）。 */
.md-reason :deep(p) { margin: 0 0 6px; }
.md-reason :deep(*:last-child) { margin-bottom: 0; }
@media (prefers-reduced-motion: reduce) { .think-orb { animation: none; opacity: .9; } }
</style>
