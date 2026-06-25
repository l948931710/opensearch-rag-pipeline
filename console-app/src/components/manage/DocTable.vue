<script setup lang="ts">
import { computed } from 'vue'
import { Search, ArrowUpDown, FilePlus2, Archive } from 'lucide-vue-next'
import { deptLabel } from '@/lib/kb'
import { useKb, type DocItem, type SortKey } from '@/composables/useKb'
import StatusPill from './StatusPill.vue'

const {
  docs, filtered, loadingDocs, q, filter, sortKey, sortDir,
  setQuery, sortBy, countOf, enterVersionMode, retire,
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
      <h2 class="text-sm font-bold text-foreground">我的文档 <span class="font-mono text-xs text-muted-foreground">{{ docs.length }}</span></h2>
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
        :class="filter === c ? 'border-ring bg-accent text-accent-foreground' : 'border-border bg-card text-muted-foreground hover:bg-secondary'"
        @click="filter = c"
      >
        {{ c || '全部' }} <span class="font-mono">{{ countOf(c) }}</span>
      </button>
    </div>

    <!-- 表格 -->
    <div class="mt-4 overflow-x-auto">
      <table class="w-full border-collapse text-sm">
        <thead>
          <tr class="border-b border-border text-left text-xs text-muted-foreground">
            <th v-for="col in COLS" :key="col.key" class="cursor-pointer select-none px-2 py-2 font-medium hover:text-foreground" @click="sortBy(col.key)">
              <span class="inline-flex items-center gap-1">{{ col.label }}<ArrowUpDown :size="11" :stroke-width="1.75" class="opacity-40" />{{ arrow(col.key) }}</span>
            </th>
            <th class="px-2 py-2 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in filtered" :key="d.doc_id" class="border-b border-border/60 last:border-0 hover:bg-secondary/30">
            <td class="max-w-xs px-2 py-2.5">
              <div class="truncate font-medium text-foreground">{{ d.title || d.original_filename || d.doc_id }}</div>
            </td>
            <td class="px-2 py-2.5 text-muted-foreground">{{ deptLabel(d.owner_dept) }}</td>
            <td class="px-2 py-2.5 font-mono text-xs text-muted-foreground">v{{ d.current_version_no || 1 }}</td>
            <td class="px-2 py-2.5"><StatusPill :badge="d.status_badge" /></td>
            <td class="px-2 py-2.5 font-mono text-xs text-muted-foreground">{{ (d.updated_at || '').slice(0, 16) }}</td>
            <td class="px-2 py-2.5">
              <div class="flex items-center justify-end gap-1">
                <button type="button" class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition hover:bg-secondary hover:text-foreground" @click="enterVersionMode(d)">
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
            </td>
          </tr>
          <tr v-if="!filtered.length">
            <td colspan="6" class="px-2 py-8 text-center text-sm text-muted-foreground">
              {{ loadingDocs ? '加载中…' : (q ? '无匹配文档' : '暂无文档，先上传一篇吧') }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
