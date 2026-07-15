<script setup lang="ts">
import { ref } from 'vue'
import { FileText, RefreshCw, PencilLine } from 'lucide-vue-next'
import { useContribute, type ContributionItem } from '@/composables/useContribute'
import { fmtTs } from '@/lib/kb'
import LoadError from '@/components/manage/LoadError.vue'
import ContribBadge from './ContribBadge.vue'

// 我的贡献：4 态徽章（待审核 / 已采纳·待入库 / 已入库 / 入库失败）+ 入库失败可重试。
// 批次ε-2：行可展开看原稿（此前 content 完全不渲染，被驳回后原稿不可见=只能凭记忆重打）；
// 被驳回行「修改重交」带旧稿重开表单（新 contribution_id 重提，原驳回记录保留审计）；
// failed 行透出失败原因（ingestion_error，老后端缺字段→兜底句）。
const { myContribs, loadErrors, isBusy, canManage, loadMine, retryContribution, openModal } = useContribute()

const expanded = ref<Record<string, boolean>>({})
function toggleExpand(id: string) { expanded.value = { ...expanded.value, [id]: !expanded.value[id] } }

// 批次ε-3 R1 + ε-5 R1：registering 的展示细分（后端 state 不变，按 doc_badge=台账词表派生）——
// 待审核=卡 kb_admin 放行（等人）；已隔离/未入索引/处理失败=管线死链（重试不自愈/作者无重试权，
// 统一「入库受阻」家族给重投出路）；内容未变=系统判定与在库内容相同（良性，muted，不给重投——
// 原样重投仍判重复）；其余回落默认。
const STALLED_BADGES = new Set(['已隔离', '未入索引', '处理失败'])
function displayState(c: ContributionItem): string {
  if (c.state !== 'registering' || !c.doc_badge) return c.state
  if (c.doc_badge === '待审核') return 'pending_approval'
  if (c.doc_badge === '内容未变') return 'ingest_skipped_duplicate'
  if (STALLED_BADGES.has(c.doc_badge)) return 'ingest_stalled'
  return c.state
}
function stallHint(c: ContributionItem): string {
  if (c.doc_badge === '已隔离') {
    return '内容触发敏感信息隔离，未能入库——重试不会自愈，请调整内容后重新提交或联系管理员。'
  }
  if (c.doc_badge === '处理失败') {
    return '入库处理失败（非正常排队）——请修改后重新提交；反复失败请联系管理员排查。'
  }
  return '未能生成可检索内容（内容被整篇隔离或过短），请修改后重新提交或联系管理员。'
}

// 修改重交：带旧稿+原归属重开贡献弹窗；继承缺口溯源（rejected 行不占缺口坑位，
// 新提交接上原缺口的关闭链路）。提交走既有 POST（新 contribution_id）。
// 批次ε-5 R1：入库受阻行同样给重投出路（原 stalled 行无退役机制，旧行仍挂——如实）；
// 警示按成因分流并随弹窗展示（行内警示在弹窗打开后已离开视野，等于白写）。
// failed 行也给重投：重试端点是管理员专属，「修改重交」是员工唯一自助出路（ε-5 R1）
function canReopen(c: ContributionItem): boolean {
  return c.state === 'rejected' || c.state === 'failed' || displayState(c) === 'ingest_stalled'
}
function reopen(c: ContributionItem) {
  const warning = displayState(c) !== 'ingest_stalled' ? undefined
    : c.doc_badge === '已隔离'
      ? '原稿触发敏感信息隔离——请修改相关内容后再提交，原样重投会再次被隔离。'
      : '原稿未能入库，请修改完善后再提交。'
  openModal({
    question: c.question, content: c.content, dept: c.category_dept,
    sourceMessageId: c.source_message_id || undefined,
    gapQuery: c.gap_query || undefined,
    warning,
  })
}
</script>

<template>
  <!-- 卡头已带图标+标题，不再另设分区眉标 -->
  <section>
    <LoadError class="mb-2.5" :message="loadErrors['mine']" @retry="loadMine()" />
    <div class="overflow-hidden rounded-[15px] border border-border bg-card">
      <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3">
        <FileText :size="16" :stroke-width="1.75" class="text-accent-text" />
        <span class="text-sm font-semibold text-foreground">我的贡献</span>
      </div>
      <div
        v-for="c in myContribs" :key="c.contribution_id"
        class="border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <div class="flex items-start gap-3">
          <button
            type="button" data-testid="mycontrib-toggle"
            class="min-w-0 flex-1 text-left"
            :aria-expanded="!!expanded[c.contribution_id]"
            @click="toggleExpand(c.contribution_id)"
          >
            <div class="truncate text-[13px] font-medium text-foreground">{{ c.question }}</div>
            <div class="mt-1 text-[11px] text-faint">
              {{ fmtTs(c.created_at) }}
              <span v-if="c.state === 'rejected'"> · {{ c.review_note || '未填写驳回理由' }}</span>
              <span v-else-if="c.review_note"> · {{ c.review_note }}</span>
            </div>
          </button>
          <div class="flex shrink-0 items-center gap-1.5">
            <!-- 被引用数（批次ε-2 R2）：已入库行的价值反馈；算不出（null/老后端）自隐，0=真零照显 -->
            <span
              v-if="c.state === 'searchable' && c.hits != null" data-testid="mycontrib-hits"
              class="rounded bg-accent-soft px-1.5 py-px text-[10.5px] font-medium tabular-nums text-accent-text"
              :title="`这条知识被引用进 ${c.hits} 次回答（累计）`"
            >被引用 {{ c.hits }} 次</span>
            <ContribBadge :state="displayState(c)" />
            <!-- 重试端点=管理员专属（_require_kb_console）——员工点了恒 403，按角色收起死按钮（ε-5 R1） -->
            <button
              v-if="c.state === 'failed' && canManage" type="button" :disabled="isBusy(`ct:${c.contribution_id}`)"
              class="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
              @click="retryContribution(c)"
            ><RefreshCw :size="11" :stroke-width="2" /> 重试</button>
            <button
              v-if="canReopen(c)" type="button" data-testid="mycontrib-reopen"
              class="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11.5px] font-medium text-accent-text transition hover:border-accent-strong hover:bg-accent-soft"
              @click="reopen(c)"
            ><PencilLine :size="11" :stroke-width="2" /> 修改重交</button>
          </div>
        </div>
        <!-- failed：失败原因透出（ingestion_error 空 → 兜底句，不留空白） -->
        <p
          v-if="c.state === 'failed'" data-testid="mycontrib-fail-reason"
          class="mt-1.5 text-[11.5px] leading-relaxed text-st-fail"
        >{{ c.ingestion_error || '入库失败——可修改后重新提交；管理员亦可直接重试。' }}</p>
        <!-- 待放行（批次ε-3 R1）：等 kb_admin 放行，非排队卡顿——告诉作者卡在哪 -->
        <p
          v-if="displayState(c) === 'pending_approval'" data-testid="mycontrib-approval-hint"
          class="mt-1.5 text-[11.5px] leading-relaxed text-muted-foreground"
        >全员公开的贡献需知识库管理员放行后才会入库检索，请耐心等待。</p>
        <!-- 入库受阻（批次ε-3 R1 + ε-5 补处理失败）：死链——此前与正常排队同显「待入库」，永远等不来 -->
        <p
          v-if="displayState(c) === 'ingest_stalled'" data-testid="mycontrib-stall-reason"
          class="mt-1.5 text-[11.5px] leading-relaxed text-st-fail"
        >{{ stallHint(c) }}</p>
        <!-- 同内容已在库（ε-5 R1）：良性去重事实，非故障——别让作者干等，也别用失败措辞吓人 -->
        <p
          v-if="displayState(c) === 'ingest_skipped_duplicate'" data-testid="mycontrib-duplicate-hint"
          class="mt-1.5 text-[11.5px] leading-relaxed text-muted-foreground"
        >系统判定这次提交与知识库中已有内容完全相同，未重复入库。若确有实质差异，请修改表述后重新提交。</p>
        <!-- 展开：原稿全文（被驳回后修改重交前先能看到自己写了什么） -->
        <div
          v-if="expanded[c.contribution_id]" data-testid="mycontrib-content"
          class="mt-2 whitespace-pre-wrap rounded-[10px] bg-panel/60 px-3 py-2.5 text-[12px] leading-relaxed text-muted-foreground"
        >{{ c.content }}</div>
      </div>
      <p v-if="!myContribs.length" class="px-[18px] py-8 text-center text-[12.5px] text-muted-foreground">还没有贡献，去「待回答」挑一个问题回答吧。</p>
    </div>
  </section>
</template>
