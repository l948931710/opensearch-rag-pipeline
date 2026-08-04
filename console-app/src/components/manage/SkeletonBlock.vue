<script setup lang="ts">
import { SKEL } from '@/lib/section'

/**
 * 分区级骨架占位。
 *
 * 两条设计约束，都是从实测故障里来的：
 *
 * ① **调用方必须用「请求是否在途」来决定显不显示，绝不能用 `!data`。**
 *    `/api/kb/stats` 返回 404 时 `noteLoadError` 走静默分支，data 与 loadErrors 同时恒空，
 *    `!data && !error` 恒为真——审计实测 4 张 hero 卡骨架转 5 秒仍在转。用 `isLoading(key)`。
 *
 * ② **高度取 SKEL_H 的实测值，不要按 class 猜。** 骨架的作用之一是预留版面：
 *    审计实测 governance 到达时 `main.scrollHeight` 单帧内 +1716px，已在阅读的用户脚下整块位移。
 *    高度对不上就等于没预留。
 *
 * 刻意**不带 role="alert"/"status"**：`ux-gate.spec.ts` 有裸的 `getByRole('alert')` 断言，
 * 多一个同角色元素会 strict-mode violation（轮 1a 踩过一次）。用 aria-busy + sr-only。
 *
 * ⚠️ **sr-only 文案固定为「加载中」，绝不拼进分区名**（与 StatCard.vue:30 一致）。
 * 初版写的是 `${label}加载中`，把受保护的 `manage-node-owner.spec.ts:164`（fdc2648 组织覆盖
 * 重设计硬门）打红了——它用的是裸 `getByText('组织覆盖')`，我的「组织覆盖加载中」同时命中，
 * strict-mode violation。骨架分区自己的 `<header>` 本就会被读屏念到，重复一遍毫无增益。
 * 同一类错误轮 1a 在 role 上犯过一次：**新增元素的可读文本会撞既有裸 locator**。
 */
withDefaults(defineProps<{
  /** 行数 */
  rows?: number
  /** 单行高度（px），取自 SKEL_H 的实测值 */
  height?: number
  /** 每行宽度，制造自然参差；循环取用 */
  widths?: string[]
  testid?: string
}>(), {
  rows: 3,
  height: 28,
  widths: () => ['92%', '76%', '84%'],
})
</script>

<template>
  <div role="presentation" aria-busy="true" :data-testid="testid">
    <div
      v-for="i in rows" :key="i"
      :class="SKEL"
      class="mb-2 last:mb-0"
      :style="{ height: `${height}px`, width: widths[(i - 1) % widths.length] }"
    />
    <span class="sr-only">加载中</span>
  </div>
</template>
