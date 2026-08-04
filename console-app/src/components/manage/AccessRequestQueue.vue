<script setup lang="ts">
import { computed, ref } from 'vue'
import { Lock, FileText, Loader2, ArrowDownWideNarrow } from '@lucide/vue'
import { deptLabel, permLabel } from '@/lib/kb'
import { useKb, type AccessRequestItem } from '@/composables/useKb'
import LoadError from './LoadError.vue'
import QueuePager from './QueuePager.vue'
import TruncationNotice from '@/components/manage/TruncationNotice.vue'
import { useDialog } from '@/composables/useDialog'

// 授权申请队列（审批人侧，Phase C）：其他部门申请检索本部门文档 → 由文档所属部门管理员审批。
// 头部语义（设计稿 2026-07-19 §1）：「待处理=琥珀」统一 → 与待审批队列同款橙头（st-busy），
// 与「已授权」（st-live 存量）区分。数据空时整块不渲染（无后端 = 自然隐藏，不造占位噪声）。
const { accessRequests, truncatedQueues, isBusy, approveAccess, rejectAccess, loadAccessRequests, loadErrors } = useKb()
const { promptText } = useDialog()

// ── 前端分页 + 时间排序（设计稿 §2：队列 2 条/页；按申请时间 created_at，默认新→旧）──
// 页码经 computed 收敛：授权/驳回后列表变短，当前页自动回落不越界。
const PER_PAGE = 2
const sortDesc = ref(true)
const pageReq = ref(1)
const sorted = computed(() =>
  [...accessRequests.value].sort((a, b) => {
    const cmp = (a.created_at || '').localeCompare(b.created_at || '')
    return sortDesc.value ? -cmp : cmp
  }))
const pages = computed(() => Math.max(1, Math.ceil(sorted.value.length / PER_PAGE)))
const page = computed(() => Math.min(pageReq.value, pages.value))
const paged = computed(() => sorted.value.slice((page.value - 1) * PER_PAGE, page.value * PER_PAGE))
function toggleSort() { sortDesc.value = !sortDesc.value; pageReq.value = 1 }

async function onReject(d: AccessRequestItem) {
  const reason = await promptText({ title: '驳回授权申请', message: `驳回「${d.requester_name || d.requester_dept}」对《${d.doc_title}》的检索授权申请？`, placeholder: '驳回原因（可空，将通知申请人）', confirmText: '驳回', danger: true })
  if (reason === null) return   // 取消
  void rejectAccess(d, reason || 'rejected')
}
</script>

<template>
  <!-- 卡头已带图标+标题+计数，不再另设分区眉标 -->
  <section v-if="accessRequests.length || loadErrors['accessRequests']">
    <LoadError class="mb-2.5" :message="loadErrors['accessRequests']" @retry="loadAccessRequests()" />
    <div v-if="accessRequests.length" class="overflow-hidden rounded-[15px] border border-border bg-card">
      <!-- 橙头（待处理=琥珀；原绿头改） -->
      <div class="flex flex-wrap items-center gap-x-2.5 gap-y-2 border-b border-border bg-st-busy/[0.07] px-[18px] py-3">
        <Lock :size="16" :stroke-width="1.75" class="text-st-busy" />
        <span class="text-sm font-semibold text-foreground">授权申请</span>
        <span class="rounded-full bg-st-busy px-2 py-px text-[11px] font-bold text-white">{{ accessRequests.length }}</span>
        <button
          type="button" data-testid="queue-sort"
          class="inline-flex items-center gap-1 rounded-full border border-border px-2.5 py-0.5 text-[11px] font-medium text-muted-foreground transition hover:bg-panel"
          title="按申请时间排序，点击切换" @click="toggleSort"
        ><ArrowDownWideNarrow :size="11" :stroke-width="1.75" /> <span class="tabular-nums">{{ sortDesc ? '新→旧' : '旧→新' }}</span></button>
        <div class="flex-1" />
        <span class="hidden text-xs text-muted-foreground sm:inline">其他部门申请检索本部门文档，由你审批</span>
      </div>
      <!-- 行（当前页切片） -->
      <div
        v-for="d in paged" :key="d.id"
        class="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <span class="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent-text">
          <FileText :size="16" :stroke-width="1.75" />
        </span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13.5px] font-semibold text-foreground">
            <span class="text-accent-text">{{ deptLabel(d.requester_dept) }}</span> 申请访问《{{ d.doc_title }}》
          </div>
          <div class="truncate text-[11.5px] text-faint">
            归属 {{ deptLabel(d.owner_dept) }} · {{ permLabel(d.permission_level) }} · 申请人 {{ d.requester_name }}
            <span v-if="d.created_at" class="tabular-nums"> · {{ d.created_at }}</span>
          </div>
          <div v-if="d.reason" class="mt-1 line-clamp-2 text-[12px] text-muted-foreground">“{{ d.reason }}”</div>
        </div>
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="isBusy(`acc:${d.id}`)" @click="onReject(d)"
        ><Loader2 v-if="isBusy(`acc:${d.id}`)" :size="13" :stroke-width="2" class="animate-spin" />{{ isBusy(`acc:${d.id}`) ? '驳回中…' : '驳回' }}</button>
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-lg bg-primary px-3.5 py-[7px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          :disabled="isBusy(`acc:${d.id}`)" @click="approveAccess(d)"
        ><Loader2 v-if="isBusy(`acc:${d.id}`)" :size="13" :stroke-width="2" class="animate-spin" />{{ isBusy(`acc:${d.id}`) ? '授权中…' : '授权' }}</button>
      </div>
      <!-- 翻页脚（单页自隐） -->
      <QueuePager :total="sorted.length" :page="page" :per-page="PER_PAGE" @update:page="pageReq = $event" />
      <!-- P3-3：后端硬 LIMIT 100 的截断此前完全静默 —— 队列超上限时被截掉的条目永远没人处理。 -->
      <TruncationNotice class="px-[18px] pb-3" :when="truncatedQueues.accessRequests" :cap="100" label="授权申请" hint="先处理完当前批再刷新" />
    </div>
  </section>
</template>
