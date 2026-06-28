<script setup lang="ts">
import Sidebar from './Sidebar.vue'

// 应用外壳：左图标轨 + 右内容区。router-view 只在外壳内（= ready 之后）渲染，
// 故视图挂载时身份已就绪，无需在路由守卫里等待 / 触发免登。
</script>

<template>
  <div class="relative flex h-[100dvh] overflow-hidden bg-background text-foreground">
    <Sidebar />
    <!-- scrollbar-gutter:stable 常驻滚动条槽位：避免短内容页（如成员管理）无滚动条时，
         mx-auto 居中内容相对长页（概览/文档管理）整体右移 ~半个滚动条宽 → 切 tab 不再位移。 -->
    <main class="min-w-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
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
