<script setup lang="ts">
import { ShieldCheck, FileText, Loader2 } from 'lucide-vue-next'
import { deptLabel, permLabel } from '@/lib/kb'
import { useKb, type AccessGrantItem } from '@/composables/useKb'
import LoadError from './LoadError.vue'
import { useDialog } from '@/composables/useDialog'

// 已授权清单（审批人侧）：本部门文档现行有效（approved 存量）的跨部门检索授权，可撤销（approved→revoked）。
// 与「授权申请」（pending 待审批）区分：此处是已放行的存量，活跃态调（st-live）。空时整块不渲染。
const { accessGrants, isBusy, revokeAccess, loadAccessGrants, loadErrors } = useKb()
const { confirm, promptText } = useDialog()

// requester_depts 为逗号分隔组码（多部门管理员可一次授予多组）→ 逐个 deptLabel 再拼。
const reqLabel = (csv: string) => csv.split(',').map((c) => deptLabel(c.trim())).filter(Boolean).join('、')

async function onRevoke(g: AccessGrantItem) {
  const okGo = await confirm({
    title: '撤销授权', confirmText: '撤销', danger: true,
    message: `撤销「${reqLabel(g.requester_dept)}」对《${g.doc_title}》的检索授权？\n撤销后该部门将不再能检索此文档（即时生效），申请人可重新申请。`,
  })
  if (!okGo) return
  const reason = await promptText({ title: '撤销原因', message: '将记录于审计（可空）。', placeholder: '撤销原因（可空）', confirmText: '确认撤销', danger: true })
  if (reason === null) return   // 取消
  void revokeAccess(g, reason || 'revoked')
}
</script>

<template>
  <section v-if="accessGrants.length || loadErrors['accessGrants']">
    <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">已授权</p>
    <LoadError class="mb-2.5" :message="loadErrors['accessGrants']" @retry="loadAccessGrants()" />
    <div v-if="accessGrants.length" class="overflow-hidden rounded-[15px] border border-border bg-card">
      <!-- 活跃态头（st-live，区别于待审批的绿/橙头） -->
      <div class="flex items-center gap-2.5 border-b border-border bg-st-live/10 px-[18px] py-3">
        <ShieldCheck :size="16" :stroke-width="1.75" class="text-st-live" />
        <span class="text-sm font-semibold text-foreground">已授权</span>
        <span class="rounded-full bg-st-live px-2 py-px text-[11px] font-bold text-white">{{ accessGrants.length }}</span>
        <div class="flex-1" />
        <span class="hidden text-xs text-muted-foreground sm:inline">本部门文档已放行的跨部门检索授权，可撤销</span>
      </div>
      <!-- 行 -->
      <div
        v-for="g in accessGrants" :key="g.id"
        class="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <span class="grid size-8 shrink-0 place-items-center rounded-lg bg-st-live/10 text-st-live">
          <FileText :size="16" :stroke-width="1.75" />
        </span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13.5px] font-semibold text-foreground">
            <span class="text-st-live">{{ reqLabel(g.requester_dept) }}</span> 可检索《{{ g.doc_title }}》
          </div>
          <div class="truncate text-[11.5px] text-faint">
            归属 {{ deptLabel(g.owner_dept) }} · {{ permLabel(g.permission_level) }} · 申请人 {{ g.requester_name }}
            <span v-if="g.decided_at"> · 授权于 {{ g.decided_at }}</span>
          </div>
          <div v-if="g.reason" class="mt-1 line-clamp-2 text-[12px] text-muted-foreground">“{{ g.reason }}”</div>
        </div>
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="isBusy(`grant:${g.id}`)" @click="onRevoke(g)"
        ><Loader2 v-if="isBusy(`grant:${g.id}`)" :size="13" :stroke-width="2" class="animate-spin" />{{ isBusy(`grant:${g.id}`) ? '撤销中…' : '撤销' }}</button>
      </div>
    </div>
  </section>
</template>
