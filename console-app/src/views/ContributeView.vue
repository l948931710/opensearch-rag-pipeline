<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { HelpCircle, Sparkles, CheckCircle2, Users, Plus } from '@lucide/vue'
import { useContribute } from '@/composables/useContribute'
import StatCard from '@/components/manage/StatCard.vue'
import GapList from '@/components/contribute/GapList.vue'
import ContributeModal from '@/components/contribute/ContributeModal.vue'
import ContributionReviewQueue from '@/components/contribute/ContributionReviewQueue.vue'
import MyContributions from '@/components/contribute/MyContributions.vue'
import HeroBoard from '@/components/contribute/HeroBoard.vue'

// 知识贡献：看大家在搜什么、还有哪些问题没人回答 → 提交问答 → 部门管理员采纳后入库。
// 员工 + 管理员通用（审核区仅管理员可见）。AppShell 仅在 ready 后渲染，故身份已解析。
const { gapsSummary, canManage, openModal, loadGaps, loadMine, loadHeroes, loadPending } = useContribute()

const s = computed(() => gapsSummary.value)

onMounted(() => {
  void loadGaps()
  void loadMine()
  void loadHeroes()
  if (canManage.value) void loadPending()
})
</script>

<template>
  <div class="mx-auto w-full max-w-5xl space-y-6 px-6 py-8">
    <header class="flex flex-wrap items-end justify-between gap-3 border-b border-border pb-4">
      <div>
        <h1 class="font-serif text-2xl tracking-tight text-foreground">知识贡献</h1>
        <p class="mt-1 text-sm text-muted-foreground">看看大家在搜什么、还有哪些问题没人回答 —— 你的一次回答，可能帮到很多人。</p>
      </div>
      <button
        type="button"
        class="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground transition hover:opacity-90"
        @click="openModal()"
      >
        <Plus :size="16" :stroke-width="2" /> 贡献知识
      </button>
    </header>

    <!-- 统计卡 -->
    <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <StatCard label="待回答问题" :value="s?.unanswered ?? '—'" :icon="HelpCircle" tone="text-st-busy" hint="等你来贡献" />
      <!-- 批次ε-3 R3 口径修正：标签/hint 对齐真实 SQL 口径——「已采纳」实为 searchable 计数
           （标签混淆双生命周期）；「本月」含已驳回未披露；「本季」实为 90 天滚动窗 -->
      <StatCard label="本月提交" :value="s?.this_month ?? '—'" :icon="Sparkles" hint="含待审核、已驳回" />
      <StatCard label="已入库" :value="s?.answered ?? '—'" :icon="CheckCircle2" tone="text-accent-text" hint="全部时间累计" />
      <StatCard label="贡献者" :value="s?.contributors ?? '—'" :icon="Users" hint="近 90 天有提交" />
    </div>

    <!-- 主区：左缺口列表 / 右审核+我的+英雄榜 -->
    <div class="grid grid-cols-1 items-start gap-[18px] lg:grid-cols-[1.6fr_1fr]">
      <div class="min-w-0">
        <GapList />
      </div>
      <div class="flex flex-col gap-[18px]">
        <ContributionReviewQueue v-if="canManage" />
        <MyContributions />
        <HeroBoard />
      </div>
    </div>

    <ContributeModal />
  </div>
</template>
