<script setup lang="ts">
import { ref } from 'vue'

// P0 验证壳：仅用语义 utilities（bg-background / bg-primary / border-border / bg-st-*），
// 不写任何字面色。能正确渲染 = 三层 token 链路打通、富岭主题生效。
const count = ref(0)
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-background text-foreground p-8">
    <div class="w-full max-w-md rounded-lg border border-border bg-card p-6 shadow-sm">
      <div class="flex items-center gap-2 text-lg font-extrabold tracking-tight">
        <span class="text-primary">✳</span> 富岭知识库
      </div>
      <p class="mt-1 text-sm text-muted-foreground">
        P0 · Vite + Vue + TS + Tailwind v4 脚手架，三层 token 已生效
      </p>

      <div class="mt-5 flex items-center gap-3">
        <button
          class="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground transition hover:opacity-90 active:translate-y-px"
          @click="count++"
        >主按钮 · {{ count }}</button>
        <button
          class="rounded-md border border-input bg-card px-4 py-2 text-sm transition hover:border-primary"
        >次按钮</button>
      </div>

      <div class="mt-5 border-t border-border pt-4">
        <div class="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">状态色 · status tokens</div>
        <div class="mt-2 flex flex-wrap gap-2 text-xs text-foreground">
          <span class="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1"><i class="size-2 rounded-full bg-st-live"></i>已上线</span>
          <span class="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1"><i class="size-2 rounded-full bg-st-busy"></i>处理中</span>
          <span class="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1"><i class="size-2 rounded-full bg-st-warn"></i>待审核</span>
          <span class="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1"><i class="size-2 rounded-full bg-st-fail"></i>失败</span>
        </div>
        <div class="mt-3 font-mono text-xs text-muted-foreground">v3 · 2026-06-25 · DOC_01J9ZXC8K3</div>
      </div>
    </div>
  </div>
</template>
