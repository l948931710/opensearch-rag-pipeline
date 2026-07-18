<script setup lang="ts">
import { onUnmounted, watch } from 'vue'
import { Eye, Loader2, ShieldAlert, X } from '@lucide/vue'
import { deptLabel, permLabel, viaLabel } from '@/lib/kb'
import { useKb } from '@/composables/useKb'

// 「谁能看到这篇文档」解释器（只读弹窗）：把 基础级别 + 组语义（生产伞组/营销共享面）
// + 跨部门授权折叠成一份有效可见范围清单。判定在后端与检索同源（visibility-explain），
// 本组件只展示，绝不在前端复算任何 ACL 规则。
const { visCtx, visExplain, visLoading, visErr, openVisibility, closeVisibility } = useKb()

// Esc 关闭（对齐 ConfirmDialog/VersionHistoryModal；挂 window，焦点不在弹窗内也能关）。
function onKey(e: KeyboardEvent) { if (e.key === 'Escape') closeVisibility() }
watch(visCtx, (v) => {
  if (v) window.addEventListener('keydown', onKey)
  else window.removeEventListener('keydown', onKey)
})
onUnmounted(() => window.removeEventListener('keydown', onKey))

const VIA_TONE: Record<string, string> = {
  owner: 'border-accent-soft bg-accent text-accent-foreground',
  umbrella: 'border-border bg-panel text-muted-foreground',
  shared_policy: 'border-border bg-panel text-muted-foreground',
  grant: 'border-st-live/30 bg-st-live/10 text-st-live',
}
</script>

<template>
  <div
    v-if="visCtx"
    class="fixed inset-0 z-[80] flex items-center justify-center bg-black/40 p-6"
    @click="closeVisibility"
  >
    <div class="w-[460px] max-w-full overflow-hidden rounded-2xl border border-border bg-card shadow-xl" @click.stop>
      <!-- 头 -->
      <div class="flex items-start gap-3 border-b border-border px-[22px] py-4">
        <span class="grid size-9 shrink-0 place-items-center rounded-[10px] bg-accent-soft text-accent-text"><Eye :size="17" :stroke-width="1.75" /></span>
        <div class="min-w-0 flex-1">
          <div class="text-base font-semibold text-foreground">谁能看到这篇文档</div>
          <div class="mt-0.5 truncate text-[12.5px] text-muted-foreground">
            《{{ visCtx.title || visCtx.original_filename || visCtx.doc_id }}》 · 归属 {{ deptLabel(visCtx.owner_dept) }}
          </div>
        </div>
        <button
          type="button" aria-label="关闭"
          class="grid size-[30px] shrink-0 place-items-center rounded-lg text-faint transition hover:bg-panel hover:text-foreground"
          @click="closeVisibility"
        ><X :size="15" :stroke-width="2" /></button>
      </div>

      <div class="px-[22px] py-4">
        <div v-if="visLoading" class="flex items-center gap-2 py-6 text-sm text-muted-foreground">
          <Loader2 :size="15" class="animate-spin" /> 正在解析可见范围…
        </div>

        <div v-else-if="visErr" class="rounded-lg border border-st-fail/30 bg-st-fail/10 px-3 py-2 text-[12.5px] text-st-fail">
          {{ visErr }}
          <button type="button" class="ml-2 underline" @click="visCtx && openVisibility(visCtx)">重试</button>
        </div>

        <template v-else-if="visExplain">
          <!-- 基础级别 -->
          <div class="text-[12.5px] text-muted-foreground">
            基础可见范围：<span class="font-medium text-foreground">{{ permLabel(visExplain.permission_level) }}</span>
          </div>

          <!-- 隔离/下线/受限：明确"不在检索中" -->
          <div
            v-if="visExplain.nobody"
            class="mt-3 flex items-start gap-2 rounded-lg border border-st-fail/30 bg-st-fail/10 px-3 py-2.5 text-[12.5px] text-st-fail"
          >
            <ShieldAlert :size="14" :stroke-width="1.75" class="mt-0.5 shrink-0" />
            <span v-if="visExplain.quarantined">该文档处于<b>安全隔离</b>（PII/敏感内容）：不在检索中，任何部门都搜不到；恢复上线的唯一途径是脱敏重灌。</span>
            <span v-else-if="!visExplain.active">该文档已退役下线：不在检索中，任何部门都搜不到（可恢复上线）。</span>
            <span v-else>受限归档：不在检索中，任何部门都搜不到（可改回「仅本部门」恢复）。</span>
          </div>

          <!-- 全公司 -->
          <div
            v-else-if="visExplain.everyone"
            class="mt-3 rounded-lg border border-st-live/30 bg-st-live/10 px-3 py-2.5 text-[12.5px] text-st-live"
          >
            全公司公开：所有员工（含仅 public 权限的账号）都能检索到本文档。
          </div>

          <!-- dept_internal：逐来源读者清单 -->
          <template v-else>
            <div class="mt-3 space-y-1.5">
              <div
                v-for="r in visExplain.readers" :key="r.dept + r.via"
                class="flex items-center justify-between rounded-lg border border-border bg-panel px-3 py-2"
              >
                <span class="text-[13px] font-medium text-foreground">{{ deptLabel(r.dept) }}</span>
                <span
                  class="rounded-full border px-2 py-0.5 text-[10.5px] font-medium"
                  :class="VIA_TONE[r.via] || VIA_TONE.umbrella"
                >{{ viaLabel(r.via) }}</span>
              </div>
              <div v-if="!visExplain.readers.length" class="rounded-lg border border-border bg-panel px-3 py-2 text-[12.5px] text-muted-foreground">
                仅归属部门可见（暂无跨部门授权）。
              </div>
            </div>
            <p class="mt-3 text-[11.5px] leading-relaxed text-faint">
              判定与检索完全同源：生产伞组 = 生产各事业部共读；营销共享面 = 营销可读生产家族（既定策略）；
              跨部门授权可在「权限」弹窗逐条撤销。
            </p>
          </template>
        </template>
      </div>
    </div>
  </div>
</template>
