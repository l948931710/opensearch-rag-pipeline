<script setup lang="ts">
import { storeToRefs } from 'pinia'
import { ShieldAlert } from 'lucide-vue-next'
import { useSession } from '@/stores/session'

// P2：外壳占位 + 视图内权限自检（深链 /manage 的非管理员落「无权限」，而非静默跳转）。
// 上传 / 台账 / 审批队列 / 退役在 P4 接入。AppShell 仅在 ready 后渲染，故此处 canManage 已解析。
const { canManage, identity } = storeToRefs(useSession())
</script>

<template>
  <div v-if="!canManage" class="mx-auto flex min-h-full max-w-md flex-col items-center justify-center px-6 text-center">
    <ShieldAlert :size="40" :stroke-width="1.75" class="text-st-busy" />
    <h2 class="mt-4 text-lg font-bold text-foreground">无管理权限</h2>
    <p class="mt-2 text-sm text-muted-foreground">
      知识库管理仅对部门管理员 / 知识库管理员开放。如需上传文档，请联系你的部门管理员。
    </p>
  </div>

  <div v-else class="mx-auto w-full max-w-5xl px-6 py-10">
    <header class="flex items-baseline justify-between border-b border-border pb-4">
      <h1 class="text-xl font-extrabold tracking-tight text-foreground">知识库管理</h1>
      <span class="font-mono text-xs text-muted-foreground">{{ identity?.managedOwnerDepts.join(' · ') || '—' }}</span>
    </header>
    <div class="mt-8 rounded-xl border border-dashed border-border bg-card/60 px-5 py-12 text-center text-sm text-muted-foreground">
      <span class="font-mono text-xs">P4 · 上传 + 台账 + 审批队列 + 退役</span>
    </div>
  </div>
</template>
