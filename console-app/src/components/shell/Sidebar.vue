<script setup lang="ts">
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import { Plus, Search, Library, Sun, Moon, Trash2 } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { useTheme } from '@/composables/useTheme'
import { useAsk } from '@/composables/useAsk'

// Atlas 式侧栏：默认 56px 图标轨，悬停/聚焦展开成 272px 浮层（覆盖内容、不挤压重排）。
// 每行图标放进统一的 56px 居中槽（.rail-ico = size-10，外层 px-2）→ 收起态所有图标恒在轨道中线，
// 展开时图标不动、仅标签淡入，故折叠时图标不会漂移。
const session = useSession()
const { identity, role, canManage } = storeToRefs(session)
const { theme, toggle } = useTheme()
const { activeId, newConversation, switchTo, removeConversation, searchConversations } = useAsk()
const route = useRoute()
const router = useRouter()

const q = ref('')
const convs = computed(() => searchConversations(q.value))
function isActiveConv(id: string) { return route.path === '/' && id === activeId.value }

function onNewChat() { newConversation(); if (route.path !== '/') void router.push('/') }
function onPickConv(id: string) { switchTo(id); if (route.path !== '/') void router.push('/') }
function onDelConv(id: string, e: Event) { e.stopPropagation(); removeConversation(id) }

const ROLE_LABEL: Record<string, string> = { employee: '员工', dept_admin: '部门管理员', kb_admin: '知识库管理员' }
const initial = computed(() => (identity.value?.name || '?').trim().charAt(0) || '?')
const kbLabel = computed(() => (canManage.value ? '知识库管理' : '知识库'))
// 展开淡入：标签/搜索/历史共用
const reveal = 'opacity-0 transition-opacity duration-200 group-hover/sb:opacity-100 group-focus-within/sb:opacity-100'
</script>

<template>
  <!-- 外层占 56px 轨道宽；内层浮层悬停/聚焦展开覆盖内容 -->
  <aside class="relative z-30 w-14 shrink-0">
    <div
      class="group/sb absolute inset-y-0 left-0 flex w-14 flex-col overflow-hidden border-r border-border bg-sidebar
             transition-[width,box-shadow] duration-200 ease-out hover:w-[272px] focus-within:w-[272px]
             hover:shadow-2xl hover:shadow-black/10 focus-within:shadow-2xl focus-within:shadow-black/10"
    >
      <!-- 品牌 -->
      <div class="flex h-14 items-center px-2">
        <span class="grid size-10 shrink-0 place-items-center">
          <span class="grid size-[30px] place-items-center rounded-[9px] bg-accent-strong">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="var(--primary-foreground)"><path d="M12 2.5l1.7 6.1 6.1 1.7-6.1 1.7L12 18.1l-1.7-6.1L4.2 10.3l6.1-1.7z" /></svg>
          </span>
        </span>
        <span class="ml-1 truncate font-serif text-[21px] leading-none tracking-tight text-foreground" :class="reveal">富岭知识库</span>
      </div>

      <!-- 新会话 -->
      <div class="px-2 pt-1">
        <button
          type="button"
          class="flex h-10 w-full items-center rounded-lg border border-border bg-surface text-sm font-medium text-foreground transition hover:border-border-strong hover:bg-panel"
          title="新会话" @click="onNewChat"
        >
          <span class="grid size-10 shrink-0 place-items-center"><Plus :size="18" :stroke-width="2" /></span>
          <span class="truncate" :class="reveal">新会话</span>
        </button>
      </div>

      <!-- 搜索（展开可见） -->
      <div class="px-3 pt-2" :class="reveal">
        <div class="relative">
          <Search :size="14" :stroke-width="1.75" class="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            v-model="q" type="search" placeholder="搜索对话…"
            class="w-full rounded-lg border border-border bg-surface py-1.5 pl-8 pr-2.5 text-sm text-foreground placeholder:text-faint focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/15"
          />
        </div>
      </div>

      <!-- 会话历史 -->
      <nav class="mt-1 min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 py-1" :class="reveal">
        <button
          v-for="c in convs" :key="c.id"
          type="button"
          class="conv-row group/c flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition hover:bg-accent-soft"
          :data-active-item="isActiveConv(c.id) ? '1' : '0'"
          :class="isActiveConv(c.id) ? 'text-accent-text' : 'text-foreground'"
          @click="onPickConv(c.id)"
        >
          <span class="min-w-0 flex-1 truncate">{{ c.title || '新对话' }}</span>
          <span
            class="conv-del grid size-6 shrink-0 place-items-center rounded text-muted-foreground transition hover:bg-st-fail/10 hover:text-st-fail"
            title="删除会话" @click="onDelConv(c.id, $event)"
          >
            <Trash2 :size="13" :stroke-width="1.75" />
          </span>
        </button>
        <p v-if="!convs.length" class="whitespace-nowrap px-2.5 py-6 text-center text-xs text-muted-foreground">
          {{ q ? '无匹配对话' : '点「新会话」开始' }}
        </p>
      </nav>

      <!-- 知识库入口 + 主题 -->
      <div class="space-y-1 border-t border-border px-2 py-2">
        <RouterLink
          to="/manage"
          class="flex h-10 items-center rounded-lg text-muted-foreground transition hover:bg-accent-soft hover:text-foreground"
          active-class="!bg-accent-soft !text-accent-text !font-semibold"
        >
          <span class="grid size-10 shrink-0 place-items-center"><Library :size="19" :stroke-width="1.75" /></span>
          <span class="truncate text-sm font-medium" :class="reveal">{{ kbLabel }}</span>
        </RouterLink>
        <button
          type="button"
          class="flex h-10 w-full items-center rounded-lg text-muted-foreground transition hover:bg-accent-soft hover:text-foreground"
          :title="theme === 'dark' ? '切到亮色' : '切到暗色'" @click="toggle"
        >
          <span class="grid size-10 shrink-0 place-items-center"><component :is="theme === 'dark' ? Sun : Moon" :size="19" :stroke-width="1.75" /></span>
          <span class="text-sm font-medium" :class="reveal">{{ theme === 'dark' ? '亮色模式' : '暗色模式' }}</span>
        </button>
      </div>

      <!-- 账户 -->
      <div class="flex items-center border-t border-border px-2 py-3">
        <span class="grid size-10 shrink-0 place-items-center">
          <span class="grid size-8 place-items-center rounded-full bg-accent-soft text-sm font-semibold text-accent-text">{{ initial }}</span>
        </span>
        <div class="ml-1 min-w-0 flex-1" :class="reveal">
          <div class="truncate text-sm font-semibold text-foreground">{{ identity?.name || '未登录' }}</div>
          <span
            class="mt-0.5 inline-block rounded px-1.5 py-0.5 text-[10px] font-medium"
            :class="canManage ? 'bg-accent-soft text-accent-text' : 'bg-panel text-muted-foreground'"
          >{{ ROLE_LABEL[role] || role }}</span>
        </div>
      </div>
    </div>
  </aside>
</template>
