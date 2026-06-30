<script setup lang="ts">
import { useAsk, type ChatMessage } from '@/composables/useAsk'

// 答案正文：未定稿（纯文本模式 / 图文帧未到）→ v-html 渲染的 markdown；
// 定稿（content_blocks 帧后）→ 文本块 + 图片块交错；图片签名 URL 过期可点按重签。
const props = defineProps<{ message: ChatMessage }>()
const { resignImage, imgFailed, preview } = useAsk()
const m = props.message
</script>

<template>
  <!-- 未定稿：纯文本逐 token 打字（流式期间末尾显光标，定稿/停止/出错即隐） -->
  <div v-if="!m.viewBlocks" class="md text-[15px] text-foreground" :class="{ 'is-streaming': m.streaming }" v-html="m.html" />

  <!-- 定稿：图文交错 -->
  <div v-else class="md text-[15px] text-foreground">
    <template v-for="(b, bi) in m.viewBlocks" :key="bi">
      <div v-if="b.type === 'text'" v-html="b.html" />
      <figure v-else class="vb-fade my-3">
        <img
          v-if="!b.failed"
          :src="b.url" :alt="b.alt || '答案配图'"
          class="max-w-full cursor-zoom-in rounded-lg border border-border"
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
