<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { Bell } from '@lucide/vue'
import { useNotices } from '@/composables/useNotices'

// 站内通知铃铛（文档升版提醒）。侧栏图标轨的一员，形态与「知识库」入口同构：
// 收起态=图标 + 角标；展开态=标签 + 右侧数字。
//
// ⚠️ UI 静默红线（Sam 2026-08-04）——改动前先读：
//   ① 无未读 = **零装饰**：灰铃铛，不出红点、不出数字，与没有这个功能时视觉一致；
//   ② 有未读 = 只出数字角标（99+ 封顶）；
//   ③ **绝不** toast / 绝不自动展开面板 / 绝不浏览器通知 / 绝不在内容区插横幅；
//   ④ 点击才展开，点面板外或 Esc 即收。
// 这些不是样式偏好，是需求本身的一部分：通知的价值在"需要时找得到"，不在"跳出来打断"。
const props = defineProps<{ reveal?: string }>()
const { items, unreadCount, unreadCapped, loading, load, markRead } = useNotices()

const open = ref(false)
const rootEl = ref<HTMLElement | null>(null)

function onDocClick(e: MouseEvent) {
  if (!open.value) return
  if (rootEl.value && !rootEl.value.contains(e.target as Node)) open.value = false
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') open.value = false
}
function bind() {
  document.addEventListener('click', onDocClick, true)
  document.addEventListener('keydown', onKey)
}
function unbind() {
  document.removeEventListener('click', onDocClick, true)
  document.removeEventListener('keydown', onKey)
}
onUnmounted(unbind)

async function toggle() {
  open.value = !open.value
  if (open.value) {
    bind()
    await load(true)     // 打开时强制现拉一次（用户此刻的意图就是"看看有什么"）
  } else {
    unbind()
  }
}

const badge = computed(() => (unreadCapped.value ? '99+' : String(unreadCount.value)))
const hasUnread = computed(() => unreadCount.value > 0)

function fmtDate(s?: string | null) {
  if (!s) return ''
  const d = new Date(s.replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getMonth() + 1}月${d.getDate()}日`
}
</script>

<template>
  <div ref="rootEl" class="relative">
    <button
      type="button"
      class="flex h-10 w-full items-center rounded-lg text-muted-foreground transition hover:text-foreground group-hover/sb:hover:bg-accent-soft"
      :aria-label="hasUnread ? `通知，${unreadCount} 条未读` : '通知'"
      :aria-expanded="open"
      @click="toggle"
    >
      <span class="relative grid size-10 shrink-0 place-items-center">
        <span
          class="grid size-8 place-items-center rounded-[10px] border transition-colors"
          :class="open
            ? 'border-transparent bg-accent-soft'
            : 'border-border bg-surface group-hover/sb:!border-transparent group-hover/sb:!bg-transparent'"
        ><Bell :size="19" :stroke-width="1.75" /></span>
        <!-- 收起态：图标角红点（仅有未读时；展开侧栏时淡出，换成右侧数字） -->
        <span
          v-if="hasUnread"
          class="absolute right-1.5 top-1.5 size-2 rounded-full bg-st-busy ring-2 ring-sidebar transition-opacity group-hover/sb:opacity-0 group-focus-within/sb:opacity-0"
          aria-hidden="true"
        />
      </span>
      <span class="truncate text-sm font-medium" :class="props.reveal">通知</span>
      <!-- 展开态：右侧未读数字。无未读时整个角标不渲染 = 零装饰 -->
      <span
        v-if="hasUnread"
        class="ml-auto mr-1 grid h-[18px] min-w-[18px] shrink-0 place-items-center rounded-full bg-st-busy px-1.5 text-[10px] font-bold tabular-nums text-white"
        :class="props.reveal" :aria-label="`${badge} 条未读`"
      >{{ badge }}</span>
    </button>

    <!-- 下拉面板：点击才出现，绝不自动展开 -->
    <div
      v-if="open"
      class="absolute bottom-0 left-[calc(100%+8px)] z-50 w-80 rounded-xl border border-border bg-surface p-2 shadow-lg"
      role="dialog" aria-label="通知"
    >
      <div class="flex items-center justify-between px-2 py-1.5">
        <span class="text-sm font-semibold text-foreground">通知</span>
        <button
          v-if="hasUnread"
          type="button"
          class="rounded px-1.5 py-0.5 text-xs text-muted-foreground transition hover:bg-accent-soft hover:text-accent-text"
          @click="markRead()"
        >全部已读</button>
      </div>
      <!-- 加载四态：loading / 空 / 有内容（错误态由 useNotices 兜底成空，不在 UI 上喊） -->
      <div v-if="loading && !items.length" class="px-2 py-6 text-center text-xs text-muted-foreground">
        加载中…
      </div>
      <div v-else-if="!items.length" class="px-2 py-6 text-center text-xs text-muted-foreground">
        暂无通知
      </div>
      <ul v-else class="max-h-80 overflow-y-auto">
        <li v-for="it in items" :key="it.id">
          <button
            type="button"
            class="flex w-full items-start gap-2 rounded-lg px-2 py-2 text-left transition hover:bg-accent-soft"
            @click="markRead([it.id])"
          >
            <span
              class="mt-1.5 size-1.5 shrink-0 rounded-full"
              :class="it.state === 'PENDING' ? 'bg-st-busy' : 'bg-transparent'"
              aria-hidden="true"
            />
            <span class="min-w-0 flex-1">
              <span class="block truncate text-sm text-foreground">{{ it.title }}</span>
              <span class="mt-0.5 block text-xs text-muted-foreground">
                已更新<template v-if="it.version_no"> 至 v{{ it.version_no }}</template>
                <template v-if="fmtDate(it.created_at)"> · {{ fmtDate(it.created_at) }}</template>
              </span>
            </span>
          </button>
        </li>
      </ul>
    </div>
  </div>
</template>
