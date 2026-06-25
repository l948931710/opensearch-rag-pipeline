<script setup lang="ts">
import Sidebar from './Sidebar.vue'

// 应用外壳：左图标轨 + 右内容区。router-view 只在外壳内（= ready 之后）渲染，
// 故视图挂载时身份已就绪，无需在路由守卫里等待 / 触发免登。
</script>

<template>
  <div class="relative flex h-[100dvh] overflow-hidden bg-background text-foreground">
    <Sidebar />
    <main class="min-w-0 flex-1 overflow-y-auto">
      <RouterView v-slot="{ Component }">
        <Transition name="fade" mode="out-in">
          <component :is="Component" />
        </Transition>
      </RouterView>
    </main>
  </div>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active { transition: opacity 0.15s ease; }
.fade-enter-from,
.fade-leave-to { opacity: 0; }
</style>
