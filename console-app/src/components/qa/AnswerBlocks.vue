<script setup lang="ts">
import { useAsk, type ChatMessage } from '@/composables/useAsk'

// 答案正文：未定稿（纯文本模式 / 图文帧未到）→ v-html 渲染的 markdown；
// 定稿（content_blocks 帧后）→ 文本块 + 图片块交错；图片签名 URL 过期可点按重签。
const props = defineProps<{ message: ChatMessage }>()
const { resignImage, imgFailed, preview } = useAsk()
const m = props.message
</script>

<template>
  <!-- 未定稿：纯文本逐 token 打字（流式期间末尾显光标，定稿/停止/出错即隐）。
       流式期双节点拆分（Perf-5）：[data-seg] 是 display:contents 的布局透明分段——
       stable=已定稿行前缀（字符串只增不改 → Vue 跳过 patch），tail=推进中尾段（每帧
       唯一被 innerHTML 替换的小节点）。定稿/停止后 _htmlTail 清空 → 回单节点权威渲染。 -->
  <div v-if="!m.viewBlocks" class="md text-[15px] text-foreground" :class="{ 'is-streaming': m.streaming }">
    <template v-if="m._htmlTail != null">
      <div data-seg v-html="m._htmlStable" />
      <div v-if="m._htmlTail" data-seg v-html="m._htmlTail" />
    </template>
    <div v-else data-seg v-html="m.html" />
  </div>

  <!-- 定稿：图文交错。图片 lazy + 占位（Perf-6）：定稿瞬间多图并发加载会把内容逐张往下顶、
       底部跟随滚动错位——加载完成前 figure 先撑固定占位高，@load 后放开并淡入；
       loading=lazy 让视口外的图不抢首屏带宽，decoding=async 解码不占主线程。 -->
  <div v-else class="md text-[15px] text-foreground">
    <template v-for="(b, bi) in m.viewBlocks" :key="bi">
      <div v-if="b.type === 'text'" v-html="b.html" />
      <figure v-else class="vb-fade my-3" :class="{ 'min-h-[140px]': !b.failed && !b.loaded }">
        <img
          v-if="!b.failed"
          :src="b.url" :alt="b.alt || '答案配图'"
          loading="lazy" decoding="async"
          class="max-w-full cursor-zoom-in rounded-lg border border-border transition-opacity duration-200"
          :class="b.loaded ? 'opacity-100' : 'opacity-0'"
          @load="b.loaded = true"
          @click="preview(b)" @error="imgFailed(m, bi)"
        />
        <button
          v-else type="button"
          class="w-full rounded-lg border border-dashed border-border bg-secondary/50 px-4 py-6 text-sm text-muted-foreground transition hover:bg-secondary"
          @click="resignImage(m, bi)"
        >
          {{ b.reloading ? '正在重新加载…' : '图片已过期 · 点按重新加载' }}
        </button>
        <figcaption v-if="b.caption" class="mt-1.5 text-center text-xs text-muted-foreground">{{ b.caption }}</figcaption>
      </figure>
    </template>
  </div>
</template>
