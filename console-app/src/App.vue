<script setup lang="ts">
import { onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useSession } from '@/stores/session'
import { useAuth } from '@/composables/useAuth'

// P1：唯一在此触发免登 init（修正#6）。store/router 不再各自触发。
const session = useSession()
const { ready, error, identity, role, canManage } = storeToRefs(session)
const { init } = useAuth()
onMounted(() => { void init() })
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-background text-foreground p-8">
    <div class="w-full max-w-md rounded-lg border border-border bg-card p-6 shadow-sm">
      <div class="flex items-center gap-2 text-lg font-extrabold tracking-tight">
        <span class="text-primary">✳</span> 富岭知识库
      </div>

      <p v-if="!ready && !error" class="mt-3 text-sm text-muted-foreground">正在登录…</p>
      <p v-else-if="error" class="mt-3 text-sm text-destructive">{{ error }}</p>

      <template v-else>
        <p class="mt-1 text-sm text-muted-foreground">P1 · 免登 + 会话就绪</p>
        <div class="mt-4 rounded-md border border-border bg-secondary p-4">
          <div class="font-semibold">{{ identity?.name || '—' }}</div>
          <div class="mt-1.5 space-y-0.5 font-mono text-xs text-muted-foreground">
            <div>role · {{ role }}</div>
            <div>canManage · {{ canManage }}</div>
            <div>acl · {{ identity?.aclGroups.join(', ') || '-' }}</div>
          </div>
        </div>
        <p class="mt-3 text-xs text-muted-foreground">token 已保存在内存并从地址栏抹除（防泄露）。</p>
      </template>
    </div>
  </div>
</template>
