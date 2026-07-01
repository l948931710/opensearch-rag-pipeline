<script setup lang="ts">
import { ref, onErrorCaptured, watch } from 'vue'
import { RotateCw } from 'lucide-vue-next'

// 视图渲染错误兜底：任一子组件渲染/setup 抛错 → 只把主内容区换成兜底提示，
// 不再让整页(含侧栏)白屏。同时把错误栈显给用户(可截图定位根因)并 console.error。
// resetSignal 变化(切路由/切会话)时清错重渲，故导航/新会话即可自愈。
const props = defineProps<{ resetSignal?: unknown }>()
const err = ref<Error | null>(null)

onErrorCaptured((e) => {
  err.value = e as Error
  console.error('[ErrorBoundary] 视图渲染出错(已兜底，防整页空白):', e)
  return false   // 阻断向上传播 → App 根不被卸载、不白屏
})

watch(() => props.resetSignal, () => { if (err.value) err.value = null })
function retry() { err.value = null }
</script>

<template>
  <slot v-if="!err" />
  <div v-else class="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
    <div class="text-base font-semibold text-foreground">这个页面出了点问题</div>
    <p class="max-w-md text-sm text-muted-foreground">已跳过出错内容以免整页空白。可点重试，或从左侧「新会话」/切换其它页面继续。</p>
    <button
      type="button"
      class="mt-1 flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-foreground transition hover:border-border-strong hover:bg-panel"
      @click="retry"
    >
      <RotateCw :size="15" :stroke-width="1.75" /> 重试
    </button>
    <details class="mt-2 w-full max-w-lg text-left">
      <summary class="cursor-pointer text-xs text-faint">技术细节（截图发我可定位根因）</summary>
      <pre class="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-md bg-panel p-2 text-[11px] leading-relaxed text-muted-foreground">{{ err?.stack || err?.message || String(err) }}</pre>
    </details>
  </div>
</template>
