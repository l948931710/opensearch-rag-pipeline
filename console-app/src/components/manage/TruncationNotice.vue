<script setup lang="ts">
/**
 * 硬 LIMIT 队列的「结果已截断」提示（P3-3，2026-08-04）。
 *
 * 六个列表端点（审批 / 授权申请 / 已授权 / 节点候选 / 我的申请 / 已忽略缺口）都是
 * 「SQL 硬 LIMIT N + 响应只有 items」的形态：超过 N 时使用者看到的「就这些」只是前 N 条，
 * **且无从知道**。后端改成 `LIMIT N+1` 取探针行并回 `truncated`，这里负责让它可见。
 *
 * 抽成公共组件而不是六段 `<p v-if>`：文案与样式只有一处，不会六路各自漂移；
 * `data-testid` 统一为 `queue-truncated`，硬门可以一条选择器覆盖全部队列。
 *
 * ⚠️ **刻意不给「加载更多」**：真分页需要稳定排序键（`DATETIME` 只到秒，OFFSET 翻页
 * 会漏行/重行——已在真库实测，见 `d2c8e12`），属设计变更，另议。
 */
defineProps<{
  /** 后端回的 truncated。false/undefined 时整块不渲染。 */
  when?: boolean
  /** 上限条数，用于把话说具体（「仅显示前 100 条」）。 */
  cap: number
  /** 该队列的名字，如「待审批」。 */
  label: string
  /** 收窄建议，因队列而异（按部门筛选 / 先处理完当前批 …）。 */
  hint?: string
}>()
</script>

<template>
  <p v-if="when" data-testid="queue-truncated"
     class="ml-0.5 mt-2 text-[11.5px] text-st-warn">
    {{ label }}超过 {{ cap }} 条，此处仅显示前 {{ cap }} 条{{ hint ? ' —— ' + hint : '。' }}
  </p>
</template>
