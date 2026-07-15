<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ClipboardCheck, User, Loader2, PencilLine } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { deptLabel, fmtTs } from '@/lib/kb'
import { useContribute, CONTRIB_DEPT_OPTS, type ContributionItem, type ContributionRevision } from '@/composables/useContribute'
import LoadError from '@/components/manage/LoadError.vue'
import { useDialog } from '@/composables/useDialog'

// 贡献审核队列（审批人侧）：本部门/全库待采纳的员工贡献 → 采纳即合成文档走管线入库，或驳回。
// 与「待回答」是两件事：这里是【管理员】审别人提交的答案。橙头（待办性质）。
// 采纳时由部门领导定可见范围：部门公开(dept_internal，默认) / 全员公开(public)。
const { pendingContribs, pendingHasMore, loadErrors, isBusy, loadPending, acceptContribution, rejectContribution } = useContribute()
const { promptText } = useDialog()

// 每行选定的可见范围（默认部门公开）
const scope = ref<Record<string, 'dept_internal' | 'public'>>({})
// 批次δ-3：kb_admin 视角=孤儿部门兜底队列（作用域由后端裁决，此处只做语义标注）
const session = useSession()
const isKbAdminRole = computed(() => session.role === 'kb_admin')
function scopeOf(id: string): 'dept_internal' | 'public' { return scope.value[id] || 'dept_internal' }

// ── 展开全文（批次ε-1 B1）：内容溢出两行截断时才显展开控件（短内容零噪声）。
// 纯前端态零网络请求；溢出量在挂载/列表变更后量测（无 ResizeObserver——e2e 视口固定，够用）。
const expanded = ref<Record<string, boolean>>({})
const overflowing = ref<Record<string, boolean>>({})
const contentEls = new Map<string, HTMLElement>()
function bindContentEl(id: string) {
  return (el: unknown) => {
    if (el instanceof HTMLElement) contentEls.set(id, el)
    else contentEls.delete(id)
  }
}
function measureOverflow() {
  const next: Record<string, boolean> = {}
  for (const [id, el] of contentEls) next[id] = el.scrollHeight > el.clientHeight + 1
  overflowing.value = next
}
onMounted(() => { void nextTick(measureOverflow) })
watch(pendingContribs, () => { void nextTick(measureOverflow) })
// 已展开的行 clamp 已移除（量测不再溢出），控件靠 expanded 兜底继续显示以便收起
function toggleExpand(id: string) { expanded.value = { ...expanded.value, [id]: !expanded.value[id] } }

// ── 采纳前修订（批次ε-1 B2）：后端 KbContributionAcceptRequest 既有契约（缺省=保留原值）。
// 行内表单（问题/正文/归属），仅下发实际变更的字段；空文本前置禁采纳（后端 strip 后会 400）。
const editing = ref<Record<string, { q: string; c: string; dept: string }>>({})
function startRevise(c: ContributionItem) {
  if (isBusy(`ct:${c.contribution_id}`)) return
  editing.value = { ...editing.value, [c.contribution_id]: { q: c.question, c: c.content, dept: c.category_dept } }
}
function cancelRevise(id: string) {
  const n = { ...editing.value }; delete n[id]; editing.value = n
}
// 归属下拉：dept_admin 只列自己管辖的部门（∪ 原归属，保「不改」可选——改到不管的部门
// 只会吃 authorize_upload 403 空转）；kb_admin 全量（孤儿兜底终审者）。
function deptOptsFor(c: ContributionItem) {
  if (isKbAdminRole.value) return CONTRIB_DEPT_OPTS
  const ids = new Set([...(session.identity?.managedOwnerDepts || []), c.category_dept].filter(Boolean))
  return CONTRIB_DEPT_OPTS.filter((o) => ids.has(o.id))
}
function reviseValid(id: string): boolean {
  const e = editing.value[id]
  return !!e && !!e.q.trim() && !!e.c.trim()
}
async function onAcceptRevised(c: ContributionItem) {
  const id = c.contribution_id
  const e = editing.value[id]
  if (!e || !reviseValid(id) || isBusy(`ct:${id}`)) return
  const revised: ContributionRevision = {}
  if (e.q.trim() !== c.question) revised.question = e.q.trim()
  if (e.c.trim() !== c.content) revised.content = e.c.trim()
  if (e.dept !== c.category_dept) revised.category_dept = e.dept
  const ok = await acceptContribution(c, scopeOf(id), Object.keys(revised).length ? revised : undefined)
  if (ok) cancelRevise(id)          // 失败保留编辑内容不丢稿（错误提示走 composable 的 notice）
}
// 改了归属时按钮文案显式带出目标部门（不可逆入库前的零点击确认线索）
function acceptRevisedLabel(c: ContributionItem): string {
  const e = editing.value[c.contribution_id]
  return e && e.dept !== c.category_dept ? `采纳到「${deptLabel(e.dept)}」` : '采纳修订'
}

async function onReject(c: ContributionItem) {
  const reason = await promptText({ title: '驳回贡献', message: `驳回「${c.author_name || c.author_id}」提交的《${c.question}》？`, placeholder: '驳回原因（可空）', confirmText: '驳回', danger: true })
  if (reason === null) return
  void rejectContribution(c, reason || '')
}
</script>

<template>
  <!-- 卡头已带图标+标题+计数，不再另设分区眉标（消除同文案两遍的噪声） -->
  <section v-if="pendingContribs.length || loadErrors['pending']">
    <LoadError class="mb-2.5" :message="loadErrors['pending']" @retry="loadPending()" />
    <div v-if="pendingContribs.length" class="overflow-hidden rounded-[15px] border border-border bg-card">
      <!-- 橙头（待办） -->
      <div class="flex items-center gap-2.5 border-b border-border bg-st-warn/8 px-[18px] py-3">
        <ClipboardCheck :size="16" :stroke-width="1.75" class="text-st-warn" />
        <span class="text-sm font-semibold text-foreground">贡献审核</span>
        <span class="rounded-full bg-st-warn px-2 py-px text-[11px] font-bold text-white">{{ pendingContribs.length }}{{ pendingHasMore ? '+' : '' }}</span>
        <div class="flex-1" />
        <!-- 批次δ-3：业务采纳/驳回归 dept_admin（业务标准在部门）；kb_admin 队列=仅孤儿部门兜底 -->
        <span v-if="isKbAdminRole" class="hidden text-xs text-muted-foreground sm:inline" data-testid="contrib-orphan-hint">
          仅显示暂无部门管理员管辖的部门（兜底）；有管辖的部门由对应部门管理员审核
        </span>
      </div>
      <div
        v-for="c in pendingContribs" :key="c.contribution_id"
        class="border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <!-- 浏览态 -->
        <template v-if="!editing[c.contribution_id]">
          <div class="text-[13.5px] font-semibold text-foreground">{{ c.question }}</div>
          <div
            :ref="bindContentEl(c.contribution_id)" data-testid="contrib-content"
            class="mt-1.5 whitespace-pre-wrap text-[12px] leading-relaxed text-muted-foreground"
            :class="expanded[c.contribution_id] ? '' : 'line-clamp-2'"
          >{{ c.content }}</div>
          <button
            v-if="overflowing[c.contribution_id] || expanded[c.contribution_id]"
            type="button" data-testid="contrib-expand"
            class="mt-1 text-[11.5px] font-medium text-accent-text transition hover:opacity-80"
            @click="toggleExpand(c.contribution_id)"
          >{{ expanded[c.contribution_id] ? '收起' : '展开全文' }}</button>
          <div class="mt-2 flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
            <span class="inline-flex items-center gap-1 text-[11px] text-faint">
              <User :size="11" :stroke-width="2" /> {{ c.author_name || c.author_id }}
            </span>
            <span class="text-[11px] text-faint">· {{ deptLabel(c.category_dept) }}</span>
            <span v-if="c.created_at" class="text-[11px] text-faint">· {{ fmtTs(c.created_at) }}</span>
            <!-- 批次ε-1 B3：缺口溯源徽标（tooltip=原提问）；老后端无字段 → 自隐 -->
            <span
              v-if="c.gap_query || c.source_message_id" data-testid="contrib-from-gap"
              class="rounded bg-accent-soft px-1.5 py-px text-[10.5px] font-medium text-accent-text"
              :title="c.gap_query || undefined"
            >来自缺口</span>
            <!-- 批次ε-3 R2：被问次数（与「来自缺口」独立——自发投稿的高频问题缺口徽标会漏报，
                 asks 恰好补盲）。>0 强调色；0 弱化照显（真零≠未知）；null=算不出自隐 -->
            <span
              v-if="c.asks != null" data-testid="contrib-asks"
              class="rounded px-1.5 py-px text-[10.5px] font-medium tabular-nums"
              :class="c.asks > 0 ? 'bg-accent-soft text-accent-text' : 'bg-panel text-faint'"
              title="近 30 天内检索未命中/未答好的提问里，与此问题相同的去重条数"
            >近 30 天被问 {{ c.asks }} 次</span>
            <div class="flex-1" />
            <select
              :value="scopeOf(c.contribution_id)" :aria-label="`可见范围：${c.question}`"
              :disabled="isBusy(`ct:${c.contribution_id}`)"
              class="ui-select cursor-pointer rounded-lg border border-border bg-card px-2 py-[6px] text-[12px] text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/15 disabled:opacity-50"
              @change="scope[c.contribution_id] = ($event.target as HTMLSelectElement).value as 'dept_internal' | 'public'"
            >
              <option value="dept_internal">部门公开</option>
              <option value="public">全员公开</option>
            </select>
            <button
              type="button" data-testid="contrib-revise" :disabled="isBusy(`ct:${c.contribution_id}`)"
              class="inline-flex items-center justify-center gap-1 rounded-lg border border-border px-3 py-[6px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
              @click="startRevise(c)"
            ><PencilLine :size="13" :stroke-width="2" /> 修订</button>
            <button
              type="button" :disabled="isBusy(`ct:${c.contribution_id}`)"
              class="inline-flex items-center justify-center gap-1 rounded-lg border border-border px-3.5 py-[6px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
              @click="onReject(c)"
            ><Loader2 v-if="isBusy(`ct:${c.contribution_id}`)" :size="13" :stroke-width="2" class="animate-spin" />{{ isBusy(`ct:${c.contribution_id}`) ? '驳回中…' : '驳回' }}</button>
            <button
              type="button" :disabled="isBusy(`ct:${c.contribution_id}`)"
              class="inline-flex items-center justify-center gap-1 rounded-lg bg-primary px-3.5 py-[6px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
              @click="acceptContribution(c, scopeOf(c.contribution_id))"
            ><Loader2 v-if="isBusy(`ct:${c.contribution_id}`)" :size="13" :stroke-width="2" class="animate-spin" />{{ isBusy(`ct:${c.contribution_id}`) ? '采纳中…' : '采纳' }}</button>
          </div>
        </template>
        <!-- 修订态（批次ε-1 B2）：行内三字段表单；取消即弃改还原 -->
        <template v-else>
          <label class="block text-[11px] font-bold uppercase tracking-[0.04em] text-faint">问题</label>
          <input
            v-model="editing[c.contribution_id].q" data-testid="contrib-revise-q" type="text" maxlength="512"
            class="mt-1 w-full rounded-lg border border-border bg-card px-2.5 py-[7px] text-[13px] text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/15"
          />
          <label class="mt-2.5 block text-[11px] font-bold uppercase tracking-[0.04em] text-faint">答案 · 知识内容</label>
          <textarea
            v-model="editing[c.contribution_id].c" data-testid="contrib-revise-c" rows="6"
            class="mt-1 w-full resize-y rounded-lg border border-border bg-card px-2.5 py-[7px] text-[12.5px] leading-relaxed text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/15"
          />
          <div class="mt-2.5 flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
            <label class="text-[11px] font-bold uppercase tracking-[0.04em] text-faint">归属</label>
            <select
              v-model="editing[c.contribution_id].dept" data-testid="contrib-revise-dept"
              class="ui-select cursor-pointer rounded-lg border border-border bg-card px-2 py-[6px] text-[12px] text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/15"
            >
              <option v-for="d in deptOptsFor(c)" :key="d.id" :value="d.id">{{ d.name }}</option>
            </select>
            <div class="flex-1" />
            <select
              :value="scopeOf(c.contribution_id)" :aria-label="`可见范围：${c.question}`"
              :disabled="isBusy(`ct:${c.contribution_id}`)"
              class="ui-select cursor-pointer rounded-lg border border-border bg-card px-2 py-[6px] text-[12px] text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/15 disabled:opacity-50"
              @change="scope[c.contribution_id] = ($event.target as HTMLSelectElement).value as 'dept_internal' | 'public'"
            >
              <option value="dept_internal">部门公开</option>
              <option value="public">全员公开</option>
            </select>
            <button
              type="button" data-testid="contrib-revise-cancel"
              class="rounded-lg border border-border px-3.5 py-[6px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong"
              @click="cancelRevise(c.contribution_id)"
            >取消</button>
            <button
              type="button" data-testid="contrib-accept-revised"
              :disabled="!reviseValid(c.contribution_id) || isBusy(`ct:${c.contribution_id}`)"
              class="inline-flex items-center justify-center gap-1 rounded-lg bg-primary px-3.5 py-[6px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
              @click="onAcceptRevised(c)"
            ><Loader2 v-if="isBusy(`ct:${c.contribution_id}`)" :size="13" :stroke-width="2" class="animate-spin" />{{ isBusy(`ct:${c.contribution_id}`) ? '采纳中…' : acceptRevisedLabel(c) }}</button>
          </div>
        </template>
      </div>
      <!-- 批次ε-1 B4：>50 条时 has_more 此前被静默丢弃（假满员）——与 GapList 同款加载更多 -->
      <div v-if="pendingHasMore" class="border-t border-border p-3 text-center">
        <button
          type="button" data-testid="contrib-load-more"
          class="rounded-lg border border-border px-4 py-1.5 text-[12.5px] font-medium text-foreground transition hover:border-border-strong"
          @click="loadPending(pendingContribs.length)"
        >加载更多</button>
      </div>
    </div>
  </section>
</template>
