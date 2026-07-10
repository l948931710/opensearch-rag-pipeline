<script setup lang="ts">
import { Fingerprint, Loader2, Search } from 'lucide-vue-next'
import { deptLabel } from '@/lib/kb'
import { useOntology, type OntologyCandidate, type OntologyCase } from '@/composables/useOntology'
import { useDialog } from '@/composables/useDialog'
import LoadError from './LoadError.vue'

// steward 消解工作台（独立 tab 主体）：覆盖率卡片 + open case 队列。
// 处置三动作：确认候选 / 手动指定（搜索目标对象后确认=改指）/ 驳回（理由必填）。
// 授权由服务端硬校验（kb_admin / stewardship scope 的 dept_admin）；此处只做操作面。
const {
  ontologyCases, ontologyCoverage, ontologyError, isOntologyBusy,
  loadOntology, confirmOntologyCase, dismissOntologyCase, searchOntologyObjects,
} = useOntology()
const { promptText, confirm, notice } = useDialog()

function pct(v: number | null | undefined): string {
  return v == null ? '—' : `${Math.round(v * 100)}%`
}
function ts(v: string | number | null): string { return typeof v === 'string' ? v.slice(0, 16) : '' }
function confPct(c: OntologyCandidate): string { return `${Math.round(c.confidence * 100)}%` }

async function onConfirm(kase: OntologyCase, cand: OntologyCandidate) {
  const ok = await confirm({
    title: '确认身份映射',
    message: `把「${kase.namespace}」编号 ${kase.raw_value} 正式映射到 ` +
      `${cand.title || cand.canonical_ref} [${cand.canonical_ref}]（来源 ${cand.method}，` +
      `置信 ${confPct(cand)}）？确认后立即生效为检索/计算的依据。`,
    confirmText: '确认映射',
  })
  if (!ok) return
  void confirmOntologyCase(kase, cand)
}

async function onManualAssign(kase: OntologyCase) {
  const q = await promptText({
    title: '手动指定目标对象',
    message: '候选都不对时，按名称搜索目标对象（取最匹配一条，确认前会再核对）。',
    placeholder: '对象名称关键词（如：龙虾杯）',
    confirmText: '搜索',
  })
  if (q === null || !q.trim()) return
  const hits = await searchOntologyObjects(kase.object_type_hint || 'product', q.trim())
  if (!hits.length) {
    void notice({ title: '未找到对象', message: '换个关键词试试；确实没有则该编号可能需要新建对象（走播种/登记流程）。', danger: true })
    return
  }
  const top = hits[0]
  const ok = await confirm({
    title: '确认手动指定',
    message: `匹配到 ${hits.length} 条，取最匹配：${top.title} [${top.canonical_ref}]。` +
      `把编号 ${kase.raw_value} 映射到它？`,
    confirmText: '确认映射',
  })
  if (!ok) return
  void confirmOntologyCase(kase, { target_object_id: top.object_id }, '手动指定')
}

async function onDismiss(kase: OntologyCase) {
  const reason = await promptText({
    title: '驳回该编号',
    message: `驳回「${kase.raw_value}」的消解申请？理由必填（废弃编号/录入错误/等待新建对象…），留档可查。`,
    placeholder: '驳回理由（必填）',
    confirmText: '驳回',
    danger: true,
  })
  if (reason === null) return
  if (!reason.trim()) {
    void notice({ title: '需要理由', message: '驳回必须填写处置理由（审计留痕）。', danger: true })
    return
  }
  void dismissOntologyCase(kase, reason.trim())
}
</script>

<template>
  <section class="space-y-4">
    <LoadError :message="ontologyError" @retry="loadOntology(true)" />

    <!-- 覆盖率卡片（S9 口径：确认别名 / 覆盖率 / 积压 / 人工审核率） -->
    <div v-if="ontologyCoverage" class="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <div class="rounded-xl border border-border bg-card px-4 py-3">
        <div class="text-[11px] font-medium text-muted-foreground">已确认别名</div>
        <div class="mt-1 text-xl font-bold text-foreground">{{ ontologyCoverage.active_identifiers }}</div>
        <div class="mt-0.5 text-[11px] text-faint">auto {{ ontologyCoverage.auto_active }}（抽检面）</div>
      </div>
      <div class="rounded-xl border border-border bg-card px-4 py-3">
        <div class="text-[11px] font-medium text-muted-foreground">消解覆盖率</div>
        <div class="mt-1 text-xl font-bold text-foreground">{{ pct(ontologyCoverage.resolution_coverage) }}</div>
        <div class="mt-0.5 text-[11px] text-faint">= 已确认 / (已确认+积压)</div>
      </div>
      <div class="rounded-xl border border-border bg-card px-4 py-3">
        <div class="text-[11px] font-medium text-muted-foreground">待处置积压</div>
        <div class="mt-1 text-xl font-bold text-foreground">{{ ontologyCoverage.open_cases }}</div>
        <div class="mt-0.5 text-[11px] text-faint">已驳回 {{ ontologyCoverage.dismissed_cases }}</div>
      </div>
      <div class="rounded-xl border border-border bg-card px-4 py-3">
        <div class="text-[11px] font-medium text-muted-foreground">人工审核率</div>
        <div class="mt-1 text-xl font-bold text-foreground">{{ pct(ontologyCoverage.manual_review_rate) }}</div>
        <div class="mt-0.5 text-[11px] text-faint">1 − auto/已确认</div>
      </div>
    </div>

    <div v-if="ontologyCases.length" class="overflow-hidden rounded-[15px] border border-border bg-card">
      <div class="flex items-center gap-2.5 border-b border-border bg-accent-soft px-[18px] py-3">
        <Fingerprint :size="16" :stroke-width="1.75" class="text-accent-text" />
        <span class="text-sm font-semibold text-foreground">未解析编号（按观测频次）</span>
        <span class="rounded-full bg-accent-strong px-2 py-px text-[11px] font-bold text-primary-foreground">{{ ontologyCases.length }}</span>
        <div class="flex-1" />
        <span class="hidden text-xs text-muted-foreground sm:inline">确认即成为检索/计算依据；拿不准就驳回留档，绝不猜</span>
      </div>

      <div
        v-for="kase in ontologyCases" :key="kase.case_id"
        class="border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span class="rounded-md bg-panel px-2 py-px font-mono text-[11px] text-muted-foreground">{{ kase.namespace }}</span>
          <span class="font-mono text-[13.5px] font-semibold text-foreground">{{ kase.raw_value }}</span>
          <span class="rounded-full bg-panel px-2 py-px text-[10.5px] font-medium text-muted-foreground">观测 ×{{ kase.seen_count }}</span>
          <span v-if="kase.object_type_hint" class="text-[11.5px] text-faint">类型 {{ kase.object_type_hint }}</span>
          <span v-if="kase.steward_dept" class="text-[11.5px] text-faint">· steward {{ deptLabel(kase.steward_dept) }}</span>
          <span v-if="kase.last_seen_at" class="text-[11.5px] text-faint">· 最近 {{ ts(kase.last_seen_at) }}</span>
          <div class="flex-1" />
          <button
            type="button"
            class="inline-flex items-center gap-1 self-start rounded-lg border border-border px-3 py-[6px] text-[12px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
            :disabled="isOntologyBusy(`onto:${kase.case_id}`)"
            @click="onManualAssign(kase)"
          ><Search :size="12" :stroke-width="2" /> 手动指定</button>
          <button
            type="button"
            class="inline-flex items-center gap-1 self-start rounded-lg border border-border px-3 py-[6px] text-[12px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
            :disabled="isOntologyBusy(`onto:${kase.case_id}`)"
            @click="onDismiss(kase)"
          ><Loader2 v-if="isOntologyBusy(`onto:${kase.case_id}`)" :size="12" :stroke-width="2" class="animate-spin" />驳回</button>
        </div>

        <!-- 候选：每条一键确认；无候选给指引 -->
        <div v-if="kase.candidates.length" class="mt-2 space-y-1.5">
          <div
            v-for="cand in kase.candidates" :key="cand.candidate_id"
            class="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg bg-panel px-3 py-2"
          >
            <span class="min-w-0 flex-1 truncate text-[12.5px] text-foreground">
              {{ cand.title || cand.canonical_ref }}
              <span class="ml-1 font-mono text-[11px] text-faint">[{{ cand.canonical_ref }}]</span>
              <span v-if="cand.target_status && cand.target_status !== 'active'" class="ml-1 text-[11px] text-st-fail">（对象已{{ cand.target_status }}）</span>
            </span>
            <span class="font-mono text-[11px] text-muted-foreground">{{ cand.method }} · {{ confPct(cand) }}</span>
            <button
              type="button"
              class="inline-flex items-center gap-1 rounded-lg bg-primary px-3 py-[5px] text-[12px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
              :disabled="cand.target_status !== 'active' || isOntologyBusy(`onto:${kase.case_id}`)"
              @click="onConfirm(kase, cand)"
            ><Loader2 v-if="isOntologyBusy(`onto:${kase.case_id}`)" :size="12" :stroke-width="2" class="animate-spin" />确认此候选</button>
          </div>
        </div>
        <p v-else class="mt-2 text-[12px] text-faint">无系统候选——可手动指定目标对象，或驳回留档（如需新建对象走登记流程）。</p>
      </div>
    </div>

    <!-- 显式空态：独立 tab 不自隐 -->
    <div v-else-if="!ontologyError" class="rounded-xl border border-border bg-card px-6 py-10 text-center">
      <Fingerprint :size="22" :stroke-width="1.5" class="mx-auto text-faint" />
      <p class="mt-3 text-sm font-medium text-foreground">当前没有待消解的编号</p>
      <p class="mt-1 text-xs text-muted-foreground">
        检索/播种/回填遇到无法自动确认的编号时会挂到这里；确认后即成为全企业统一的身份映射。
      </p>
    </div>

    <p class="ml-0.5 text-[11.5px] text-faint">
      确认=铸正式别名（同编号至多一条现行映射，可在对象详情里纠错改指）；驳回必须留理由；auto 通道确认的映射会进抽检队列复核。
    </p>
  </section>
</template>
