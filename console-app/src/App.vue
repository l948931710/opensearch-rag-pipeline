<script setup lang="ts">
import { onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useSession } from '@/stores/session'
import { useAuth } from '@/composables/useAuth'
import AppShell from '@/components/shell/AppShell.vue'

// 唯一在此触发免登 init（修正#6）。store/router 不再各自触发。
// 三态：登录中（全屏加载）/ 失败（全屏错误，多为非钉钉环境）/ 就绪（进应用外壳）。
const session = useSession()
const { ready, error } = storeToRefs(session)
const { init } = useAuth()
onMounted(() => { void init() })
</script>

<template>
  <!-- 就绪：进外壳（侧栏 + 路由内容）。router-view 只在此分支内，故视图挂载时身份已解析。 -->
  <AppShell v-if="ready" />

  <!-- 未就绪：全屏品牌 + 登录中 / 错误 -->
  <div v-else class="flex min-h-[100dvh] items-center justify-center bg-background p-8 text-foreground">
    <div class="w-full max-w-sm text-center">
      <div class="mx-auto grid size-12 place-items-center rounded-xl bg-primary text-lg font-bold text-primary-foreground">富</div>
      <div class="mt-4 text-base font-extrabold tracking-tight">富岭知识库</div>

      <p v-if="!error" class="mt-3 text-sm text-muted-foreground">正在登录…</p>
      <p v-else class="mx-auto mt-3 max-w-xs text-sm text-destructive">{{ error }}</p>
    </div>
  </div>
</template>
