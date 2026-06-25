<script setup lang="ts">
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { MessagesSquare, Library, Sun, Moon, type LucideIcon } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { useTheme } from '@/composables/useTheme'

// 图标轨侧栏（Atlas 风）：默认 56px 只显图标，悬停展开成 240px 浮层（绝对定位，覆盖内容、不挤压重排）。
// 导航由路由派生；「管理」仅 canManage 可见（深链另由 ManageView 自检兜底）。
interface NavItem { to: string; label: string; icon: LucideIcon; show: boolean }

const session = useSession()
const { identity, role, canManage } = storeToRefs(session)
const { theme, toggle } = useTheme()

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
             bg-sidebar transition-[width,box-shadow] duration-200 ease-out hover:w-60 hover:shadow-xl hover:shadow-black/10"
    >
      <!-- 品牌位：绿色 sparkle 标 + 衬线字标 -->
      <div class="flex h-16 items-center gap-2.5 px-3.5">
        <div class="grid size-[30px] shrink-0 place-items-center rounded-[9px] bg-accent-strong">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="var(--primary-foreground)">
            <path d="M12 2.5l1.7 6.1 6.1 1.7-6.1 1.7L12 18.1l-1.7-6.1L4.2 10.3l6.1-1.7z" />
          </svg>
        </div>
        <span class="whitespace-nowrap font-serif text-[21px] leading-none tracking-tight text-foreground opacity-0 transition-opacity duration-200 group-hover/sb:opacity-100">
          富岭知识库
        </span>
      </div>

      <!-- 导航 -->
      <nav class="mt-1 flex flex-col gap-1 px-2">
        <RouterLink
          v-for="item in nav"
          :key="item.to"
          :to="item.to"
          class="group/it relative flex h-10 items-center gap-3 rounded-lg px-2.5 text-muted-foreground
                 transition-colors hover:bg-accent-soft hover:text-foreground"
          active-class="!bg-accent-soft !text-accent-text !font-semibold"
          exact-active-class=""
        >
          <component :is="item.icon" :size="19" :stroke-width="1.75" class="shrink-0" />
          <span class="whitespace-nowrap text-sm font-medium opacity-0 transition-opacity duration-200 group-hover/sb:opacity-100">
            {{ item.label }}
          </span>
        </RouterLink>
      </nav>

      <div class="flex-1" />

      <!-- 主题切换 -->
      <div class="px-2 pb-1">
        <button
          type="button"
          class="flex h-10 w-full items-center gap-3 rounded-lg px-2.5 text-muted-foreground transition-colors hover:bg-accent-soft hover:text-foreground"
          :title="theme === 'dark' ? '切到亮色' : '切到暗色'"
          @click="toggle"
        >
          <component :is="theme === 'dark' ? Sun : Moon" :size="19" :stroke-width="1.75" class="shrink-0" />
          <span class="whitespace-nowrap text-sm font-medium opacity-0 transition-opacity duration-200 group-hover/sb:opacity-100">
            {{ theme === 'dark' ? '亮色模式' : '暗色模式' }}
          </span>
        </button>
      </div>

      <!-- 身份位 -->
      <div class="flex items-center gap-3 border-t border-border px-3 py-3">
        <div class="grid size-8 shrink-0 place-items-center rounded-full bg-accent-soft text-sm font-semibold text-accent-text">{{ initial }}</div>
        <div class="min-w-0 opacity-0 transition-opacity duration-200 group-hover/sb:opacity-100">
          <div class="truncate text-sm font-semibold text-foreground">{{ identity?.name || '未登录' }}</div>
          <div class="truncate font-mono text-[11px] text-muted-foreground">{{ ROLE_LABEL[role] || role }}</div>
        </div>
      </div>
    </div>
  </aside>
</template>
