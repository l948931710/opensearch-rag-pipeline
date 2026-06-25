<script setup lang="ts">
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { MessagesSquare, Library, type LucideIcon } from 'lucide-vue-next'
import { useSession } from '@/stores/session'

// 图标轨侧栏：默认 56px 只显图标，悬停展开成 240px 浮层（绝对定位，覆盖内容、不挤压重排）。
// 导航由路由派生；「管理」仅 canManage 可见（深链另由 ManageView 自检兜底）。
interface NavItem { to: string; label: string; icon: LucideIcon; show: boolean }

const session = useSession()
const { identity, role, canManage } = storeToRefs(session)

const nav = computed<NavItem[]>(() => [
  { to: '/', label: '问答', icon: MessagesSquare, show: true },
  { to: '/manage', label: '知识库管理', icon: Library, show: canManage.value },
].filter((i) => i.show))

const ROLE_LABEL: Record<string, string> = {
  employee: '员工', dept_admin: '部门管理员', kb_admin: '知识库管理员',
}
const initial = computed(() => (identity.value?.name || '?').trim().charAt(0) || '?')
</script>

<template>
  <!-- 外层占住 56px 轨道宽；内层浮层悬停展开覆盖内容 -->
  <aside class="relative z-20 w-14 shrink-0">
    <div
      class="group/sb absolute inset-y-0 left-0 flex w-14 flex-col overflow-hidden border-r border-border
             bg-card transition-[width,box-shadow] duration-200 ease-out hover:w-60 hover:shadow-xl hover:shadow-black/5"
    >
      <!-- 品牌位 -->
      <div class="flex h-14 items-center gap-3 px-3.5">
        <div class="grid size-7 shrink-0 place-items-center rounded-md bg-primary text-sm font-bold text-primary-foreground">富</div>
        <span class="whitespace-nowrap text-sm font-extrabold tracking-tight text-foreground opacity-0 transition-opacity duration-200 group-hover/sb:opacity-100">
          富岭知识库
        </span>
      </div>

      <!-- 导航 -->
      <nav class="mt-2 flex flex-col gap-1 px-2">
        <RouterLink
          v-for="item in nav"
          :key="item.to"
          :to="item.to"
          class="group/it relative flex h-10 items-center gap-3 rounded-lg px-2.5 text-muted-foreground
                 transition-colors hover:bg-secondary hover:text-foreground"
          active-class="!bg-accent !text-accent-foreground"
          exact-active-class=""
        >
          <component :is="item.icon" :size="20" :stroke-width="1.75" class="shrink-0" />
          <span class="whitespace-nowrap text-sm font-medium opacity-0 transition-opacity duration-200 group-hover/sb:opacity-100">
            {{ item.label }}
          </span>
        </RouterLink>
      </nav>

      <div class="flex-1" />

      <!-- 身份位 -->
      <div class="flex items-center gap-3 border-t border-border px-3 py-3">
        <div class="grid size-8 shrink-0 place-items-center rounded-full bg-accent text-sm font-bold text-accent-foreground">{{ initial }}</div>
        <div class="min-w-0 opacity-0 transition-opacity duration-200 group-hover/sb:opacity-100">
          <div class="truncate text-sm font-semibold text-foreground">{{ identity?.name || '未登录' }}</div>
          <div class="truncate font-mono text-[11px] text-muted-foreground">{{ ROLE_LABEL[role] || role }}</div>
        </div>
      </div>
    </div>
  </aside>
</template>
