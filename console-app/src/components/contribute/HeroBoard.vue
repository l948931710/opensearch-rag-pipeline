<script setup lang="ts">
import { computed, ref } from 'vue'
import { Trophy } from '@lucide/vue'
import { useSession } from '@/stores/session'
import { useContribute } from '@/composables/useContribute'

// 总积分榜（2026-08-01，Sam 拍板预览权重 10/1/3）：score=10×采纳+1×引用+3×有效反馈。
// 构成随行副行展示（可审计——任何人质疑都能指到构成与口径定义）；旧后端无 score 键时
// 回退按 count 展示（副行自隐）。正式积分换算以《AI参与奖励管理办法》定稿为准。
const { heroes, deptHeroes, myDeptHeroes, myDeptName } = useContribute()
const hasScore = computed(() => heroes.value.some((h) => typeof h.score === 'number'))
// 三榜切换（2026-08-01）：全公司个人 / 本部门个人 / 部门榜（同分按一级部门求和）。
// 组织快照缺失 → 后端回空 → tab 栏自隐（只剩全公司，与旧版观感一致）。
type Tab = 'company' | 'mydept' | 'dept'
const tab = ref<Tab>('company')
const hasOrgBoards = computed(() => deptHeroes.value.length > 0 || myDeptHeroes.value.length > 0)
const personList = computed(() => (tab.value === 'mydept' ? myDeptHeroes.value : heroes.value))
const TABS = computed(() => [
  { key: 'company' as Tab, label: '全公司' },
  { key: 'mydept' as Tab, label: myDeptName.value ? `本部门 · ${myDeptName.value}` : '本部门' },
  { key: 'dept' as Tab, label: '部门榜' },
])
const me = computed(() => useSession().identity?.userId || '')
// 名次奖牌调：金（琥珀）/ 银（灰）/ 铜（暖橙，借调 --c-str），4 名以后素灰。
const RANK_TONE: Record<number, string> = {
  1: 'bg-st-busy/15 text-st-busy',
  2: 'bg-panel text-muted-foreground',
  3: 'bg-[color-mix(in_srgb,var(--c-str)_13%,transparent)] text-[var(--c-str)]',
}
function rankCls(r: number) { return RANK_TONE[r] || 'bg-panel text-faint' }
function initial(name: string) { return (name || '?').trim().charAt(0) || '?' }
</script>

<template>
  <!-- 卡头已带图标+标题，不再另设分区眉标 -->
  <section v-if="heroes.length">
    <div class="overflow-hidden rounded-[15px] border border-border bg-card">
      <div class="flex flex-wrap items-center gap-2.5 border-b border-border px-[18px] py-3">
        <Trophy :size="16" :stroke-width="1.75" class="text-st-warn" />
        <span class="text-sm font-semibold text-foreground">{{ hasScore ? '总积分榜' : '英雄榜' }}</span>
        <span v-if="hasScore" class="rounded bg-panel px-1.5 py-px text-[10.5px] text-faint"
              title="预览口径：10 分/篇采纳 · 1 分/次引用 · 3 分/条有效反馈；正式积分以《AI参与奖励管理办法》定稿为准">预览口径</span>
        <div class="flex-1" />
        <div v-if="hasScore && hasOrgBoards" class="flex gap-1" role="tablist" aria-label="榜单维度">
          <button
            v-for="t in TABS" :key="t.key" type="button" role="tab" :aria-selected="tab === t.key"
            class="rounded-full border px-2.5 py-0.5 text-[11px] transition"
            :class="tab === t.key ? 'border-accent-strong bg-accent-soft font-medium text-accent-text' : 'border-border text-muted-foreground hover:border-ring'"
            :data-testid="`hero-tab-${t.key}`"
            @click="tab = t.key"
          >{{ t.label }}</button>
        </div>
      </div>
      <!-- 部门榜（同分按一级部门求和；members=有积分的参与人数） -->
      <template v-if="tab === 'dept'">
        <div
          v-for="d in deptHeroes" :key="d.dept_id"
          class="flex items-center gap-3 border-t border-border px-[18px] py-2.5 first:border-t-0"
          data-testid="hero-dept-row"
        >
          <span class="grid size-6 shrink-0 place-items-center rounded-md font-mono text-[12px] font-bold tabular-nums" :class="rankCls(d.rank)">{{ d.rank }}</span>
          <div class="min-w-0 flex-1">
            <div class="truncate text-[13px] font-medium text-foreground">{{ d.dept_name }}</div>
            <div class="mt-0.5 text-[10.5px] text-faint">{{ d.members }} 人参与</div>
          </div>
          <span class="shrink-0 font-mono text-[14px] font-bold tabular-nums text-foreground" title="部门成员总积分之和">{{ d.score }}</span>
        </div>
        <div v-if="!deptHeroes.length" class="border-t border-border px-[18px] py-5 text-center text-[12px] text-muted-foreground">
          暂无部门数据（组织快照未同步或尚无有积分的在册成员）
        </div>
      </template>
      <div
        v-for="h in personList" v-else :key="h.author_id"
        class="flex items-center gap-3 border-t border-border px-[18px] py-2.5 first:border-t-0"
        :class="h.author_id === me ? 'bg-accent-soft/40' : ''"
      >
        <span class="grid size-6 shrink-0 place-items-center rounded-md font-mono text-[12px] font-bold tabular-nums" :class="rankCls(h.rank)">{{ h.rank }}</span>
        <span class="grid size-7 shrink-0 place-items-center rounded-full bg-accent-soft text-[12px] font-semibold text-accent-text">{{ initial(h.author_name) }}</span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13px] font-medium text-foreground">{{ h.author_name || h.author_id }}<span v-if="h.author_id === me" class="ml-1 text-[11px] text-accent-text">（我）</span></div>
          <!-- 构成副行（可审计）：采纳×N · 引用×M · 反馈×K；引用算不出显「—」不用 0 顶替 -->
          <div v-if="hasScore" class="mt-0.5 truncate text-[10.5px] tabular-nums text-faint" data-testid="hero-breakdown">
            采纳×{{ h.adopted ?? h.count }} · 引用×{{ h.hits != null ? h.hits : '—' }} · 反馈×{{ h.feedback ?? 0 }}
          </div>
        </div>
        <span v-if="!hasScore && h.hits != null" data-testid="hero-hits"
          class="shrink-0 rounded bg-accent-soft px-1.5 py-px text-[10.5px] font-medium tabular-nums text-accent-text"
          :title="`TA 的贡献被引用进 ${h.hits} 次回答（累计）`"
        >引用 {{ h.hits }}</span>
        <span class="shrink-0 font-mono text-[14px] font-bold tabular-nums text-foreground"
              :title="hasScore ? '总积分（10×采纳 + 1×引用 + 3×有效反馈）' : '已入库篇数（排名依据）'"
        >{{ hasScore ? (h.score ?? 0) : h.count }}</span>
      </div>
      <div v-if="tab === 'mydept' && !myDeptHeroes.length" class="border-t border-border px-[18px] py-5 text-center text-[12px] text-muted-foreground">
        你所在部门暂无上榜数据
      </div>
    </div>
  </section>
</template>
