<script setup lang="ts">
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import { Plus, Search, Library, Lightbulb, Sun, Moon, Trash2 } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { useTheme } from '@/composables/useTheme'
import { useAsk } from '@/composables/useAsk'
import { useKb } from '@/composables/useKb'
import { useContribute } from '@/composables/useContribute'
import { ROLE_LABEL } from '@/lib/kb'

// Atlas 式侧栏：默认 56px 图标轨，悬停/聚焦展开成 272px 浮层（覆盖内容、不挤压重排）。
// 每行图标放进统一的 56px 居中槽（size-10，外层 px-2）→ 收起态所有图标恒在轨道中线，
// 展开时图标不动、仅标签淡入，故折叠时图标不漂移。
// 收起态视觉对齐：每个"可见标记"再统一成 32px 圆角格（size-8）—— 品牌/头像为 accent 实底，
// 知识库/主题为淡底格（展开转透明），避免裸图标(19px)比绿块(32px)缩进而显得错位。
const session = useSession()
const { identity, role, canManage } = storeToRefs(session)
const { theme, toggle } = useTheme()
const { activeId, newConversation, switchTo, removeConversation, searchConversations } = useAsk()
const { reviewCount } = useKb()   // 待你审核数（红点/角标）；App.vue 在 ready 后已预加载，故入口红点即时可见
const { reviewCount: contribReviewCount } = useContribute()   // 待审核的知识贡献数（管理员）
const route = useRoute()
const router = useRouter()

const q = ref('')
const convs = computed(() => searchConversations(q.value))
const onManage = computed(() => route?.path === '/manage')
const onContribute = computed(() => route?.path === '/contribute')
function isActiveConv(id: string) { return route?.path === '/' && id === activeId.value }

function onNewChat() { newConversation(); if (route.path !== '/') void router.push('/') }
function onPickConv(id: string) { switchTo(id); if (route.path !== '/') void router.push('/') }
function onDelConv(id: string, e: Event) { e.stopPropagation(); removeConversation(id) }

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
          <span class="grid size-8 place-items-center rounded-[10px] bg-accent-strong">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="var(--primary-foreground)" aria-hidden="true" focusable="false"><path d="M12 2.5l1.7 6.1 6.1 1.7-6.1 1.7L12 18.1l-1.7-6.1L4.2 10.3l6.1-1.7z" /></svg>
          </span>
        </span>
        <span class="truncate font-serif text-[21px] leading-none tracking-tight text-foreground" :class="reveal">富岭知识库</span>
      </div>

      <!-- 新会话 -->
      <div class="px-2 pt-1">
        <button
          type="button"
          class="flex h-10 w-full items-center rounded-lg border border-border bg-surface text-sm font-medium text-foreground transition hover:border-border-strong hover:bg-panel"
          title="新会话" aria-label="新会话" @click="onNewChat"
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
            class="w-full rounded-md border border-input bg-surface py-1.5 pl-8 pr-2.5 text-sm text-foreground placeholder:text-faint focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/15"
          />
        </div>
      </div>

      <!-- 会话历史 -->
      <nav class="mt-1 min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 py-1" :class="reveal">
        <div v-for="c in convs" :key="c.id" class="conv-row relative">
          <button
            type="button"
            class="flex w-full items-center rounded-lg px-2.5 py-2 pr-9 text-left text-sm transition hover:bg-accent-soft"
            :data-active-item="isActiveConv(c.id) ? '1' : '0'"
            :class="isActiveConv(c.id) ? 'text-accent-text' : 'text-foreground'"
            @click="onPickConv(c.id)"
          >
            <span class="min-w-0 flex-1 truncate">{{ c.title || '新对话' }}</span>
          </button>
          <button
            type="button"
            class="conv-del absolute right-1.5 top-1/2 grid size-6 -translate-y-1/2 place-items-center rounded text-muted-foreground transition hover:bg-st-fail/10 hover:text-st-fail"
            :aria-label="`删除会话：${c.title || '新对话'}`" @click.stop="onDelConv(c.id, $event)"
          >
            <Trash2 :size="13" :stroke-width="1.75" />
          </button>
        </div>
        <p v-if="!convs.length" class="whitespace-nowrap px-2.5 py-6 text-center text-xs text-muted-foreground">
          {{ q ? '无匹配对话' : '点「新会话」开始' }}
        </p>
      </nav>

      <!-- 知识贡献 + 知识库入口 + 主题 -->
      <div class="space-y-1 border-t border-border px-2 py-2">
        <RouterLink
          to="/contribute"
          class="flex h-10 items-center rounded-lg text-muted-foreground transition hover:text-foreground group-hover/sb:hover:bg-accent-soft"
          active-class="!text-accent-text !font-semibold"
        >
          <span class="relative grid size-10 shrink-0 place-items-center">
            <span class="grid size-8 place-items-center rounded-[10px] border transition-colors"
                  :class="onContribute ? 'border-transparent bg-accent-soft' : 'border-border bg-surface group-hover/sb:!border-transparent group-hover/sb:!bg-transparent'"><Lightbulb :size="19" :stroke-width="1.75" /></span>
            <span
              v-if="canManage && contribReviewCount"
              class="absolute right-1.5 top-1.5 size-2 rounded-full bg-st-warn ring-2 ring-sidebar transition-opacity group-hover/sb:opacity-0 group-focus-within/sb:opacity-0"
              aria-hidden="true"
            />
          </span>
          <span class="truncate text-sm font-medium" :class="reveal">知识贡献</span>
          <span
            v-if="canManage && contribReviewCount"
            class="ml-auto mr-1 grid h-[18px] min-w-[18px] shrink-0 place-items-center rounded-full bg-st-warn px-1.5 text-[10px] font-bold tabular-nums text-white"
            :class="reveal" :aria-label="`待审核贡献 ${contribReviewCount} 项`"
          >{{ contribReviewCount }}</span>
        </RouterLink>
        <RouterLink
          to="/manage"
          class="flex h-10 items-center rounded-lg text-muted-foreground transition hover:text-foreground group-hover/sb:hover:bg-accent-soft"
          active-class="!text-accent-text !font-semibold"
        >
          <span class="relative grid size-10 shrink-0 place-items-center">
            <span class="grid size-8 place-items-center rounded-[10px] border transition-colors"
                  :class="onManage ? 'border-transparent bg-accent-soft' : 'border-border bg-surface group-hover/sb:!border-transparent group-hover/sb:!bg-transparent'"><Library :size="19" :stroke-width="1.75" /></span>
            <!-- 收起态：图标角红点；展开态淡出（换成右侧数字角标） -->
            <span
              v-if="canManage && reviewCount"
              class="absolute right-1.5 top-1.5 size-2 rounded-full bg-st-busy ring-2 ring-sidebar transition-opacity group-hover/sb:opacity-0 group-focus-within/sb:opacity-0"
              aria-hidden="true"
            />
          </span>
          <span class="truncate text-sm font-medium" :class="reveal">{{ kbLabel }}</span>
          <!-- 展开态：右侧待审核数字 -->
          <span
            v-if="canManage && reviewCount"
            class="ml-auto mr-1 grid h-[18px] min-w-[18px] shrink-0 place-items-center rounded-full bg-st-busy px-1.5 text-[10px] font-bold tabular-nums text-white"
            :class="reveal" :aria-label="`待审核 ${reviewCount} 项`"
          >{{ reviewCount }}</span>
        </RouterLink>
        <button
          type="button"
          class="flex h-10 w-full items-center rounded-lg text-muted-foreground transition hover:text-foreground group-hover/sb:hover:bg-accent-soft"
          :title="theme === 'dark' ? '切到亮色' : '切到暗色'" :aria-label="theme === 'dark' ? '切到亮色' : '切到暗色'" @click="toggle"
        >
          <span class="grid size-10 shrink-0 place-items-center">
            <span class="grid size-8 place-items-center rounded-[10px] border border-border bg-surface transition-colors group-hover/sb:!border-transparent group-hover/sb:!bg-transparent"><component :is="theme === 'dark' ? Sun : Moon" :size="19" :stroke-width="1.75" /></span>
          </span>
          <span class="text-sm font-medium" :class="reveal">{{ theme === 'dark' ? '亮色模式' : '暗色模式' }}</span>
        </button>
      </div>

      <!-- 账户 -->
      <div class="flex items-center border-t border-border px-2 py-3">
        <span class="grid size-10 shrink-0 place-items-center">
          <span class="grid size-8 place-items-center rounded-full bg-accent-soft text-sm font-semibold text-accent-text">{{ initial }}</span>
        </span>
        <div class="min-w-0 flex-1" :class="reveal">
          <div class="truncate text-sm font-semibold text-foreground">{{ identity?.name || '未登录' }}</div>
          <span
            class="mt-0.5 inline-block whitespace-nowrap rounded px-1.5 py-0.5 text-[10px] font-medium"
            :class="canManage ? 'bg-accent-soft text-accent-text' : 'bg-panel text-muted-foreground'"
          >{{ ROLE_LABEL[role] || role }}</span>
        </div>
      </div>
    </div>
  </aside>
</template>
