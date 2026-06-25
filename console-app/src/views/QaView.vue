<script setup lang="ts">
import { storeToRefs } from 'pinia'
import { useSession } from '@/stores/session'

// P2：仅外壳占位。问答输入框 / 流式打字机 / 来源卡 / 反馈按钮在 P3 接入。
const { identity } = storeToRefs(useSession())
</script>

<template>
  <div class="mx-auto flex min-h-full w-full max-w-3xl flex-col items-center justify-center px-6 py-16 text-center">
    <div class="grid size-14 place-items-center rounded-2xl bg-accent text-2xl text-accent-foreground">✳</div>
    <h1 class="mt-6 text-2xl font-extrabold tracking-tight text-foreground">
      你好{{ identity?.name ? '，' + identity.name : '' }}
    </h1>
    <p class="mt-2 text-sm text-muted-foreground">从富岭自有文档里找答案 —— SOP、U8+、行政与 FAQ。</p>

    <div class="mt-8 w-full rounded-xl border border-dashed border-border bg-card/60 px-5 py-8 text-sm text-muted-foreground">
      <span class="font-mono text-xs">P3 · 问答输入 + 流式回答 + 来源 + 反馈</span>
    </div>
  </div>
</template>
