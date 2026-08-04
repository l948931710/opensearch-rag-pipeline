<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { UserCog, ShieldCheck, Plus, X, GitBranch, Loader2 } from '@lucide/vue'
import { deptLabel } from '@/lib/kb'
import { useKb, type AdminItem } from '@/composables/useKb'
import LoadError from './LoadError.vue'
import SkeletonBlock from './SkeletonBlock.vue'
import OrgTreePicker, { type PickedNode } from './OrgTreePicker.vue'
import TruncationNotice from '@/components/manage/TruncationNotice.vue'
import { useDialog } from '@/composables/useDialog'

// Phase F 成员/角色管理（kb_admin 专属）：维护部门管理员 + 其可管理 owner_dept（写授权）。
// 三分授权：读组 ≠ 可管理(dept_admin_grant) ≠ 可授权(本面=kb_admin)。kb_admin 行只读受保护。
// 阶段 B：双轴——legacy 组码 + node 管辖根（manual；auto 由组织同步派生，中心级/超规模/换根
// 进下方候选队列待确认）。覆盖语义按轴隔离：不动节点选择器 = 不动节点轴。
const { adminGrants, grantableDepts, isBusy, grantDeptAdmin, revokeAdminGrant, loadAdminGrants, loadErrors,
        nodeAclGrant, nodeCandidates, truncatedQueues, loadNodeCandidates, decideNodeCandidate, isLoading } = useKb()
const { confirm } = useDialog()

const formUser = ref('')
const formName = ref('')
const formDepts = ref<string[]>([])
const formNodes = ref<PickedNode[]>([])       // node 管辖根（manual；「含下级」语义固定，无开关）
const formNodesTouched = ref(false)           // 未动 = 不覆盖节点轴（node_roots 不发）
const formNote = ref('')
const formOpen = ref(false)
const triedSubmit = ref(false)   // 提交过一次后才显内联校验红框（aria-invalid，G9）
const userInvalid = computed(() => triedSubmit.value && !formUser.value.trim())
const deptsInvalid = computed(() =>
  triedSubmit.value && !formDepts.value.length && !(formNodesTouched.value && formNodes.value.length))
const riskLabel = (r: string) => ({
  center_level: '中心级根（覆盖整个中心）', oversized: '子树超规模', root_changed: '调岗换根',
} as Record<string, string>)[r] || r
const candErr = ref('')
async function onDecide(id: number, action: 'confirm' | 'reject') {
  candErr.value = (await decideNodeCandidate(id, action)) || ''
}
onMounted(() => { if (nodeAclGrant.value) void loadNodeCandidates() })

const deptAdmins = computed(() => adminGrants.value.filter((a) => a.role === 'dept_admin'))
const kbAdmins = computed(() => adminGrants.value.filter((a) => a.role === 'kb_admin'))

function toggleDept(d: string) {
  formDepts.value = formDepts.value.includes(d) ? formDepts.value.filter((x) => x !== d) : [...formDepts.value, d]
}
function startEdit(a: AdminItem) {
  formUser.value = a.user_id; formName.value = a.user_name; formDepts.value = [...a.managed_owner_depts]; formNote.value = ''
  formNodes.value = (a.managed_node_roots || []).map((n) => ({ dept_id: n.dept_id, subtree: true }))
  formNodesTouched.value = false
  formOpen.value = true
}
async function submit() {
  triedSubmit.value = true
  const hasNodeAxis = formNodesTouched.value && formNodes.value.length > 0
  if (!formUser.value.trim() || (!formDepts.value.length && !hasNodeAxis)) return   // 两轴任一非空即可
  // 覆盖式语义防误伤：手输已存在的 staffId 提交 = 整体覆盖其全部授权——若有部门将被移除,
  // 先列差异确认(走「编辑」预填不会触发,勾选集 ⊇ 现状时也不打扰)。
  const uid = formUser.value.trim()
  const existing = deptAdmins.value.find((a) => a.user_id === uid)
  const removed = existing ? existing.managed_owner_depts.filter((d) => !formDepts.value.includes(d)) : []
  if (removed.length) {
    const okGo = await confirm({
      title: '覆盖既有授权', confirmText: '确认覆盖', danger: true,
      message: `「${existing!.user_name || uid}」已是部门管理员，提交将【整体覆盖】其授权。\n以下部门的管理权将被移除：${removed.map(deptLabel).join('、')}`,
    })
    if (!okGo) return
  }
  const ok = await grantDeptAdmin(uid, formName.value.trim(), formDepts.value, formNote.value.trim(),
    formNodesTouched.value ? formNodes.value.map((p) => p.dept_id) : undefined)
  if (ok) {
    formUser.value = ''; formName.value = ''; formDepts.value = []; formNote.value = ''
    formNodes.value = []; formNodesTouched.value = false
    formOpen.value = false; triedSubmit.value = false
    if (nodeAclGrant.value) void loadNodeCandidates()
  }
}
async function onRevokeAll(a: AdminItem) {
  const okGo = await confirm({
    title: '撤销全部管理权限', confirmText: '撤销全部', danger: true,
    message: `撤销「${a.user_name || a.user_id}」的全部部门管理权限？\n该用户将降为普通员工（即时失去管理入口）。`,
  })
  if (okGo) void revokeAdminGrant(a.user_id, '')
}
async function onRevokeDept(a: AdminItem, d: string) {
  const okGo = await confirm({
    title: '撤销部门管理权限', confirmText: '撤销', danger: true,
    message: `撤销「${a.user_name || a.user_id}」对【${deptLabel(d)}】的管理权限？`,
  })
  if (okGo) void revokeAdminGrant(a.user_id, d)
}
</script>

<template>
  <section>
    <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">成员 / 角色管理</p>
    <LoadError class="mb-2.5" :message="loadErrors['adminGrants']" @retry="loadAdminGrants()" />
    <div class="overflow-hidden rounded-[15px] border border-border bg-card">
      <div class="flex items-center gap-2.5 border-b border-border bg-accent-soft px-[18px] py-3">
        <UserCog :size="16" :stroke-width="1.75" class="text-accent-text" />
        <span class="text-sm font-semibold text-foreground">部门管理员</span>
        <span class="rounded-full bg-accent-strong px-2 py-px text-[11px] font-bold text-primary-foreground">{{ deptAdmins.length }}</span>
        <div class="flex-1" />
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-lg bg-primary px-3 py-[6px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90"
          @click="formOpen = !formOpen"
        ><Plus :size="14" :stroke-width="2" /> 授予</button>
      </div>

      <!-- 授予/编辑表单 -->
      <div v-if="formOpen" class="space-y-2.5 border-b border-border bg-panel px-[18px] py-3.5">
        <div class="flex flex-wrap gap-2.5">
          <div class="flex flex-col gap-1">
            <input
              v-model="formUser" placeholder="钉钉 staffId" :aria-invalid="userInvalid"
              class="w-44 rounded-lg border bg-card px-3 py-1.5 text-sm text-foreground placeholder:text-faint"
              :class="userInvalid ? 'border-st-fail' : 'border-border'"
            />
            <span v-if="userInvalid" class="text-[11px] text-st-fail">请填钉钉 staffId</span>
          </div>
          <input v-model="formName" placeholder="显示名（可空）" class="w-40 rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-foreground placeholder:text-faint" />
        </div>
        <div>
          <div class="mb-1 text-[11.5px]" :class="deptsInvalid ? 'text-st-fail' : 'text-faint'">
            可管理部门（多选，提交即覆盖该管理员全部授权）<span v-if="deptsInvalid"> · 至少选一个</span>
          </div>
          <div class="flex flex-wrap gap-1.5" :aria-invalid="deptsInvalid">
            <button
              v-for="d in grantableDepts" :key="d" type="button"
              class="rounded-full border px-2.5 py-1 text-[12px] transition"
              :class="formDepts.includes(d) ? 'border-accent-strong bg-accent-soft font-medium text-accent-text' : 'border-border text-muted-foreground hover:border-ring'"
              @click="toggleDept(d)"
            >{{ deptLabel(d) }}</button>
          </div>
        </div>
        <div v-if="nodeAclGrant">
          <div class="mb-1 text-[11.5px] text-faint">
            管辖节点（组织树，manual——覆盖 auto 派生；不动此处 = 不改节点轴）
          </div>
          <div class="max-h-48 overflow-y-auto rounded-lg border border-border bg-card p-2"
               @click.capture="formNodesTouched = true">
            <OrgTreePicker v-model="formNodes" :disabled="isBusy(`member:${formUser.trim()}`)" />
          </div>
        </div>
        <input v-model="formNote" placeholder="备注（可空，如授权依据）" class="w-full rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-foreground placeholder:text-faint" />
        <div class="flex gap-2">
          <button type="button" :disabled="isBusy(`member:${formUser.trim()}`)" class="inline-flex items-center gap-1 rounded-lg bg-primary px-4 py-[7px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50" @click="submit"><Loader2 v-if="isBusy(`member:${formUser.trim()}`)" :size="12" :stroke-width="2" class="animate-spin" />{{ isBusy(`member:${formUser.trim()}`) ? "提交中…" : "提交授予" }}</button>
          <button type="button" class="rounded-lg border border-border px-4 py-[7px] text-[12.5px] text-foreground transition hover:border-border-strong" @click="formOpen = false">取消</button>
        </div>
      </div>

      <!-- 部门管理员行 -->
      <div
        v-for="a in deptAdmins" :key="a.user_id"
        class="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-t border-border px-[18px] py-3"
      >
        <span class="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent-text"><UserCog :size="16" :stroke-width="1.75" /></span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13.5px] font-semibold text-foreground">{{ a.user_name || a.user_id }} <span class="ml-1 text-[11px] font-normal text-faint">{{ a.user_id }}</span></div>
          <div class="mt-1 flex flex-wrap gap-1.5">
            <span v-for="d in a.managed_owner_depts" :key="d" class="inline-flex items-center gap-1 rounded-md bg-panel px-2 py-0.5 text-[11.5px] text-muted-foreground">
              {{ deptLabel(d) }}
              <button type="button" class="text-faint transition hover:text-st-busy disabled:opacity-50" :disabled="isBusy(`member:${a.user_id}`)" @click="onRevokeDept(a, d)"><Loader2 v-if="isBusy(`member:${a.user_id}`)" :size="11" :stroke-width="2.5" class="animate-spin" /><X v-else :size="11" :stroke-width="2.5" /></button>
            </span>
            <span
              v-for="n in (a.managed_node_roots || [])" :key="'n' + n.dept_id"
              class="inline-flex items-center gap-1 rounded-md bg-accent-soft px-2 py-0.5 text-[11.5px] text-accent-text"
              :title="n.source === 'auto' ? '组织同步自动派生' : '手动指定'"
            >
              <GitBranch :size="10" :stroke-width="2" />{{ n.name }}<span v-if="n.source === 'auto'" class="text-[10px] opacity-70">auto</span>
              <button type="button" class="text-faint transition hover:text-st-busy disabled:opacity-50" :disabled="isBusy(`member:${a.user_id}`)" @click="revokeAdminGrant(a.user_id, '', n.dept_id)"><Loader2 v-if="isBusy(`member:${a.user_id}`)" :size="11" :stroke-width="2.5" class="animate-spin" /><X v-else :size="11" :stroke-width="2.5" /></button>
            </span>
            <span v-if="!a.managed_owner_depts.length && !(a.managed_node_roots || []).length" class="text-[11.5px] text-faint">（无可管理部门）</span>
          </div>
        </div>
        <!-- 「编辑」刻意**不加** spinner：它只是打开表单，不是异步操作。这里的 disabled 表达的是
             「被在途的撤销挡住」而不是「正在进行中」，加转圈会把两种状态混成一种谎。 -->
        <button type="button" class="self-start rounded-lg border border-border px-3 py-[6px] text-[12px] text-foreground transition hover:border-border-strong disabled:opacity-50" :disabled="isBusy(`member:${a.user_id}`)" @click="startEdit(a)">编辑</button>
        <button type="button" class="inline-flex items-center gap-1 self-start rounded-lg border border-border px-3 py-[6px] text-[12px] text-foreground transition hover:border-border-strong disabled:opacity-50" :disabled="isBusy(`member:${a.user_id}`)" @click="onRevokeAll(a)"><Loader2 v-if="isBusy(`member:${a.user_id}`)" :size="12" :stroke-width="2" class="animate-spin" />{{ isBusy(`member:${a.user_id}`) ? '撤销中…' : '撤销全部' }}</button>
      </div>
      <!-- 在途期间不得断言「暂无」——那是还没有依据的结论。与 ApprovalHistory 同一形状的缺陷。 -->
      <div v-if="isLoading('adminGrants') && !deptAdmins.length" class="border-t border-border px-[18px] py-4" data-testid="skel-admin-grants">
        <SkeletonBlock :rows="2" :height="44" :widths="['94%', '86%']" />
      </div>
      <!-- 失败与「确认为空」说不同的话：5xx 时数组同样是空的，而「一个都没有」是结论。 -->
      <div v-else-if="loadErrors['adminGrants']" class="border-t border-border px-[18px] py-6 text-center text-sm text-muted-foreground">部门管理员名单暂不可用——上方错误条可重试。</div>
      <div v-else-if="!deptAdmins.length" class="border-t border-border px-[18px] py-6 text-center text-sm text-muted-foreground">暂无部门管理员，点「授予」添加</div>

      <!-- 知识库管理员（只读，受保护） -->
      <div
        v-for="a in kbAdmins" :key="a.user_id"
        class="flex items-center gap-2.5 border-t border-border bg-st-live/5 px-[18px] py-2.5"
      >
        <ShieldCheck :size="15" :stroke-width="1.75" class="shrink-0 text-st-live" />
        <span class="text-[13px] text-foreground">{{ a.user_name || a.user_id }} <span class="ml-1 text-[11px] text-faint">{{ a.user_id }}</span></span>
        <span class="rounded-md bg-st-live/10 px-1.5 py-0.5 text-[10.5px] font-medium text-st-live">知识库管理员</span>
        <div class="flex-1" />
        <span class="hidden text-[11px] text-faint sm:inline">全部门 · 受保护（调整走运维脚本）</span>
      </div>
    </div>

    <!-- 阶段 B：管辖根候选确认队列（org_sync 派生的中心级/超规模/换根，权威表绝不进待确认态） -->
    <div v-if="nodeAclGrant && nodeCandidates.length" class="mt-4 overflow-hidden rounded-[15px] border border-border bg-card">
      <div class="flex items-center gap-2.5 border-b border-border bg-st-busy/10 px-[18px] py-3">
        <GitBranch :size="16" :stroke-width="1.75" class="text-st-busy" />
        <span class="text-sm font-semibold text-foreground">管辖根待确认</span>
        <span class="rounded-full bg-st-busy px-2 py-px text-[11px] font-bold text-white">{{ nodeCandidates.length }}</span>
      </div>
      <p v-if="candErr" class="border-b border-border px-[18px] py-2 text-[11.5px] text-danger">{{ candErr }}</p>
      <div v-for="c in nodeCandidates" :key="c.id"
           class="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-t border-border px-[18px] py-3">
        <div class="min-w-0 flex-1">
          <div class="text-[13px] text-foreground">
            {{ c.user_name || c.user_id }} → <b>{{ c.dept_name || c.dept_id }}</b>
          </div>
          <div class="mt-0.5 text-[11.5px] text-st-busy">{{ riskLabel(c.risk_reason) }} · 快照 rev {{ c.derived_snapshot_rev }}</div>
        </div>
        <!-- 这两个原先是全组件唯一**零防护**的写按钮：无 :disabled、useKb 侧也无 busy 追踪，
             连点可并发发出多个决策请求，且在途期间界面零反应。补上与本文件其余 5 个按钮
             同一套 isBusy（useKb 的 withInflight key 为 `cand:${id}`），并套用全站既有的
             Loader2 范式。 -->
        <button type="button" :disabled="isBusy(`cand:${c.id}`)" data-testid="cand-confirm"
                class="inline-flex items-center gap-1 rounded-lg bg-primary px-3 py-[6px] text-[12px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
                @click="onDecide(c.id, 'confirm')"><Loader2 v-if="isBusy(`cand:${c.id}`)" :size="12" :stroke-width="2" class="animate-spin" />{{ isBusy(`cand:${c.id}`) ? '处理中…' : '确认生效' }}</button>
        <button type="button" :disabled="isBusy(`cand:${c.id}`)" data-testid="cand-reject"
                class="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-[6px] text-[12px] text-foreground transition hover:border-border-strong disabled:opacity-50"
                @click="onDecide(c.id, 'reject')"><Loader2 v-if="isBusy(`cand:${c.id}`)" :size="12" :stroke-width="2" class="animate-spin" />驳回</button>
      </div>
      <!-- P3-3：后端硬 LIMIT 200 的截断此前完全静默 —— 被截掉的候选永远不被裁决。 -->
      <TruncationNotice class="px-[18px] pb-3" :when="truncatedQueues.nodeCandidates" :cap="200" label="待裁决候选" hint="先处理完当前批再刷新" />
    </div>
  </section>
</template>
