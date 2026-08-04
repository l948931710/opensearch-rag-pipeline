<script setup lang="ts">
import { computed } from 'vue'
import { CheckCircle2, AlertTriangle, Info, X } from '@lucide/vue'
import { useToast } from '@/composables/useToast'

/**
 * 全局 toast 宿主。挂在 AppShell 的 ErrorBoundary **外**（与 ConfirmDialog 同位置）——
 * 视图崩了也要还有一条能发出反馈的通道。
 *
 * 三个不显眼但都踩过坑的实现约束：
 *
 * ① **两个独立活区，而不是一个节点动态切 polite↔assertive**：后者在多数屏幕阅读器上不可靠。
 *    成功走 polite（不打断读屏当前朗读），失败走 assertive（用户可能正要做下一步，必须立刻知道）。
 *
 * ② **活区容器恒渲染**（哪怕零条 toast），只 v-for 内部的 <li>。活区若在内容插入的同时才出现，
 *    大多数 AT 不播报。且这里刻意**不用 display:contents** —— 它在部分浏览器版本上会把带语义
 *    角色的元素从无障碍树里摘掉，正好废掉活区。空 <ul> 本身零高度，不占版面。
 *
 * ③ **只用 `aria-live` 基元，不挂 `role="status"` / `role="alert"`**。两者本质是 aria-live 的
 *    便捷写法（且各自隐含一个 aria-atomic，而这里显式要 false），播报行为不因去掉角色而变。
 *    去掉是因为这两个角色**在别处已被占用**：`qa/FeedbackBar.vue:107/130` 各有一个，而活区是
 *    全站常驻的——挂上角色会让问答页出现 2 个 role=alert，把 `ux-gate.spec.ts:309` 里裸的
 *    `getByRole('alert')` 打成 strict-mode violation（全量 e2e 三视口确定性复现过）。
 *    正确的修法是从源头不引入第二个同角色元素，而不是去给既有断言加 .first()。
 *
 * ④ **绝不用 `fixed inset-0`**：toast 是角落定位，本就不该盖满全屏；而且
 *    `tests/manage-node-owner.spec.ts:130` 用的是无 .first() 限定的 `.fixed.inset-0`，
 *    多一个同类元素同样会触发 strict-mode violation。
 *
 * z-[100]：高于 ConfirmDialog 的 z-[90]（当前全应用最高），保证确认框上方仍能看到反馈。
 */
const { toasts, dismissToast } = useToast()

const politeToasts = computed(() => toasts.value.filter((t) => t.tone !== 'error'))
const errorToasts = computed(() => toasts.value.filter((t) => t.tone === 'error'))

const ICON = { success: CheckCircle2, error: AlertTriangle, info: Info } as const
const TONE_CLASS = {
  success: 'border-st-live/35 text-st-live',
  error: 'border-st-fail/35 text-st-fail',
  info: 'border-border text-accent-text',
} as const

function onAction(id: number, run: () => void) {
  run()
  dismissToast(id)
}
</script>

<template>
  <div class="pointer-events-none fixed bottom-4 right-4 z-[100] flex flex-col items-end gap-2">
    <!-- 成功 / 信息：polite，不打断读屏 -->
    <ul
      data-testid="toast-polite" aria-live="polite" aria-atomic="false"
      class="m-0 flex list-none flex-col items-end gap-2 p-0"
    >
      <li
        v-for="t in politeToasts" :key="t.id"
        class="pointer-events-auto flex max-w-[min(360px,calc(100vw-2rem))] items-start gap-2 rounded-xl border bg-card px-3.5 py-2.5 shadow-lg"
        :class="TONE_CLASS[t.tone]"
      >
        <component :is="ICON[t.tone]" :size="15" :stroke-width="1.75" class="mt-px shrink-0" />
        <span class="min-w-0 text-[12.5px] text-foreground">{{ t.text }}</span>
        <button
          v-if="t.action" type="button"
          class="shrink-0 rounded px-1.5 py-0.5 text-[12px] font-semibold text-accent-text transition hover:bg-accent-soft"
          @click="onAction(t.id, t.action.run)"
        >{{ t.action.label }}</button>
        <button
          type="button" :aria-label="`关闭提示：${t.text}`"
          class="-mr-1 grid size-5 shrink-0 place-items-center rounded text-muted-foreground transition hover:bg-panel hover:text-foreground"
          @click="dismissToast(t.id)"
        ><X :size="12" :stroke-width="2" /></button>
      </li>
    </ul>

    <!-- 失败：assertive，立刻打断 -->
    <ul
      data-testid="toast-assertive" aria-live="assertive" aria-atomic="false"
      class="m-0 flex list-none flex-col items-end gap-2 p-0"
    >
      <li
        v-for="t in errorToasts" :key="t.id"
        class="pointer-events-auto flex max-w-[min(360px,calc(100vw-2rem))] items-start gap-2 rounded-xl border border-st-fail/35 bg-st-fail/5 px-3.5 py-2.5 shadow-lg"
      >
        <AlertTriangle :size="15" :stroke-width="1.75" class="mt-px shrink-0 text-st-fail" />
        <span class="min-w-0 text-[12.5px] text-foreground">{{ t.text }}</span>
        <button
          type="button" :aria-label="`关闭提示：${t.text}`"
          class="-mr-1 grid size-5 shrink-0 place-items-center rounded text-muted-foreground transition hover:bg-panel hover:text-foreground"
          @click="dismissToast(t.id)"
        ><X :size="12" :stroke-width="2" /></button>
      </li>
    </ul>
  </div>
</template>
