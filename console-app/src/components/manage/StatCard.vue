<script setup lang="ts">
// 概览看板统一指标卡：图标 + 标签 + 主值 + 可选（强调徽标 / 子行强调值 / 说明 / 整卡边框覆盖）。
// 抽出以消除各看板分区里 4×/2× 的重复卡片标记，并让设计对齐（数据库图标、本月新增徽标、已索引分块子行）集中一处。
defineProps<{
  label: string
  value: string | number
  icon: any
  tone?: string          // 文字/图标色（text-st-live / text-st-busy …），默认 foreground
  hint?: string          // 末行说明（faint）
  box?: string           // 整卡边框/底色覆盖（如待审批橙框）；空 = 常态白卡
  pill?: string          // 强调徽标文本（如 +1,249）；空 = 不显
  pillLabel?: string     // 徽标后说明（如 本月新增）
  subValue?: string      // 子行强调值（如 27,659）
  subLabel?: string      // 子行说明（如 已索引分块）
  loading?: boolean      // 数据未就绪（如 kbStats 尚未返回）：显骨架，避免闪 "0" 误导
}>()
</script>

<template>
  <div class="kb-card rounded-[14px] border p-[15px]" :class="box || 'border-border bg-card'">
    <div class="mb-2.5 flex items-center gap-2">
      <span class="grid size-7 shrink-0 place-items-center rounded-lg bg-accent-soft" :class="tone || 'text-foreground'">
        <component :is="icon" :size="15" :stroke-width="1.75" />
      </span>
      <span class="truncate text-[12.5px] font-medium text-muted-foreground">{{ label }}</span>
    </div>
    <!-- 加载态：骨架条占位（避免在 stats 返回前闪 "0"，与下方分区的「加载中」占位一致） -->
    <template v-if="loading">
      <div class="mt-0.5 h-[24px] w-14 animate-pulse rounded-md bg-border/70" aria-hidden="true" />
      <span class="sr-only">加载中</span>
    </template>
    <template v-else>
      <div class="font-mono text-[26px] font-bold leading-none tracking-tight tabular-nums" :class="tone || 'text-foreground'">{{ value }}</div>
      <div v-if="pill" class="mt-2 flex items-center gap-1.5">
        <span class="rounded-full bg-accent-soft px-[7px] py-px text-[11px] font-semibold text-accent-text">{{ pill }}</span>
        <span v-if="pillLabel" class="text-[11.5px] text-faint">{{ pillLabel }}</span>
      </div>
      <div v-if="subValue" class="mt-1.5 flex items-baseline gap-1.5">
        <span class="font-mono text-[13px] font-bold tabular-nums text-accent-text">{{ subValue }}</span>
        <span v-if="subLabel" class="text-[11.5px] text-muted-foreground">{{ subLabel }}</span>
      </div>
      <div v-if="hint" class="mt-1.5 truncate text-[11.5px] text-faint">{{ hint }}</div>
    </template>
  </div>
</template>
