<script setup lang="ts">
import { ref } from 'vue'
import { Fingerprint, Loader2, Search, X } from 'lucide-vue-next'
import { deptLabel } from '@/lib/kb'
import { useOntology, type OntologyCandidate, type OntologyCase, type OntologyObjectHit } from '@/composables/useOntology'
import { useDialog } from '@/composables/useDialog'
import LoadError from './LoadError.vue'

// steward 消解工作台（独立 tab 主体）：覆盖率卡片 + open case 队列。
// 处置三动作：确认候选 / 手动指定（搜索 → **人工显式点选**目标 → 确认）/ 驳回。
// PR-F（P0-06 HITL 验收⑥）：confirm 与 dismiss 同纪律**理由必填**；手动指定
// 绝不自动取第一条；候选行展示目标归属/密级；evidence_json 可展开核对。
// 授权由服务端硬校验（kb_admin / stewardship scope 的 dept_admin）；此处只做操作面。
const {
  ontologyCases, ontologyCoverage, ontologyError, isOntologyBusy,
  loadOntology, confirmOntologyCase, dismissOntologyCase, batchDismissOntologyCases,
  searchOntologyObjects,
} = useOntology()
const { promptText, notice } = useDialog()

// 手动指定：per-case 搜索结果（内联渲染，人工点选——不取 hits[0]）
const manualHits = ref<Record<string, OntologyObjectHit[]>>({})
// PR-I（P2 批量处置）：勾选集——只支持批量**驳回**（批量确认=放弃逐条核对，违 HITL 纪律）
const selected = ref<Set<string>>(new Set())

function toggleSelect(caseId: string) {
  const next = new Set(selected.value)
  if (next.has(caseId)) next.delete(caseId)
  else next.add(caseId)
  selected.value = next
}

async function onBatchDismiss() {
  const ids = [...selected.value]
  if (!ids.length) return
  const reason = await promptText({
    title: `批量驳回 ${ids.length} 条`,
    message: '同一理由应用到所有勾选条目（废弃编号/录入错误批次…）；逐条独立留痕，单条失败不影响其余。',
    placeholder: '驳回理由（必填）',
    confirmText: '批量驳回',
    danger: true,
  })
  if (reason === null) return
  if (!reason.trim()) {
    void notice({ title: '需要理由', message: '驳回必须填写处置理由（审计留痕）。', danger: true })
    return
  }
  const n = await batchDismissOntologyCases(ids, reason.trim())
  if (n > 0) selected.value = new Set()
}

function pct(v: number | null | undefined): string {
  return v == null ? '—' : `${Math.round(v * 100)}%`
}
function ts(v: string | number | null): string { return typeof v === 'string' ? v.slice(0, 16) : '' }
function confPct(c: OntologyCandidate): string { return `${Math.round(c.confidence * 100)}%` }
function prettyEvidence(raw: string | null): string {
  if (!raw) return ''
  try { return JSON.stringify(JSON.parse(raw), null, 2) } catch { return raw }
}

async function askReason(title: string, message: string): Promise<string | null> {
  const reason = await promptText({
    title, message, placeholder: '确认理由（必填，审计留痕）', confirmText: '确认映射',
  })
  if (reason === null) return null
  if (!reason.trim()) {
    void notice({ title: '需要理由', message: '确认必须填写理由（与驳回同纪律，审计留痕）。', danger: true })
    return null
  }
  return reason.trim()
}

async function onConfirm(kase: OntologyCase, cand: OntologyCandidate) {
  const reason = await askReason(
    '确认身份映射',
    `把「${kase.namespace}」编号 ${kase.raw_value} 正式映射到 ` +
      `${cand.title || cand.canonical_ref} [${cand.canonical_ref}]（来源 ${cand.method}，` +
      `置信 ${confPct(cand)}${cand.owner_dept ? `，归属 ${deptLabel(cand.owner_dept)}` : ''}` +
      `${cand.data_classification ? `，密级 ${cand.data_classification}` : ''}）？` +
      '确认后立即生效为检索/计算的依据。')
  if (reason === null) return
  void confirmOntologyCase(kase, cand, reason)
}

async function onManualSearch(kase: OntologyCase) {
  const q = await promptText({
    title: '手动指定目标对象',
    message: '候选都不对时，按名称搜索目标对象；结果列在卡片里，由你**点选**要映射的那一条。',
    placeholder: '对象名称关键词（如：龙虾杯）',
    confirmText: '搜索',
  })
  if (q === null || !q.trim()) return
  const hits = await searchOntologyObjects(kase.object_type_hint || 'product', q.trim())
  if (!hits.length) {
    void notice({ title: '未找到对象', message: '换个关键词试试；确实没有则该编号可能需要新建对象（走播种/登记流程）。', danger: true })
    return
  }
  manualHits.value = { ...manualHits.value, [kase.case_id]: hits }
}

function clearManualHits(caseId: string) {
  const next = { ...manualHits.value }
  delete next[caseId]
  manualHits.value = next
}

async function onPickManualTarget(kase: OntologyCase, hit: OntologyObjectHit) {
  const reason = await askReason(
    '确认手动指定',
    `把编号 ${kase.raw_value} 映射到你选中的 ${hit.title} [${hit.canonical_ref}]？`)
  if (reason === null) return
  const done = await confirmOntologyCase(kase, { target_object_id: hit.object_id }, reason)
  if (done) clearManualHits(kase.case_id)
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

    <!-- 覆盖率卡片（S9 口径；PR-F：分母口径明示——population=源快照真实分母 / approx=处置率近似） -->
    <div v-if="ontologyCoverage" class="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <div class="rounded-xl border border-border bg-card px-4 py-3">
        <div class="text-[11px] font-medium text-muted-foreground">已确认别名</div>
        <div class="mt-1 text-xl font-bold text-foreground">{{ ontologyCoverage.active_identifiers }}</div>
        <div class="mt-0.5 text-[11px] text-faint">auto {{ ontologyCoverage.auto_active }}（抽检面）</div>
      </div>
      <div class="rounded-xl border border-border bg-card px-4 py-3">
        <div class="text-[11px] font-medium text-muted-foreground">消解覆盖率</div>
        <div class="mt-1 text-xl font-bold text-foreground">{{ pct(ontologyCoverage.resolution_coverage) }}</div>
        <div v-if="ontologyCoverage.denominator === 'population'" class="mt-0.5 text-[11px] text-faint">
          = 已确认 / 源快照 {{ ontologyCoverage.population_records }}
        </div>
        <div v-else class="mt-0.5 text-[11px] text-faint">≈ 已确认 / (已确认+积压)——近似口径，非真实覆盖率</div>
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
        <button
          v-if="selected.size"
          type="button"
          class="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-[5px] text-[12px] font-semibold text-st-fail transition hover:border-border-strong disabled:opacity-50"
          :disabled="isOntologyBusy('onto:batch')"
          @click="onBatchDismiss"
        ><Loader2 v-if="isOntologyBusy('onto:batch')" :size="12" :stroke-width="2" class="animate-spin" />批量驳回（{{ selected.size }}）</button>
        <span class="hidden text-xs text-muted-foreground sm:inline">确认即成为检索/计算依据；拿不准就驳回留档，绝不猜</span>
      </div>

      <div
        v-for="kase in ontologyCases" :key="kase.case_id"
        class="border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
          <input
            type="checkbox"
            class="h-3.5 w-3.5 accent-[var(--primary,#4b6bfb)]"
            :aria-label="`勾选 ${kase.raw_value}`"
            :checked="selected.has(kase.case_id)"
            @change="toggleSelect(kase.case_id)"
          >
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
            @click="onManualSearch(kase)"
          ><Search :size="12" :stroke-width="2" /> 手动指定</button>
          <button
            type="button"
            class="inline-flex items-center gap-1 self-start rounded-lg border border-border px-3 py-[6px] text-[12px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
            :disabled="isOntologyBusy(`onto:${kase.case_id}`)"
            @click="onDismiss(kase)"
          ><Loader2 v-if="isOntologyBusy(`onto:${kase.case_id}`)" :size="12" :stroke-width="2" class="animate-spin" />驳回</button>
        </div>

        <!-- 证据快照（PR-F HITL：处置人看得见依据，不是盲确认按钮） -->
        <details v-if="kase.evidence_json" class="mt-2 rounded-lg bg-panel px-3 py-2">
          <summary class="cursor-pointer text-[11.5px] font-medium text-muted-foreground">证据快照（evidence）</summary>
          <pre class="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] text-foreground">{{ prettyEvidence(kase.evidence_json) }}</pre>
        </details>

        <!-- 候选：每条一键确认（理由必填）；跨部门不可读候选只显受限占位；无候选给指引 -->
        <div v-if="kase.candidates.length" class="mt-2 space-y-1.5">
          <div
            v-for="cand in kase.candidates" :key="cand.candidate_id"
            class="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg bg-panel px-3 py-2"
          >
            <span v-if="cand.target_visible === false" class="min-w-0 flex-1 truncate text-[12.5px] text-faint">
              [受限对象]（目标归属其他部门，无权查看/确认）
            </span>
            <span v-else class="min-w-0 flex-1 truncate text-[12.5px] text-foreground">
              {{ cand.title || cand.canonical_ref }}
              <span class="ml-1 font-mono text-[11px] text-faint">[{{ cand.canonical_ref }}]</span>
              <span v-if="cand.owner_dept" class="ml-1 text-[11px] text-faint">· {{ deptLabel(cand.owner_dept) }}</span>
              <span v-if="cand.data_classification && cand.data_classification !== 'internal'" class="ml-1 text-[11px] text-faint">· {{ cand.data_classification }}</span>
              <span v-if="cand.target_status && cand.target_status !== 'active'" class="ml-1 text-[11px] text-st-fail">（对象已{{ cand.target_status }}）</span>
            </span>
            <span class="font-mono text-[11px] text-muted-foreground">{{ cand.method }} · {{ confPct(cand) }}</span>
            <button
              type="button"
              class="inline-flex items-center gap-1 rounded-lg bg-primary px-3 py-[5px] text-[12px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
              :disabled="cand.target_visible === false || cand.target_status !== 'active' || isOntologyBusy(`onto:${kase.case_id}`)"
              @click="onConfirm(kase, cand)"
            ><Loader2 v-if="isOntologyBusy(`onto:${kase.case_id}`)" :size="12" :stroke-width="2" class="animate-spin" />确认此候选</button>
          </div>
        </div>
        <p v-else class="mt-2 text-[12px] text-faint">无系统候选——可手动指定目标对象，或驳回留档（如需新建对象走登记流程）。</p>

        <!-- 手动指定：搜索结果内联列出，人工点选（PR-F：绝不自动取第一条） -->
        <div v-if="manualHits[kase.case_id]?.length" class="mt-2 rounded-lg border border-border bg-panel px-3 py-2">
          <div class="flex items-center gap-2">
            <span class="text-[11.5px] font-medium text-muted-foreground">
              搜索结果 {{ manualHits[kase.case_id].length }} 条——点选要映射的目标（不会自动选第一条）
            </span>
            <div class="flex-1" />
            <button type="button" class="text-faint transition hover:text-foreground" aria-label="关闭搜索结果" @click="clearManualHits(kase.case_id)">
              <X :size="13" :stroke-width="2" />
            </button>
          </div>
          <div class="mt-1.5 space-y-1">
            <button
              v-for="hit in manualHits[kase.case_id]" :key="hit.object_id"
              type="button"
              class="flex w-full items-center gap-2 rounded-md border border-border bg-card px-2.5 py-1.5 text-left text-[12.5px] text-foreground transition hover:border-border-strong disabled:opacity-50"
              :disabled="isOntologyBusy(`onto:${kase.case_id}`)"
              @click="onPickManualTarget(kase, hit)"
            >
              <span class="min-w-0 flex-1 truncate">{{ hit.title }}</span>
              <span class="font-mono text-[11px] text-faint">[{{ hit.canonical_ref }}]</span>
              <span v-if="hit.owner_dept" class="text-[11px] text-faint">{{ deptLabel(hit.owner_dept) }}</span>
            </button>
          </div>
        </div>
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
      确认=铸正式别名（同编号至多一条现行映射，可在对象详情里纠错改指）；确认与驳回都必须留理由；auto 通道确认的映射会进抽检队列复核。
    </p>
  </section>
</template>
