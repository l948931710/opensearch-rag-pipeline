<script setup lang="ts">
import { onMounted, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { useSession } from '@/stores/session'
import { useAuth, hasPendingVersion } from '@/composables/useAuth'
import { debugEnabled } from '@/lib/diag'
import AppShell from '@/components/shell/AppShell.vue'
import DebugPanel from '@/components/DebugPanel.vue'

const debug = debugEnabled()   // ?debug=1（不被 scrubUrl 抹除）

// 唯一在此触发免登 init（修正#6）。store/router 不再各自触发。
// 三态：登录中（全屏加载）/ 失败（全屏错误，多为非钉钉环境）/ 就绪（进应用外壳）。
const session = useSession()
const { ready, error } = storeToRefs(session)
const { init } = useAuth()
const router = useRouter()
onMounted(() => { void init() })

// 升版深链（小程序「上传新版本」?doc_id=...）：就绪后若有待处理升版，路由到 /manage 让 ManageView 消费。
watch(ready, (r) => { if (r && hasPendingVersion() && session.canManage) void router.push('/manage') }, { immediate: true })
</script>

<template>
  <!-- 就绪：进外壳（侧栏 + 路由内容）。router-view 只在此分支内，故视图挂载时身份已解析。 -->
  <AppShell v-if="ready" />

  <!-- 未就绪：全屏品牌 + 登录中 / 错误（居中径向绿光） -->
  <div v-else class="relative flex min-h-[100dvh] items-center justify-center overflow-hidden p-8 text-foreground">
    <!-- 双层径向光：上方主光晕 + 底部暖光，呼应 Atlas 签名底 -->
    <div
      class="pointer-events-none absolute inset-0"
      style="background:
        radial-gradient(620px 440px at 50% 34%, color-mix(in srgb, var(--accent) 16%, transparent) 0%, transparent 68%),
        radial-gradient(900px 500px at 50% 120%, color-mix(in srgb, var(--accent) 7%, transparent) 0%, transparent 60%);"
    />
    <div class="relative w-full max-w-sm text-center">
      <div class="mx-auto grid size-14 place-items-center rounded-2xl bg-accent-strong shadow-lg shadow-[color-mix(in_srgb,var(--accent)_35%,transparent)]">
        <svg width="30" height="30" viewBox="0 0 24 24" fill="var(--primary-foreground)"><path d="M12 2.5l1.7 6.1 6.1 1.7-6.1 1.7L12 18.1l-1.7-6.1L4.2 10.3l6.1-1.7z" /></svg>
      </div>
      <div class="mt-5 font-serif text-3xl tracking-tight">富岭知识库</div>

      <p v-if="!error" class="mt-3 text-sm text-muted-foreground">正在登录…</p>
      <p v-else class="mx-auto mt-3 max-w-xs text-sm text-destructive">{{ error }}</p>
    </div>
  </div>

  <!-- ?debug=1：诊断面板（覆盖在三态之上，登录失败也可见） -->
  <DebugPanel v-if="debug" />
</template>
