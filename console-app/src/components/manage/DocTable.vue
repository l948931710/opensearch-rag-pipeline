<script setup lang="ts">
import { computed } from 'vue'
import { Search, ArrowUpDown, FilePlus2, Archive, History } from 'lucide-vue-next'
import { deptLabel, permLabel } from '@/lib/kb'
import { useKb, type DocItem, type SortKey } from '@/composables/useKb'
import StatusPill from './StatusPill.vue'

const {
  docs, filtered, loadingDocs, q, filter, sortKey, sortDir,
  setQuery, sortBy, countOf, enterVersionMode, retire, openHistory,
} = useKb()

// 状态筛选 chip：从已加载文档里取出现过的徽章（+ 全部）。
const chips = computed(() => {
  const present = Array.from(new Set(docs.value.map((d) => d.status_badge).filter(Boolean)))
  return ['', ...present]
})

const COLS: { key: SortKey; label: string }[] = [
  { key: 'title', label: '文档名' },
  { key: 'owner_dept', label: '归属' },
  { key: 'current_version_no', label: '版本' },
  { key: 'status_badge', label: '状态' },
  { key: 'updated_at', label: '更新' },
]

function arrow(k: SortKey) { return sortKey.value === k ? (sortDir.value === 1 ? '↑' : '↓') : '' }

async function onRetire(d: DocItem) {
  if (!confirm(`确认退役《${d.title || d.original_filename || d.doc_id}》？\n将标记下线、停止作为升版目标。从检索彻底移除会在下次维护完成（本操作可逆）。`)) return
  const r = await retire(d)
  if (!r.ok && r.msg) alert('退役失败：' + r.msg)
}
</script>

<template>
  <section class="rounded-xl border border-border bg-card p-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <h2 class="text-[15px] font-semibold text-foreground">我的文档 <span class="font-mono text-xs text-muted-foreground">{{ docs.length }}</span></h2>
      <div class="relative">
        <Search :size="14" :stroke-width="1.75" class="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
        <input
          :value="q" type="search" placeholder="搜索文档名…"
          class="w-56 rounded-md border border-input bg-card py-1.5 pl-8 pr-2.5 text-sm text-foreground focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/15"
          @input="setQuery(($event.target as HTMLInputElement).value)"
        />
      </div>
    </div>

    <!-- 状态筛选 chips -->
    <div class="mt-3 flex flex-wrap gap-1.5">
      <button
        v-for="c in chips" :key="c || 'all'"
        type="button"
        class="rounded-full border px-2.5 py-1 text-xs transition"
        :class="filter === c ? 'border-accent-soft bg-accent text-accent-foreground' : 'border-border text-muted-foreground hover:bg-panel'"
        @click="filter = c"
      >
        {{ c || '全部' }} <span class="font-mono">{{ countOf(c) }}</span>
      </button>
    </div>

    <!-- Atlas 台账网格（< 680px 自动卡片化，由 .led-* 媒体查询接管） -->
    <div class="mt-4 overflow-hidden rounded-xl border border-border bg-card">
      <div class="led-head">
        <button
          v-for="col in COLS" :key="col.key" type="button"
          class="led-sort inline-flex items-center gap-1"
          :aria-label="`按${col.label}排序`"
          :aria-sort="sortKey === col.key ? (sortDir === 1 ? 'ascending' : 'descending') : 'none'"
          @click="sortBy(col.key)"
        >
          {{ col.label }}<ArrowUpDown :size="11" :stroke-width="1.75" class="opacity-40" /><span class="text-accent-text">{{ arrow(col.key) }}</span>
        </button>
        <span class="text-right">操作</span>
      </div>

      <div
        v-for="d in filtered" :key="d.doc_id"
        class="led-row" :data-retired="d.status_badge === '已退役' ? '1' : '0'"
      >
        <div class="led-cell led-cell-main min-w-0" data-label="文档名">
          <div class="truncate text-[13.5px] font-semibold text-foreground">{{ d.title || d.original_filename || d.doc_id }}</div>
          <div class="truncate text-[11px] text-faint">
            {{ permLabel(d.permission_level) }}<span v-if="d.original_filename && d.original_filename !== d.title"> · {{ d.original_filename }}</span>
          </div>
        </div>
        <div class="led-cell text-sm text-muted-foreground" data-label="归属">{{ deptLabel(d.owner_dept) }}</div>
        <div class="led-cell font-mono text-xs text-muted-foreground" data-label="版本">v{{ d.current_version_no || 1 }}</div>
        <div class="led-cell" data-label="状态"><StatusPill :badge="d.status_badge" /></div>
        <div class="led-cell font-mono text-xs text-muted-foreground" data-label="更新">{{ (d.updated_at || '').slice(0, 16) }}</div>
        <div class="led-cell led-actions doc-actions" data-label="操作">
          <button type="button" class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition hover:bg-panel hover:text-foreground" @click="openHistory(d)">
            <History :size="13" :stroke-width="1.75" /> 历史
          </button>
          <button type="button" class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition hover:bg-panel hover:text-foreground" @click="enterVersionMode(d)">
            <FilePlus2 :size="13" :stroke-width="1.75" /> 升版
          </button>
          <button
            v-if="d.status_badge !== '已退役'"
            type="button" class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition hover:bg-st-fail/10 hover:text-st-fail"
            @click="onRetire(d)"
          >
            <Archive :size="13" :stroke-width="1.75" /> 退役
          </button>
        </div>
      </div>

      <div v-if="!filtered.length" class="px-4 py-10 text-center text-sm text-muted-foreground">
        {{ loadingDocs ? '加载中…' : (q ? '无匹配文档' : '暂无文档，先上传一篇吧') }}
      </div>
    </div>
  </section>
</template>
