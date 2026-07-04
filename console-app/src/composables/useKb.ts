import { computed, ref } from 'vue'
import { apiJson, ApiError } from '@/lib/api'
import { useSession } from '@/stores/session'
import {
  GROUP_LABEL, MAX_UPLOAD_MB, TERMINAL_BADGES, deptLabel, putWithProgress, uploadErrText, buildDupMsg, fileCore, unsupportedNames, type DupDoc,
} from '@/lib/kb'

// 知识库管理台单例 store。身份/可管部门复用 P1 的 session（whoami 已给 managed_owner_depts），
// 不再走旧 console 的 org-tree。所有写接口后端【现查】授权，前端 role 仅作 UI 门禁。

export interface DocItem {
  doc_id: string; title: string; original_filename: string; owner_dept: string
  permission_level: string; current_version_no: number; status: string
  status_badge: string; updated_at: string
  can_manage?: boolean   // 可操作（写作用域）；my-docs 恒 true，browse 全部门时外部门为 false
  cited_count?: number | null   // 利用度：被引用问答数（null/undefined=数据不可用，0=真·从未被引用）
  last_cited_at?: string        // 最近被引用时间（cited_count>0 时有值）
}
export interface PendingItem {
  doc_id: string; version_no: number; title: string; original_filename: string
  owner_dept: string; permission_level: string; owner_name: string; created_at: string
}
// 授权申请（Phase C）：其他部门申请检索本部门文档；审批人 = 文档所属部门管理员（锁定决策 2026-06-26）。
// 后端 /api/kb/access-requests 尚未上线 → loadAccessRequests 静默兜底空；DEV ?preview 注入 mock 以可视化。
export interface AccessRequestItem {
  id: string; doc_id: string; doc_title: string; owner_dept: string
  requester_dept: string; requester_name: string; permission_level: string; reason: string; created_at: string
}
// 审批方侧：已放行（approved）的跨部门授权存量（后端 /api/kb/access-grants）——供「已授权清单」展示 + 撤销。
export interface AccessGrantItem {
  id: string; doc_id: string; doc_title: string; owner_dept: string
  requester_dept: string; requester_name: string; permission_level: string; reason: string; decided_at: string
}
// 审批历史（只读聚合 /api/kb/approval-history）：四条审批流的【历史决策】合并时间线。
// dept_admin 见 access+contribution（本部门）；kb_admin 见全部四类（全库，含 upload/admin_grant）。
export interface ApprovalHistoryItem {
  kind: string            // 'access' | 'contribution' | 'upload' | 'admin_grant'
  action: string          // approved | rejected | revoked | accepted | granted
  title: string; owner_dept: string; subject: string
  detail: string; extra: string
  decided_by: string; decided_by_name: string; decided_at: string
}
// Phase F 成员/角色管理（kb_admin 专属）：现行管理员 + 各自可管理 owner_dept（后端 /api/kb/admin-grants）。
export interface AdminItem {
  user_id: string; user_name: string; role: string; managed_owner_depts: string[]
}
// 申请人侧：我的申请 + 派生同步态（已批准·待同步 vs 已放行；后端 /api/kb/my-access-requests）。
export interface MyAccessRequestItem {
  id: string; doc_id: string; doc_title: string; owner_dept: string; requester_dept: string
  status: string                 // pending / approved / rejected
  sync_state: string             // n/a | pending_sync | projected
  reason: string; created_at: string; decided_at: string
}
export type AccessState = 'none' | 'pending' | 'approved_pending_sync' | 'projected'
export interface QueueRow { name: string; status: string; pct: number; msg: string; dupMsg?: string }
export interface VerCtx { doc_id: string; title: string; owner_dept: string; permission_level: string; current_version_no: number }

interface MyDocsResp { items: DocItem[]; has_more: boolean }
export interface KbStats { total: number; active: number; retired: number; chunks: number; new_this_month: number; by_badge: Record<string, number> }
// Phase E 概览看板真实数据（镜像 api.py KbInsightsResponse / KbGovernanceResponse，字段一一对应）
export interface KbTopDoc { title: string; owner_dept: string; hits: number }
export interface KbGapQuery { query: string; count: number; avg_top: number }
export interface KbInsights {
  scope: string; window_days: number
  questions: number; askers: number; success: number; refusal: number; cited: number; helped_users: number; effective_rate: number
  top_docs: KbTopDoc[]; gap_queries: KbGapQuery[]
}
export interface KbEmbedRun { bizdate: string; embedded: number; failed: number; fail_rate: number }
export interface KbDeptCoverage { owner_dept: string; docs: number; new_month: number; qa_hits: number; no_answer_rate: number; pii_docs: number; wow_net?: number | null; wow_total?: number | null; qa_wow_net?: number | null; qa_wow?: number | null }
export interface KbFeedbackDay { day: string; up: number; down: number }
export interface KbDownvoteReason { reason: string; count: number }
export interface KbFileType { ftype: string; count: number }
export interface KbGovernance {
  window_days: number
  file_types: KbFileType[]
  docs_active: number; docs_in_index: number; dual_version_docs: number
  avg_latency_ms: number; p50_latency_ms: number; p95_latency_ms: number
  avg_retrieval_ms: number; avg_llm_ms: number; embed_runs: KbEmbedRun[]
  qa_api_success_rate: number; retrieval_api_success_rate: number; errors_24h: number; qa_total_30d: number
  pii_redacted_docs: number; pii_quarantined_docs: number
  answer_total: number; answer_success: number; answer_refusal: number; answer_no_result: number; answer_error: number
  effective_rate: number
  feedback_up: number; feedback_down: number; feedback_total: number; helpful_rate: number
  feedback_last7: number; feedback_daily: KbFeedbackDay[]; downvote_reasons: KbDownvoteReason[]
  escalations: number
  dept_coverage: KbDeptCoverage[]
}
export interface KbConfig { max_upload_bytes: number; accepted_exts: string[] }
export interface VersionItem {
  version_no: number; content_process_status: string; chunk_status: string
  index_status: string; publish_status: string; status_badge: string; error_message: string; created_at: string
}
interface UploadUrlResp { upload_token: string; put_url: string; raw_key: string; doc_id: string; expires_in: number; requires_kb_admin_approval: boolean; content_type?: string }
interface RegisterResp { doc_id: string; version_no: number; content_process_status: string; requires_kb_admin_approval: boolean; status_badge: string; idempotent: boolean; title: string; content_dups: DupDoc[]; content_dups_other: number }
interface DocStatusResp { status_badge: string; chunk_active: number; error_message: string }
interface RetireResp { status: string; retired: boolean; already: boolean; status_badge: string; note: string }

export type SortKey = 'updated_at' | 'current_version_no' | 'title' | 'owner_dept' | 'status_badge'

// ── 状态 ──
const docs = ref<DocItem[]>([])
const kbStats = ref<KbStats | null>(null)   // 全作用域聚合（真实总数/状态分布，不受 my-docs 50 上限影响）
const kbInsights = ref<KbInsights | null>(null)     // Phase E：使用成效 + 知识缺口（owner 作用域）
const kbGovernance = ref<KbGovernance | null>(null) // Phase E：全库运行健康/治理风险/部门覆盖（kb_admin）
const kbConfig = ref<KbConfig | null>(null) // 后端能力配置（上传上限/类型）；缺省用常量兜底
const maxUploadBytes = computed(() => kbConfig.value?.max_upload_bytes || MAX_UPLOAD_MB * 1048576)
const maxUploadMb = computed(() => Math.round(maxUploadBytes.value / 1048576))
const verHistory = ref<{ doc: DocItem | null; versions: VersionItem[]; loading: boolean; error: string } | null>(null)
const approvals = ref<PendingItem[]>([])
const accessRequests = ref<AccessRequestItem[]>([])   // 授权申请队列（审批人侧 · pending）
const accessGrants = ref<AccessGrantItem[]>([])       // 已授权清单（审批人侧 · approved 存量，供撤销）
const approvalHistory = ref<ApprovalHistoryItem[]>([]) // 审批历史（只读聚合，四流合并时间线）
const adminGrants = ref<AdminItem[]>([])              // Phase F 现行管理员名单（kb_admin 专属）
const grantableDepts = ref<string[]>([])             // 授予表单可选 owner_dept（写白名单）
const loadingDocs = ref(false)
const loadingMoreDocs = ref(false)                   // 「加载更多」翻页中（与首屏 loadingDocs 区分）
const hasMoreDocs = ref(false)                        // 服务端还有下一页（消费 my-docs/browse 的 has_more）
const docScope = ref<'managed' | 'all'>('managed')   // 本部门(my-docs) / 全部门只读浏览(browse)
// 授权申请（申请人侧，Phase C）：本会话内已申请的 doc_id（无后端持久化，仅即时反映「审批中」）。
const accessReqDoc = ref<DocItem | null>(null)
const accessReqBusy = ref(false)
const requestedDocIds = ref<Set<string>>(new Set())   // 乐观：本会话刚提交、服务端态尚未回灌前临时显「审批中」
// 申请人侧权威态：doc_id → {status, sync_state}（拉自 /api/kb/my-access-requests；后端未上线则空）
const myAccessReqs = ref<Map<string, { status: string; sync_state: string }>>(new Map())
const q = ref('')
const filter = ref('')                 // status_badge 精确过滤；'' = 全部
const permFilter = ref('')             // permission_level 精确过滤；'' = 全部
const ownerFilter = ref('')            // owner_dept（含生产子线）精确过滤；'' = 全部
const citedFilter = ref('')            // 利用度筛选：'' 全部 / 'never' 从未被引用（退役候选）/ 'used' 有引用
const sortKey = ref<SortKey>('updated_at')
const sortDir = ref<1 | -1>(-1)
// 多选（批量操作）：选中的 doc_id 集合。仅对可管理(can_manage!==false)行有意义；筛选/换源时自动收敛。
const selectedIds = ref<Set<string>>(new Set())
const bulkBusy = ref(false)
const bulkMsg = ref('')

// 上传表单 / 状态
const newTitle = ref('')
const newOwner = ref('')
const newPerm = ref('dept_internal')          // dept_internal / shared（=dept_internal+主动授权）/ public / restricted
const newShareDepts = ref<string[]>([])       // newPerm='shared' 时的共享目标组码
// 主动共享弹窗（owner 侧）：把自己部门的 dept_internal 文档直接放行给指定部门（POST /api/kb/access-grants）。
const shareCtx = ref<DocItem | null>(null)
const shareBusy = ref(false)
// 「谁能看到这篇文档」解释器弹窗（只读）：GET /api/kb/visibility-explain。
const visCtx = ref<DocItem | null>(null)
const visExplain = ref<VisExplain | null>(null)
const visLoading = ref(false)
const visErr = ref('')
// 差评联动复核（看板卡片）：引用了我作用域文档的回答收到 👎。null=尚未加载（显占位不显 0）。
const feedbackReview = ref<FeedbackReviewItem[] | null>(null)
// 共享目标可选项 = 10 个用户面 ACL 组码（与后端 sanitize 白名单同源；生产子线是 owner 粒度、非读者组）。
const SHARE_TARGETS = Object.keys(GROUP_LABEL)
const verCtx = ref<VerCtx | null>(null)
const uploadBusy = ref(false)
const uploadMsg = ref('')
const uploadErr = ref('')
const uploadOk = ref(false)
const dupWarn = ref('')                // 文件名级预查重
const contentDupMsg = ref('')          // ETag 内容级查重
const uploadQueue = ref<QueueRow[]>([])
const selectedNames = ref<string[]>([])
// 在途互斥（按行/按用户 key）：一行审批/撤销不再禁用其他不相关按钮（B5）。
const inflight = ref<Set<string>>(new Set())
const retireBusy = ref(false)

// File 真身【绝不进响应式】（Vue3 Proxy 会破坏 xhr.send(file)）——留模块闭包。
let selectedFiles: File[] = []
const DOCS_PAGE = 50                  // 台账翻页页大小（= 后端 limit 上限）
let docsOffset = 0                    // 当前已加载到的 offset（首屏 0；每翻一页 +DOCS_PAGE）
let docsSeq = 0
let trackSeq = 0
let qTimer: ReturnType<typeof setTimeout> | null = null
let trackTimer: ReturnType<typeof setTimeout> | null = null   // 当前轮询定时器句柄（可取消）

// 各分区加载错误（key→提示文案）。诚实区分：404（端点未上线，Phase C/D 可选）→ 静默兜底空；
// 5xx/网络/其他 → 置错误条，组件显「加载失败 + 重试」，不再把服务端故障伪装成「无数据」。
const loadErrors = ref<Record<string, string>>({})
function noteLoadError(key: string, e: unknown) {
  if (e instanceof ApiError && e.status === 404) { delete loadErrors.value[key]; return }
  loadErrors.value[key] = '加载失败，请重试'
}
function clearLoadError(key: string) { delete loadErrors.value[key] }

// staleness 门（#82）：App ready 已为侧栏红点预载 approvals/accessRequests，30s 内再进 /manage 不重拉。
// 起跑即记时间戳 → 预载尚在途时挂载视图也不会并发双拉；失败清零重开门（LoadError 重试按钮走非 force
// 也能真重拉）。数据确实变化的写路径（上传产生待审批单等）传 force=true 逃生。
const STALE_MS = 30_000
const lastLoadedAt: Record<string, number> = {}
function freshEnough(key: string): boolean { return Date.now() - (lastLoadedAt[key] || 0) < STALE_MS }

// 在途互斥：按 key（行/用户）加锁，避免一键禁用无关按钮 + 防同行重复提交。组件用 isBusy(key) 绑 :disabled。
function isBusy(key: string): boolean { return inflight.value.has(key) }
async function withInflight<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
  if (inflight.value.has(key)) return undefined
  inflight.value = new Set(inflight.value).add(key)
  try { return await fn() } finally { const n = new Set(inflight.value); n.delete(key); inflight.value = n }
}

function sortDocs(list: DocItem[], key: SortKey, dir: 1 | -1): DocItem[] {
  return [...list].sort((a, b) => {
    let r: number
    if (key === 'current_version_no') r = (Number(a[key]) || 0) - (Number(b[key]) || 0)
    else r = String(a[key] ?? '').localeCompare(String(b[key] ?? ''))
    return r * dir
  })
}

const filtered = computed(() =>
  sortDocs(docs.value.filter((d) =>
    (!filter.value || d.status_badge === filter.value)
    && (!permFilter.value || d.permission_level === permFilter.value)
    && (!ownerFilter.value || d.owner_dept === ownerFilter.value)
    // 利用度：never=真·从未被引用（cited_count===0，退役候选）；used=有引用。
    // 数据不可用（null/undefined，RAG_QA_FACT_JOIN 未开）两个档都不入——0 与「不知道」必须可区分。
    && (!citedFilter.value || (citedFilter.value === 'never' ? d.cited_count === 0 : (d.cited_count ?? 0) > 0))
  ), sortKey.value, sortDir.value))

// 归属筛选选项 = 已加载文档里出现过的 owner_dept（含生产子线，如 production_mold）→ 覆盖"按子部门管理"。
const ownerOptions = computed(() => Array.from(new Set(docs.value.map((d) => d.owner_dept).filter(Boolean))).sort())

function countOf(badge: string): number {
  return badge ? docs.value.filter((d) => d.status_badge === badge).length : docs.value.length
}

// ── 多选 / 批量 ──────────────────────────────────────────────────
// 当前可见【且可管理】的行（批量只作用于这些；外部门只读行不可批量操作）。
const selectableVisible = computed(() => filtered.value.filter((d) => d.can_manage !== false))
// 选中集与可见集的交集（筛选后自动收敛：切筛选/换源，隐藏行虽仍在 set 里但不参与操作/计数）。
const selectedDocs = computed(() => selectableVisible.value.filter((d) => selectedIds.value.has(d.doc_id)))
const selectedCount = computed(() => selectedDocs.value.length)
const allVisibleSelected = computed(() =>
  selectableVisible.value.length > 0 && selectedDocs.value.length === selectableVisible.value.length)
function isSelected(id: string): boolean { return selectedIds.value.has(id) }
function toggleSelect(id: string) {
  const n = new Set(selectedIds.value)
  if (n.has(id)) n.delete(id); else n.add(id)
  selectedIds.value = n
}
function toggleSelectAllVisible() {
  // 已全选可见 → 清空；否则把当前可见可管理行全选（不动隐藏行的既有选中）。
  const n = new Set(selectedIds.value)
  const vis = selectableVisible.value.map((d) => d.doc_id)
  if (allVisibleSelected.value) vis.forEach((id) => n.delete(id))
  else vis.forEach((id) => n.add(id))
  selectedIds.value = n
}
function clearSelection() { selectedIds.value = new Set(); bulkMsg.value = '' }

// 批量执行器：顺序跑（aux 限流 30/分，串行最稳）+ 进度回填 bulkMsg；失败隔离，末尾一次 loadDocs。
async function _bulkRun(docsToRun: DocItem[], label: string,
                        one: (d: DocItem) => Promise<string | null>): Promise<void> {
  if (bulkBusy.value || !docsToRun.length) return
  bulkBusy.value = true
  let ok = 0; const fails: string[] = []
  for (let i = 0; i < docsToRun.length; i++) {
    bulkMsg.value = `${label}中… ${i + 1}/${docsToRun.length}`
    const err = await one(docsToRun[i])
    if (err) fails.push(`${docsToRun[i].title || docsToRun[i].doc_id}：${err}`); else ok++
  }
  bulkBusy.value = false
  bulkMsg.value = `${label}完成：成功 ${ok}${fails.length ? `，失败 ${fails.length}` : ''}`
  if (fails.length) alert(`${label}部分失败：\n` + fails.slice(0, 8).join('\n'))
  if (ok) { clearSelectionKeepMsg(); void loadDocs() }   // 成功过 → 清选中 + 权威重拉
}
function clearSelectionKeepMsg() { selectedIds.value = new Set() }

/** 批量退役（复用单篇 retire 端点，逐篇顺序）。调用方已二次确认。 */
async function bulkRetire(): Promise<void> {
  const targets = selectedDocs.value.filter((d) => d.status_badge !== '已退役')
  await _bulkRun(targets, '退役', async (d) => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { d.status_badge = '已退役'; return null }
      await apiJson('/api/kb/retire', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id }) })
      return null
    } catch (e: any) { return e && e.status === 403 ? (e.detail || '无权退役') : uploadErrText(e) }
  })
}

/** 批量改可见范围（复用 set-visibility；public 涉全公司需 kb_admin，调用方按角色限制选项）。 */
async function bulkSetVisibility(level: string): Promise<void> {
  const targets = selectedDocs.value.filter((d) => d.permission_level !== level)
  // reload:false——逐篇重拉会 N+1 次全量列表；_bulkRun 批末统一一次权威 loadDocs
  await _bulkRun(targets, '改可见范围', (d) => setVisibility(d, level, '批量调整', { reload: false }))
}

function sortBy(key: SortKey) {
  if (sortKey.value === key) sortDir.value = (sortDir.value === 1 ? -1 : 1)
  else { sortKey.value = key; sortDir.value = key === 'updated_at' ? -1 : 1 }
}

function patchRow(docId: string, badge: string) {
  const d = docs.value.find((x) => x.doc_id === docId)
  if (d) d.status_badge = badge
}

// 台账列表 URL：scope 分流（全部门 browse / 本部门 my-docs）+ 分页（limit/offset）+ 文档名搜索。
function docsUrl(offset: number): string {
  const params = new URLSearchParams()
  if (docScope.value === 'all') params.set('scope', 'all')
  params.set('limit', String(DOCS_PAGE))
  params.set('offset', String(offset))
  if (q.value) params.set('q', q.value)
  const path = docScope.value === 'all' ? '/api/kb/browse' : '/api/kb/my-docs'
  return `${path}?${params.toString()}`
}

async function loadDocs() {
  const seq = ++docsSeq
  loadingDocs.value = true
  docsOffset = 0
  clearLoadError('docs')
  try {
    // DEV ?preview：注入 mock（含外部门 can_manage=false 行）以可视化全部门只读浏览；prod 死代码消除。
    if (import.meta.env.DEV && useSession().token === 'dev-preview') {
      const mine: DocItem[] = [
        { doc_id: 'm1', title: '营销物料使用规范 v3', original_filename: 'guideline.pdf', owner_dept: 'marketing', permission_level: 'dept_internal', current_version_no: 3, status: 'active', status_badge: '已上线', updated_at: '2026-06-26 10:00', can_manage: true, cited_count: 12, last_cited_at: '2026-07-02 09:12' },
        { doc_id: 'm2', title: '品牌 VI 手册', original_filename: 'vi.pdf', owner_dept: 'marketing', permission_level: 'public', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '2026-06-20 09:00', can_manage: true, cited_count: 0 },
        { doc_id: 'm3', title: '618 活动复盘', original_filename: '618.docx', owner_dept: 'marketing', permission_level: 'dept_internal', current_version_no: 2, status: 'active', status_badge: '处理中', updated_at: '2026-06-25 14:00', can_manage: true },
      ]
      const foreign: DocItem[] = [
        { doc_id: 'h1', title: '员工考勤管理制度', original_filename: 'attendance.pdf', owner_dept: 'hr', permission_level: 'dept_internal', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '2026-06-24 11:00', can_manage: false },
        { doc_id: 'f1', title: '差旅报销标准', original_filename: 'travel.xlsx', owner_dept: 'finance', permission_level: 'public', current_version_no: 2, status: 'active', status_badge: '已上线', updated_at: '2026-06-22 16:00', can_manage: false },
        { doc_id: 'p1', title: '注塑车间作业指导书', original_filename: 'sop.docx', owner_dept: 'production', permission_level: 'dept_internal', current_version_no: 5, status: 'active', status_badge: '已上线', updated_at: '2026-06-19 08:00', can_manage: false },
      ]
      docs.value = docScope.value === 'all' ? [...mine, ...foreign] : mine
      hasMoreDocs.value = false
      return
    }
    // 作用域分流：全部门走只读 browse（排除 restricted、带 can_manage），本部门走 my-docs。
    const r = await apiJson<MyDocsResp>(docsUrl(0), { auth: true })
    if (seq !== docsSeq) return            // 竞态守卫：仅最新结果落地
    docs.value = r.items || []
    hasMoreDocs.value = !!r.has_more       // 服务端探测到下一页 → 显「加载更多」
  } catch (e) { if (seq === docsSeq) noteLoadError('docs', e) /* 保留旧表 */ } finally { if (seq === docsSeq) loadingDocs.value = false }
}

// 加载下一页并【追加】到当前列表（不自增 docsSeq：追加属于当前列表；期间若 loadDocs/换 scope/搜索
// 触发，docsSeq 变化 → 本页结果作废丢弃，避免错插到新列表）。
async function loadMoreDocs() {
  if (loadingMoreDocs.value || !hasMoreDocs.value) return
  const seq = docsSeq
  loadingMoreDocs.value = true
  try {
    const nextOffset = docsOffset + DOCS_PAGE
    const r = await apiJson<MyDocsResp>(docsUrl(nextOffset), { auth: true })
    if (seq !== docsSeq) return            // 期间列表已被重置 → 丢弃本页
    docs.value = [...docs.value, ...(r.items || [])]
    docsOffset = nextOffset
    hasMoreDocs.value = !!r.has_more
  } catch { /* 保留现有列表，hasMore 不变可重试 */ } finally { loadingMoreDocs.value = false }
}

// 切换台账作用域（本部门 ↔ 全部门只读）。切换即清状态筛选（两个集合徽章分布不同）并重载。
function setScope(s: 'managed' | 'all') {
  if (docScope.value === s) return
  docScope.value = s
  filter.value = ''; permFilter.value = ''; ownerFilter.value = ''; citedFilter.value = ''
  selectedIds.value = new Set(); bulkMsg.value = ''   // 换源清空选中（旧 doc_id 不再可见）
  void loadDocs()
  if (s === 'all') void loadMyAccessRequests()   // 全部门浏览：回灌我的申请态以渲染 申请授权/审批中/同步中/已放行
}

// ── 授权申请（申请人侧）：对其他部门文档发起检索授权申请 ──
function openAccessRequest(d: DocItem) { accessReqDoc.value = d }
function closeAccessRequest() { accessReqDoc.value = null }
function accessStateOf(docId: string): AccessState {
  const r = myAccessReqs.value.get(docId)
  if (r?.status === 'approved') return r.sync_state === 'projected' ? 'projected' : 'approved_pending_sync'
  if (r?.status === 'pending') return 'pending'
  // 服务端无 row 或已驳回 → 看本会话乐观标记（刚提交、态未回灌前）；否则未申请
  return requestedDocIds.value.has(docId) ? 'pending' : 'none'
}
// 申请人侧权威态：拉我的申请 + 派生同步态。后端未上线 / 无申请 → 静默空（不报错、不打扰）。
async function loadMyAccessRequests() {
  try {
    const r = await apiJson<{ items: MyAccessRequestItem[] }>('/api/kb/my-access-requests', { auth: true })
    const m = new Map<string, { status: string; sync_state: string }>()
    // 后端按 created_at DESC（最新在前）返回；每 doc 保留【最新】一行——拒后重申/撤销后重申会留多行，
    // 若 last-write-wins（直接 m.set）会让最旧行覆盖最新 → 误显「申请授权」。首见即最新 → 不覆盖。
    for (const it of (r.items || [])) if (!m.has(it.doc_id)) m.set(it.doc_id, { status: it.status, sync_state: it.sync_state })
    myAccessReqs.value = m
    // 权威已有该 doc 的行 → 清掉本会话乐观标记（否则被驳回/撤销的文档因乐观回退仍显「审批中」、抑制「申请授权」按钮）。
    if (requestedDocIds.value.size) {
      const next = new Set(requestedDocIds.value)
      let changed = false
      for (const id of next) if (m.has(id)) { next.delete(id); changed = true }
      if (changed) requestedDocIds.value = next
    }
  } catch { /* 兜底空 */ }
}
async function submitAccessRequest(reason: string) {
  const d = accessReqDoc.value
  if (!d || accessReqBusy.value) return
  accessReqBusy.value = true
  try {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') {   // 预览演示：本地标记审批中
      requestedDocIds.value = new Set(requestedDocIds.value).add(d.doc_id)
      accessReqDoc.value = null
      return
    }
    await apiJson('/api/kb/access-requests', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id, owner_dept: d.owner_dept, reason }) })
    requestedDocIds.value = new Set(requestedDocIds.value).add(d.doc_id)
    accessReqDoc.value = null
    void loadMyAccessRequests()   // 提交后回灌权威态（pending）；乐观标记保证即时反馈
  } catch (e: any) {
    // 后端（Phase C）未上线 → 404：诚实告知，不伪造「已提交」。
    alert(e && e.status === 404 ? '授权申请功能即将上线，敬请期待。' : '提交失败：' + uploadErrText(e))
  } finally { accessReqBusy.value = false }
}

async function loadStats() {
  // 概览真实口径（总数/状态分布/已索引分块）；失败则前端兜底用已加载文档计数（docs.length / countOf）。
  const s = useSession()
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    kbStats.value = s.role === 'kb_admin'
      ? { total: 1618, active: 1475, retired: 143, chunks: 27659, new_this_month: 1249, by_badge: { 已上线: 1475, 处理中: 8, 排队中: 4, 已退役: 143 } }
      : { total: 42, active: 40, retired: 2, chunks: 612, new_this_month: 6, by_badge: { 已上线: 38, 待审核: 3, 处理中: 1 } }
    return
  }
  clearLoadError('stats')
  try { kbStats.value = await apiJson<KbStats>('/api/kb/stats', { auth: true }) } catch (e) { noteLoadError('stats', e) /* 兜底 */ }
}

async function loadConfig() {
  // 上传上限/类型走后端权威，避免硬编码漂移（失败则用 MAX_UPLOAD_MB 常量兜底）。
  try { kbConfig.value = await apiJson<KbConfig>('/api/kb/config', { auth: true }) } catch { /* 兜底 */ }
}

// ── Phase E：概览看板真实数据（缺数据/端点未上线 → 静默兜底 null，由组件如实显空/加载中）──
// DEV ?preview 注入 mock（取自真实口径量级，便于设计走查）；prod build 死代码消除。
async function loadInsights() {
  const s = useSession()
  if (!s.identity?.canManage) { kbInsights.value = null; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    kbInsights.value = {
      scope: s.role === 'kb_admin' ? 'global' : 'dept', window_days: 30,
      questions: 186, askers: 40, success: 143, refusal: 43, cited: 130, helped_users: 37, effective_rate: 0.769,
      top_docs: [
        { title: 'FL-GJMY-WI-008《下达销售订单》作业指导书.docx', owner_dept: 'marketing', hits: 64 },
        { title: '亚马逊运营SOP（标准化流程）.docx', owner_dept: 'marketing', hits: 51 },
        { title: '客户投诉处理 SOP.pdf', owner_dept: 'marketing', hits: 33 },
      ],
      gap_queries: [
        { query: '2ozpp杯在龙盛机上的速度', count: 2, avg_top: 0.729 },
        { query: '由此写一封英文信', count: 1, avg_top: 0.617 },
      ],
    }
    return
  }
  clearLoadError('insights')
  try { kbInsights.value = await apiJson<KbInsights>('/api/kb/insights', { auth: true }) } catch (e) { noteLoadError('insights', e) /* 兜底 */ }
}

async function loadGovernance() {
  const s = useSession()
  if (s.role !== 'kb_admin') { kbGovernance.value = null; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    kbGovernance.value = {
      window_days: 30, docs_active: 1618, docs_in_index: 1475, dual_version_docs: 0,
      file_types: [
        { ftype: 'PDF', count: 628 }, { ftype: 'DOCX', count: 607 }, { ftype: 'XLSX', count: 313 },
        { ftype: 'PPTX', count: 5 }, { ftype: '图片', count: 6 },
      ],
      qa_api_success_rate: 0.974, retrieval_api_success_rate: 0.974, errors_24h: 0, qa_total_30d: 951,
      avg_latency_ms: 14035, p50_latency_ms: 8106, p95_latency_ms: 54994, avg_retrieval_ms: 1538, avg_llm_ms: 12428,
      embed_runs: [
        { bizdate: '2026-06-23', embedded: 117, failed: 0, fail_rate: 0 },
        { bizdate: '2026-06-22', embedded: 96, failed: 0, fail_rate: 0 },
        { bizdate: '2026-06-21', embedded: 228, failed: 0, fail_rate: 0 },
      ],
      pii_redacted_docs: 475, pii_quarantined_docs: 3,
      answer_total: 902, answer_success: 790, answer_refusal: 112, answer_no_result: 15, answer_error: 25,
      effective_rate: 0.876,
      feedback_up: 64, feedback_down: 44, feedback_total: 108, helpful_rate: 0.593,
      feedback_last7: 5, escalations: 19,
      feedback_daily: [
        { day: '2026-06-15', up: 4, down: 4 }, { day: '2026-06-16', up: 9, down: 0 },
        { day: '2026-06-17', up: 1, down: 7 }, { day: '2026-06-18', up: 3, down: 21 },
        { day: '2026-06-20', up: 1, down: 0 }, { day: '2026-06-22', up: 0, down: 2 },
        { day: '2026-06-24', up: 0, down: 1 }, { day: '2026-06-26', up: 1, down: 1 },
      ],
      downvote_reasons: [
        { reason: '其他', count: 14 }, { reason: '不准确', count: 12 }, { reason: '不相关', count: 8 },
        { reason: '不完整', count: 8 }, { reason: '已过时', count: 2 }, { reason: '未注明', count: 2 },
      ],
      dept_coverage: [
        { owner_dept: 'production', docs: 800, new_month: 711, qa_hits: 303, no_answer_rate: 0.221, pii_docs: 247, wow_net: 30, wow_total: 0.04, qa_wow_net: -10, qa_wow: -0.032 },
        { owner_dept: 'hr', docs: 192, new_month: 0, qa_hits: 372, no_answer_rate: 0.124, pii_docs: 71, wow_net: 0, wow_total: 0.0, qa_wow_net: 15, qa_wow: 0.042 },
        { owner_dept: 'it', docs: 36, new_month: 0, qa_hits: 384, no_answer_rate: 0.102, pii_docs: 8, wow_net: -2, wow_total: -0.053, qa_wow_net: 22, qa_wow: 0.061 },
        { owner_dept: 'marketing', docs: 178, new_month: 178, qa_hits: 186, no_answer_rate: 0.231, pii_docs: 29, wow_net: 19, wow_total: 0.12, qa_wow_net: 8, qa_wow: 0.045 },
        { owner_dept: 'rd', docs: 175, new_month: 175, qa_hits: 24, no_answer_rate: 0.0, pii_docs: 64, wow_net: 9, wow_total: 0.054, qa_wow_net: 3, qa_wow: 0.143 },
      ],
    }
    return
  }
  clearLoadError('governance')
  try { kbGovernance.value = await apiJson<KbGovernance>('/api/kb/governance', { auth: true }) } catch (e) { noteLoadError('governance', e) /* 兜底 */ }
}

// 版本历史（点击文档行「历史」）：拉 /api/kb/version-history（后端现成）。
async function openHistory(d: DocItem) {
  verHistory.value = { doc: d, versions: [], loading: true, error: '' }
  try {
    const r = await apiJson<{ versions: VersionItem[] }>(`/api/kb/version-history?doc_id=${encodeURIComponent(d.doc_id)}`, { auth: true })
    verHistory.value = { doc: d, versions: r.versions || [], loading: false, error: '' }
  } catch { verHistory.value = { doc: d, versions: [], loading: false, error: '版本历史加载失败' } }
}
function closeHistory() { verHistory.value = null }

function setQuery(v: string) {
  q.value = v
  if (qTimer) clearTimeout(qTimer)
  qTimer = setTimeout(() => void loadDocs(), 300)   // 防抖；搜索走服务端（可命中未加载文档）
}

async function loadApprovals(force = false) {
  const s = useSession()
  if (s.role !== 'kb_admin') { approvals.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    approvals.value = [
      { doc_id: 'P1', version_no: 2, title: '2026 客户验厂应答模板', original_filename: '验厂应答.docx', owner_dept: 'quality', permission_level: 'public', owner_name: '李娜', created_at: '2026-06-27' },
      { doc_id: 'P2', version_no: 1, title: '外销报价单（公开版）', original_filename: '报价单.xlsx', owner_dept: 'marketing', permission_level: 'public', owner_name: '王伟', created_at: '2026-06-26' },
    ]
    return
  }
  if (!force && freshEnough('approvals')) return   // App ready 刚预载过 → 跳过（#82）
  lastLoadedAt['approvals'] = Date.now()
  clearLoadError('approvals')
  try {
    const r = await apiJson<{ items: PendingItem[] }>('/api/kb/pending-approvals', { auth: true })
    approvals.value = r.items || []
  } catch (e) { lastLoadedAt['approvals'] = 0; approvals.value = []; noteLoadError('approvals', e) }
}

// ── 升版态 ──
function enterVersionMode(d: DocItem) {
  verCtx.value = { doc_id: d.doc_id, title: d.title, owner_dept: d.owner_dept, permission_level: d.permission_level, current_version_no: d.current_version_no }
  newTitle.value = ''; dupWarn.value = ''; contentDupMsg.value = ''; uploadErr.value = ''; uploadMsg.value = ''
  selectedFiles = []; selectedNames.value = []
}
function exitVersionMode() { verCtx.value = null }

/**
 * 升版深链落地（小程序「上传新版本」→ ?doc_id=&owner=&title=）：命中已加载文档则正常进升版态；
 * 列表外（>50 / 旧文档）则用 doc_id+owner+title 合成 verCtx，permission_level 留空交后端强制继承
 * （action=version 时后端忽略客户端 perm）。补回 parity-1/3 丢失的能力。
 */
function applyPendingVersion(p: { docId: string; owner: string; title: string }) {
  const doc = docs.value.find((d) => d.doc_id === p.docId)
  if (doc) { enterVersionMode(doc); return }
  verCtx.value = { doc_id: p.docId, title: p.title || p.docId, owner_dept: p.owner, permission_level: '', current_version_no: 0 }
  newTitle.value = ''; dupWarn.value = ''; contentDupMsg.value = ''; uploadErr.value = ''; uploadMsg.value = ''
  selectedFiles = []; selectedNames.value = []
}

// ── 选文件：预检 + 文件名级查重 ──
async function onFileSelected(list: FileList | null) {
  uploadErr.value = ''; uploadMsg.value = ''; contentDupMsg.value = ''; uploadQueue.value = []
  selectedFiles = list ? Array.from(list) : []
  if (verCtx.value) selectedFiles = selectedFiles.slice(0, 1)   // 升版仅 1 文件
  // 客户端扩展名预检（拖拽绕过 input accept）：剔除不支持的文件并提示，省一次「传完才被后端拒」的往返。
  const bad = unsupportedNames(selectedFiles)
  if (bad.length) {
    selectedFiles = selectedFiles.filter((f) => !bad.includes(f.name))
    uploadErr.value = `已忽略 ${bad.length} 个不支持的文件（${bad.join('、')}）。仅支持 PDF / DOCX / XLSX / PPTX / JPG / PNG。`
  }
  selectedNames.value = selectedFiles.map((f) => f.name)
  dupWarn.value = ''
  if (!verCtx.value && selectedFiles.length === 1) {
    const core = fileCore(selectedFiles[0].name)
    if (core.length >= 2) {
      try {
        const r = await apiJson<MyDocsResp>(`/api/kb/my-docs?limit=10&q=${encodeURIComponent(core)}`, { auth: true })
        const hit = (r.items || []).find((d) => d.status_badge !== '已退役')
        if (hit) dupWarn.value = `已有相似文档《${hit.title || hit.original_filename || hit.doc_id}》v${hit.current_version_no}（${hit.status_badge}）。如是同一文档，建议改为「升版」。`
      } catch { /* 软提示，失败忽略 */ }
    }
  }
}

function trackStatus(docId: string, versionNo: number) {
  const mySeq = ++trackSeq
  if (trackTimer) clearTimeout(trackTimer)
  let tries = 0
  let fails = 0                                     // 连续轮询失败计：区分「处理慢」与「状态检查接口出错」
  const MAX = 22
  const poll = async () => {
    if (mySeq !== trackSeq) return                 // 被新上传/操作作废
    tries++
    try {
      const s = await apiJson<DocStatusResp>(`/api/kb/doc-status?doc_id=${encodeURIComponent(docId)}&version=${versionNo}`, { auth: true })
      if (mySeq !== trackSeq) return               // await 期间被作废
      fails = 0                                     // 成功 → 清失败计
      patchRow(docId, s.status_badge)
      if (TERMINAL_BADGES.includes(s.status_badge)) {
        if (s.status_badge === '处理失败') { uploadOk.value = false; uploadErr.value = `入库失败：${s.error_message || ''}（${docId} v${versionNo}）`; uploadMsg.value = '' }
        else if (s.status_badge === '已上线') uploadMsg.value = `已上线（${s.chunk_active} 段）`
        void loadDocs()
        return
      }
    } catch {
      // 轮询本身失败：累计，连续多次才提示（偶发抖动不打扰），仍重试到上限。
      if (++fails >= 3) uploadMsg.value = '状态检查暂时失败，仍在重试…若持续请手动刷新「我的文档」'
    }
    if (tries >= MAX) { uploadMsg.value = '仍在处理…耗时较长，稍后刷新「我的文档」查看'; return }
    trackTimer = setTimeout(poll, 8000)
  }
  trackTimer = setTimeout(poll, 4000)              // 首查延 4s，给 scanner 认领时间
}

async function uploadSingle(file: File) {
  uploadErr.value = ''; uploadMsg.value = ''; uploadOk.value = false; contentDupMsg.value = ''
  if (file.size <= 0) { uploadErr.value = '所选文件为空。'; return }
  if (file.size > maxUploadBytes.value) { uploadErr.value = `文件 ${(file.size / 1048576).toFixed(1)}MB，超过上限 ${maxUploadMb.value}MB，请压缩或拆分。`; return }
  trackSeq++                                        // 作废上一轮轮询
  uploadBusy.value = true
  try {
    const isVer = !!verCtx.value
    // 「指定部门」模式：登记仍是 dept_internal（可见度基线），随后经主动共享端点放行所选部门。
    const shared = !isVer && newPerm.value === 'shared'
    const body = isVer
      ? { action: 'version', doc_id: verCtx.value!.doc_id, owner_dept: verCtx.value!.owner_dept, permission_level: verCtx.value!.permission_level, filename: file.name, title: newTitle.value || undefined }
      : { action: 'new', filename: file.name, owner_dept: newOwner.value, permission_level: shared ? 'dept_internal' : newPerm.value, title: newTitle.value || undefined }
    uploadMsg.value = '申请上传地址…'
    const u = await apiJson<UploadUrlResp>('/api/kb/upload-url', { method: 'POST', auth: true, body: JSON.stringify(body) })
    uploadMsg.value = '上传文件到 OSS… 0%'
    await putWithProgress(u.put_url, file, (pct) => { uploadMsg.value = `上传文件到 OSS… ${pct}%` }, u.content_type)
    uploadMsg.value = '登记…'
    const r = await apiJson<RegisterResp>('/api/kb/register', { method: 'POST', auth: true, body: JSON.stringify({ upload_token: u.upload_token }) })
    uploadOk.value = true
    let shareNote = ''
    if (shared && newShareDepts.value.length) {
      // 共享失败不判上传失败：文档已在库，提示可去台账「共享」补做。文案以响应 granted/skipped 为准。
      try { shareNote = shareResultNote(await createGrants(r.doc_id, [...newShareDepts.value])) }
      catch { shareNote = '；共享设置失败，可稍后在台账点「共享」重试' }
    }
    uploadMsg.value = `已提交：${r.title || file.name} v${r.version_no}（${r.status_badge}${r.requires_kb_admin_approval ? '，待审批' : ''}）${shareNote}`
    contentDupMsg.value = buildDupMsg(r.content_dups, r.content_dups_other)
    newTitle.value = ''; dupWarn.value = ''; selectedFiles = []; selectedNames.value = []
    if (isVer) exitVersionMode()
    void loadDocs(); void loadApprovals(true)   // force：刚上传可能新增待审批单，穿透 staleness 门
    if (!r.requires_kb_admin_approval) trackStatus(r.doc_id, r.version_no)   // 待审批不轮询
  } catch (e: any) { uploadErr.value = uploadErrText(e); uploadMsg.value = '' } finally { uploadBusy.value = false }
}

// 批量上传并发度（E#35）。跨文件小并发池：每文件内部 upload-url → OSS PUT → register 三步顺序不变，
// 文件之间并发。register 侧已按 raw_key 幂等 + 行锁防撞号（批量恒为 action=new、各文件独立 doc_id/
// raw_key），无跨文件顺序依赖。取 2 而非 3：upload-url/register 都计入后端 aux 限流（默认 30/分，
// 每文件吃 2 次），并发越高小文件批越易撞 429——撞上也只影响该行（失败隔离），但保守起见压低突发。
const BATCH_CONCURRENCY = 2

async function uploadBatch(files: File[]) {
  uploadErr.value = ''; uploadOk.value = false; contentDupMsg.value = ''; uploadMsg.value = ''
  trackSeq++
  const rows: QueueRow[] = files.map((f) => ({ name: f.name, status: '排队', pct: 0, msg: '' }))
  uploadQueue.value = rows
  uploadBusy.value = true
  let okN = 0, badN = 0
  // 单文件全流程（三步顺序不变）；异常只落到本行——与原串行版一致的失败隔离，其余文件照常。
  const shared = newPerm.value === 'shared'   // 「指定部门」模式对批量同样生效（每文件登记后放行同一组目标）
  const uploadOne = async (i: number) => {
    const f = files[i], row = rows[i]
    if (f.size <= 0 || f.size > maxUploadBytes.value) { row.status = '跳过'; row.msg = f.size <= 0 ? '空文件' : `超过 ${maxUploadMb.value}MB`; badN++; return }
    try {
      row.status = '上传中'
      const u = await apiJson<UploadUrlResp>('/api/kb/upload-url', { method: 'POST', auth: true, body: JSON.stringify({ action: 'new', filename: f.name, owner_dept: newOwner.value, permission_level: shared ? 'dept_internal' : newPerm.value }) })
      await putWithProgress(u.put_url, f, (pct) => { row.pct = pct; row.msg = `${pct}%` }, u.content_type)
      row.status = '登记中'; row.msg = ''
      const r = await apiJson<RegisterResp>('/api/kb/register', { method: 'POST', auth: true, body: JSON.stringify({ upload_token: u.upload_token }) })
      row.status = '已提交'; row.msg = `v${r.version_no}（${r.status_badge}）`
      if (shared && newShareDepts.value.length) {
        // refresh:false：并发 worker 逐文件刷清单会互相覆盖+烧 aux 限流，批末统一拉一次
        try { row.msg += shareResultNote(await createGrants(r.doc_id, [...newShareDepts.value], '', { refresh: false })).replace(/^，/, ' · ') }
        catch { row.msg += ' · 共享失败可稍后重试' }
      }
      const dm = buildDupMsg(r.content_dups, r.content_dups_other); if (dm) row.dupMsg = dm
      okN++
    } catch (e: any) { row.status = '失败'; row.msg = uploadErrText(e); badN++ }
  }
  // 小并发池：worker 共享游标领任务，按队列顺序开工（uploadOne 自吞异常，Promise.all 不会中途 reject）。
  let cursor = 0
  const worker = async () => { for (;;) { const i = cursor++; if (i >= files.length) return; await uploadOne(i) } }
  await Promise.all(Array.from({ length: Math.min(BATCH_CONCURRENCY, files.length) }, () => worker()))
  uploadBusy.value = false
  uploadMsg.value = `${okN} 成功${badN ? `，${badN} 失败/跳过` : ''}`
  void loadDocs(); void loadApprovals(true)   // force：批量里可能有待审批单
  if (shared && okN) void loadAccessGrants()  // 批末一次权威刷新（逐文件已抑制）
}

function doUpload() {
  if (uploadBusy.value) return
  if (!selectedFiles.length) { uploadErr.value = '请先选择文件。'; return }
  if (verCtx.value || selectedFiles.length === 1) void uploadSingle(selectedFiles[0])
  else void uploadBatch(selectedFiles)
}

// 审批后定向更新（#82）：成功即本地移除该单（reviewCount 红点由 approvals.length 派生，随之同步），
// 免一次全量审批队列重拉；文档列表真受影响（放行/驳回改变该文档徽章）→ 保留一次权威 loadDocs。
// 失败路径保持现状：alert 提示、该单留在队列可重试。
function removeApproval(d: PendingItem) {
  approvals.value = approvals.value.filter((x) => !(x.doc_id === d.doc_id && x.version_no === d.version_no))
}

async function approve(d: PendingItem) {
  await withInflight(`appr:${d.doc_id}/${d.version_no}`, async () => {
    try {
      await apiJson('/api/kb/approve', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id, version_no: d.version_no }) })
      removeApproval(d)
      await loadDocs()
    } catch (e: any) { alert('通过失败：' + uploadErrText(e)) }
  })
}

async function reject(d: PendingItem, reason: string) {
  await withInflight(`appr:${d.doc_id}/${d.version_no}`, async () => {
    try {
      await apiJson('/api/kb/reject', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id, version_no: d.version_no, reason }) })
      removeApproval(d)
      await loadDocs()
    } catch (e: any) { alert('驳回失败：' + uploadErrText(e)) }
  })
}

// ── 授权申请（Phase C，审批人侧）──
// 数据源 /api/kb/access-requests 尚未上线 → 静默兜底空（不报错、不打扰）。DEV ?preview 注入 mock 可视化。
async function loadAccessRequests(force = false) {
  const s = useSession()
  if (!s.identity?.canManage) { accessRequests.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    accessRequests.value = [
      { id: 'ar1', doc_id: 'D1', doc_title: '营销物料使用规范 v3', owner_dept: 'marketing', requester_dept: 'production', requester_name: '王伟', permission_level: 'dept_internal', reason: '生产部包装设计需引用营销规范，确保对外物料一致。', created_at: '2026-06-26' },
      { id: 'ar2', doc_id: 'D2', doc_title: '客户投诉处理 SOP', owner_dept: 'marketing', requester_dept: 'quality', requester_name: '李娜', permission_level: 'dept_internal', reason: '品质部需对照投诉闭环流程。', created_at: '2026-06-25' },
    ]
    return
  }
  if (!force && freshEnough('accessRequests')) return   // App ready 刚预载过 → 跳过（#82）
  lastLoadedAt['accessRequests'] = Date.now()
  clearLoadError('accessRequests')
  try {
    const r = await apiJson<{ items: AccessRequestItem[] }>('/api/kb/access-requests', { auth: true })
    accessRequests.value = r.items || []
  } catch (e) { lastLoadedAt['accessRequests'] = 0; accessRequests.value = []; noteLoadError('accessRequests', e) }   // 404（未上线）静默；5xx 显错
}

async function approveAccess(d: AccessRequestItem) {
  await withInflight(`acc:${d.id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { accessRequests.value = accessRequests.value.filter((x) => x.id !== d.id); return }
      await apiJson('/api/kb/access-requests/approve', { method: 'POST', auth: true, body: JSON.stringify({ id: d.id }) })
      // 定向更新（#82）：本地移除该单（与 preview 分支同构）；新授权落到「已授权清单」→ 只刷真正受影响的列表。
      accessRequests.value = accessRequests.value.filter((x) => x.id !== d.id)
      void loadAccessGrants()
    } catch (e: any) { alert('授权失败：' + uploadErrText(e)) }
  })
}

async function rejectAccess(d: AccessRequestItem, reason: string) {
  await withInflight(`acc:${d.id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { accessRequests.value = accessRequests.value.filter((x) => x.id !== d.id); return }
      await apiJson('/api/kb/access-requests/reject', { method: 'POST', auth: true, body: JSON.stringify({ id: d.id, reason }) })
      accessRequests.value = accessRequests.value.filter((x) => x.id !== d.id)   // 定向更新（#82）：驳回只影响本队列
    } catch (e: any) { alert('驳回失败：' + uploadErrText(e)) }
  })
}

// 已授权清单（审批人侧 · approved 存量）：后端 /api/kb/access-grants 未上线 → 静默兜底空；DEV ?preview 注入 mock。
// grantsSeq 竞态守卫（同 docsSeq）：批量上传的并发 worker 各自触发刷新时，仅最新一次落地，防旧响应覆盖新清单。
let grantsSeq = 0
async function loadAccessGrants() {
  const seq = ++grantsSeq
  const s = useSession()
  if (!s.identity?.canManage) { accessGrants.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    accessGrants.value = [
      { id: 'ag1', doc_id: 'D1', doc_title: '营销物料使用规范 v3', owner_dept: 'marketing', requester_dept: 'production', requester_name: '王伟', permission_level: 'dept_internal', reason: '生产部包装设计需引用营销规范。', decided_at: '2026-06-26' },
      { id: 'ag2', doc_id: 'D2', doc_title: '客户投诉处理 SOP', owner_dept: 'marketing', requester_dept: 'quality', requester_name: '李娜', permission_level: 'dept_internal', reason: '品质部对照投诉闭环流程。', decided_at: '2026-06-25' },
    ]
    return
  }
  clearLoadError('accessGrants')
  try {
    const r = await apiJson<{ items: AccessGrantItem[] }>('/api/kb/access-grants', { auth: true })
    if (seq !== grantsSeq) return          // 竞态守卫：仅最新结果落地
    accessGrants.value = r.items || []
  } catch (e) { if (seq === grantsSeq) { accessGrants.value = []; noteLoadError('accessGrants', e) } }   // 404（未上线）静默；5xx 显错
}

// 撤销【已批准】的跨部门授权（approved→revoked）：后端同事务收窄 allowed_depts 投影 + 标脏，stage-3 收回放行。
async function revokeAccess(g: AccessGrantItem, reason: string) {
  await withInflight(`grant:${g.id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { accessGrants.value = accessGrants.value.filter((x) => x.id !== g.id); return }
      await apiJson('/api/kb/access-requests/revoke', { method: 'POST', auth: true, body: JSON.stringify({ id: g.id, reason }) })
      await loadAccessGrants()
    } catch (e: any) { alert('撤销失败：' + uploadErrText(e)) }
  })
}

// ── 主动共享（owner 侧，多部门可见度）───────────────────────────────────────
// 被动申请流（submit→approve）的主动式对偶：文档所属部门管理员直接放行指定部门，
// 后端直插 approved 行并复用 Phase D 投影；撤销/清单/审批历史与被动流同一套。
interface GrantCreateResp { doc_id: string; granted: string[]; skipped: string[]; ok: boolean }

// 差评联动复核（与后端 KbFeedbackReviewItem 对齐；question 已服务端 PII 脱敏）。
export interface FeedbackDocRef { doc_id: string; title: string; owner_dept: string }
export interface FeedbackReviewItem { message_id: string; question: string; created_at: string; docs: FeedbackDocRef[] }

/** 拉差评复核队列（看板卡片）：失败静默兜底空 + loadErrors 显错可重试。 */
async function loadFeedbackReview() {
  const s = useSession()
  if (!s.identity?.canManage) { feedbackReview.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    feedbackReview.value = [
      { message_id: 'fb1', question: '2oz 纸杯的收缩率标准是多少？', created_at: '2026-07-03 14:20',
        docs: [{ doc_id: 'D1', title: '纸杯工艺参数表', owner_dept: 'production' }] },
      { message_id: 'fb2', question: '出口美国的 FDA 检测周期？', created_at: '2026-07-02 09:11',
        docs: [{ doc_id: 'D2', title: '出口检测流程 SOP', owner_dept: 'marketing' }, { doc_id: 'D3', title: '认证台账', owner_dept: 'marketing' }] },
    ]
    return
  }
  clearLoadError('feedbackReview')
  try {
    const r = await apiJson<{ items: FeedbackReviewItem[] }>('/api/kb/feedback-review', { auth: true })
    feedbackReview.value = r.items || []
  } catch (e) { feedbackReview.value = feedbackReview.value ?? []; noteLoadError('feedbackReview', e) }
}

// 「谁能看到这篇文档」解释器响应（与后端 KbVisibilityExplainResponse 对齐；判定与检索同源）。
export interface VisReader { dept: string; via: 'owner' | 'umbrella' | 'shared_policy' | 'grant' }
export interface VisExplain {
  doc_id: string; owner_dept: string; permission_level: string
  everyone: boolean; nobody: boolean; quarantined: boolean; active: boolean
  readers: VisReader[]
}

/** 打开「谁能看到」弹窗并拉取解释（只读；失败在弹窗内显示错误）。 */
async function openVisibility(d: DocItem): Promise<void> {
  visCtx.value = d
  visExplain.value = null
  visErr.value = ''
  visLoading.value = true
  try {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') {
      visExplain.value = {
        doc_id: d.doc_id, owner_dept: d.owner_dept, permission_level: d.permission_level,
        everyone: d.permission_level === 'public', nobody: d.permission_level === 'restricted',
        quarantined: false, active: true,
        readers: d.permission_level === 'dept_internal'
          ? [{ dept: d.owner_dept, via: 'owner' }, { dept: 'hr', via: 'grant' }] : [],
      }
      return
    }
    visExplain.value = await apiJson<VisExplain>(
      `/api/kb/visibility-explain?doc_id=${encodeURIComponent(d.doc_id)}`, { auth: true })
  } catch (e: any) {
    visErr.value = e && e.status === 403 ? (e.detail || '无权查看该文档的可见范围明细') : uploadErrText(e)
  } finally { visLoading.value = false }
}
function closeVisibility() { visCtx.value = null; visExplain.value = null; visErr.value = '' }

/** 该文档现行已放行的组码集合（含被动流 CSV 行拆分）——驱动弹窗「已共享」与台账副行计数。 */
function grantedDeptsOf(docId: string): string[] {
  const out = new Set<string>()
  for (const g of accessGrants.value) {
    if (g.doc_id !== docId) continue
    for (const p of String(g.requester_dept || '').split(',')) { const c = p.trim(); if (c) out.add(c) }
  }
  return [...out].sort()
}

/** 该文档的授权行（供弹窗逐行撤销）。 */
function docGrantRows(docId: string): AccessGrantItem[] {
  return accessGrants.value.filter((g) => g.doc_id === docId)
}

function openShare(d: DocItem) { shareCtx.value = d }
function closeShare() { if (!shareBusy.value) shareCtx.value = null }

/** 直接放行：POST /api/kb/access-grants；成功后刷新已授权清单（弹窗/台账随之更新）。
 * opts.refresh=false 供批量路径抑制逐次刷新（批末统一拉一次，防 N 并发 GET 互相覆盖+烧限流）。 */
async function createGrants(docId: string, depts: string[], reason = '',
                            opts: { refresh?: boolean } = {}): Promise<GrantCreateResp | null> {
  if (!depts.length) return null
  const r = await apiJson<GrantCreateResp>('/api/kb/access-grants', {
    method: 'POST', auth: true,
    body: JSON.stringify({ doc_id: docId, target_depts: depts, ...(reason ? { reason } : {}) }),
  })
  if (opts.refresh !== false) void loadAccessGrants()
  return r
}

/** 共享结果如实文案：以后端 granted/skipped 为准——skipped（归属自身/伞下冗余/已覆盖）
 * 没有写任何授权行，不能再按请求数报「已共享 N 部门」。 */
function shareResultNote(gr: GrantCreateResp | null): string {
  if (!gr) return ''
  const g = gr.granted?.length || 0, s = gr.skipped?.length || 0
  if (g && s) return `，已共享 ${g} 部门（${s} 个已覆盖跳过）`
  if (g) return `，已共享 ${g} 部门`
  if (s) return `，${s} 个目标已覆盖无需新授权`
  return ''
}

/** 弹窗提交：共享 shareCtx 文档给所选部门。返回 null=成功关闭；string=错误/提示文案（弹窗内联显示）。
 * 全部目标被后端跳过（伞组/共享面本就可读）→ 不关弹窗、如实提示——不能让用户以为写了授权。 */
async function submitShare(depts: string[], reason: string): Promise<string | null> {
  const d = shareCtx.value
  if (!d || !depts.length) return '请选择要共享的部门'
  shareBusy.value = true
  try {
    const r = await createGrants(d.doc_id, depts, reason)
    if (r && !r.granted?.length && r.skipped?.length) {
      return `所选部门本就可读该文档（生产伞组/营销共享面覆盖），无需新增授权：${r.skipped.map(deptLabel).join('、')}`
    }
    shareCtx.value = null
    return null
  } catch (e: any) {
    return e && e.status === 403 ? (e.detail || '无权共享该文档') : uploadErrText(e)
  } finally { shareBusy.value = false }
}

/** 重设基础可见范围（dept_internal / public / restricted）：POST /api/kb/set-visibility。
 * 成功后即时反映行的 permission_level + 权威 loadDocs 纠正徽章（restricted 会离开检索）。
 * opts.reload=false 供批量路径抑制逐篇重拉（_bulkRun 批末已有一次权威 loadDocs——否则
 * N 篇 = N+1 次全量列表拉取，烧 aux 限流且批中列表反复重置分页/闪动）。
 * 返回 null=成功；string=错误文案。 */
async function setVisibility(d: DocItem, level: string, reason = '',
                             opts: { reload?: boolean } = {}): Promise<string | null> {
  return withInflight(`vis:${d.doc_id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { d.permission_level = level; return null }
      await apiJson('/api/kb/set-visibility', {
        method: 'POST', auth: true,
        body: JSON.stringify({ doc_id: d.doc_id, permission_level: level, ...(reason ? { reason } : {}) }),
      })
      d.permission_level = level                     // 乐观即时反映
      if (opts.reload !== false) void loadDocs()     // 权威纠正（restricted→可能掉出列表/改徽章）
      return null
    } catch (e: any) {
      return e && e.status === 403 ? (e.detail || '无权修改该文档可见范围')
        : e && e.status === 409 ? (e.detail || '该文档非在线状态') : uploadErrText(e)
    }
  }) as Promise<string | null>
}

// 审批历史（只读聚合）：后端按角色作用域 —— dept_admin 见本部门 access+contribution、kb_admin 见全库四类。
// 404（未上线）静默兜底空；5xx 显错。DEV ?preview 注入 mock（kb_admin 见四类混合、dept_admin 仅两类）。
async function loadApprovalHistory() {
  const s = useSession()
  if (!s.identity?.canManage) { approvalHistory.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    const base: ApprovalHistoryItem[] = [
      { kind: 'access', action: 'approved', title: '客户投诉处理 SOP', owner_dept: 'marketing', subject: '王伟', detail: '生产部包装设计需引用营销规范。', extra: '', decided_by: 'kb001', decided_by_name: '系统管理员', decided_at: '2026-06-28 14:32:10' },
      { kind: 'access', action: 'rejected', title: '海外客户名录 v2', owner_dept: 'marketing', subject: '赵强', detail: '涉客户隐私，暂不外放。', extra: '', decided_by: 'mgr001', decided_by_name: '王伟', decided_at: '2026-06-27 10:05:00' },
      { kind: 'contribution', action: 'accepted', title: '2ozpp杯在龙盛机上的速度是多少？', owner_dept: 'production', subject: '孙工', detail: '', extra: 'searchable', decided_by: 'mgr002', decided_by_name: '李娜', decided_at: '2026-06-27 09:15:22' },
      { kind: 'contribution', action: 'rejected', title: '请假流程能不能加急', owner_dept: 'hr', subject: '周敏', detail: '与现行制度冲突，未采纳。', extra: '', decided_by: 'mgr003', decided_by_name: '陈立', decided_at: '2026-06-26 16:40:00' },
    ]
    const adminOnly: ApprovalHistoryItem[] = [
      { kind: 'upload', action: 'approved', title: '注塑车间安全操作规程 v4.docx', owner_dept: 'production', subject: '', detail: '', extra: '', decided_by: 'kb001', decided_by_name: '系统管理员', decided_at: '2026-06-28 11:20:00' },
      { kind: 'upload', action: 'rejected', title: '旧版包装规范.pdf', owner_dept: 'marketing', subject: '', detail: '内容过期，已被 v3 取代。', extra: '', decided_by: 'kb001', decided_by_name: '系统管理员', decided_at: '2026-06-26 17:40:00' },
      { kind: 'admin_grant', action: 'granted', title: 'mgr002', owner_dept: '', subject: 'mgr002', detail: 'grant dept_admin mgr002 → quality,production', extra: '', decided_by: 'kb001', decided_by_name: '系统管理员', decided_at: '2026-06-25 09:00:00' },
    ]
    approvalHistory.value = s.role === 'kb_admin'
      ? [...base, ...adminOnly].sort((a, b) => (a.decided_at < b.decided_at ? 1 : -1))
      : base
    return
  }
  clearLoadError('approvalHistory')
  try {
    const r = await apiJson<{ items: ApprovalHistoryItem[] }>('/api/kb/approval-history', { auth: true })
    approvalHistory.value = r.items || []
  } catch (e) { approvalHistory.value = []; noteLoadError('approvalHistory', e) }
}

// ── Phase F：成员/角色管理（仅 kb_admin）──
async function loadAdminGrants() {
  const s = useSession()
  if (s.role !== 'kb_admin') { adminGrants.value = []; grantableDepts.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    adminGrants.value = [
      { user_id: 'mgr001', user_name: '王伟', role: 'dept_admin', managed_owner_depts: ['marketing'] },
      { user_id: 'mgr002', user_name: '李娜', role: 'dept_admin', managed_owner_depts: ['quality', 'production'] },
      { user_id: 'kb001', user_name: '系统管理员', role: 'kb_admin', managed_owner_depts: [] },
    ]
    grantableDepts.value = ['marketing', 'production', 'quality', 'finance', 'hr', 'supply', 'pmc', 'rd', 'admin', 'it']
    return
  }
  clearLoadError('adminGrants')
  try {
    const r = await apiJson<{ items: AdminItem[]; grantable_owner_depts: string[] }>('/api/kb/admin-grants', { auth: true })
    adminGrants.value = r.items || []
    grantableDepts.value = r.grantable_owner_depts || []
  } catch (e) { adminGrants.value = []; grantableDepts.value = []; noteLoadError('adminGrants', e) }   // 404 静默；5xx 显错
}

// 授予/更新一名部门管理员（owner_depts = 权威全集,提交即覆盖）。成功返回 true。
async function grantDeptAdmin(userId: string, userName: string, ownerDepts: string[], note: string): Promise<boolean> {
  return (await withInflight(`member:${userId}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') {
        const i = adminGrants.value.findIndex((a) => a.user_id === userId)
        const row: AdminItem = { user_id: userId, user_name: userName, role: 'dept_admin', managed_owner_depts: [...ownerDepts] }
        adminGrants.value = i >= 0 ? adminGrants.value.map((a, k) => (k === i ? row : a)) : [...adminGrants.value, row]
        return true
      }
      await apiJson('/api/kb/admin-grants', { method: 'POST', auth: true, body: JSON.stringify({ user_id: userId, user_name: userName, owner_depts: ownerDepts, note }) })
      await loadAdminGrants()
      return true
    } catch (e: any) { alert('授予失败：' + uploadErrText(e)); return false }
  })) ?? false   // 在途（重复点击）→ 视为未提交
}

// 撤销：ownerDept 指定→撤该一项；为空→撤全部并降级 employee。
async function revokeAdminGrant(userId: string, ownerDept = ''): Promise<void> {
  await withInflight(`member:${userId}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') {
        adminGrants.value = adminGrants.value
          .map((a) => (a.user_id === userId ? { ...a, managed_owner_depts: ownerDept ? a.managed_owner_depts.filter((d) => d !== ownerDept) : [] } : a))
          .filter((a) => a.role === 'kb_admin' || a.managed_owner_depts.length > 0)   // 无授权剩余 → 视为降级移出
        return
      }
      await apiJson('/api/kb/admin-grants/revoke', { method: 'POST', auth: true, body: JSON.stringify({ user_id: userId, owner_dept: ownerDept }) })
      await loadAdminGrants()
    } catch (e: any) { alert('撤销失败：' + uploadErrText(e)) }
  })
}

async function retire(d: DocItem): Promise<{ ok: boolean; msg?: string }> {
  if (retireBusy.value) return { ok: false }
  retireBusy.value = true
  try {
    const r = await apiJson<RetireResp>('/api/kb/retire', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id }) })
    d.status_badge = '已退役'                       // 即时反映行
    await loadDocs()                                // await 重拉，让服务端权威徽章纠正乐观值（对齐 approve/reject；G3）
    return { ok: true, msg: r.note }
  } catch (e: any) {
    const msg = e && e.status === 403 ? (e.detail || '无权退役该文档') : uploadErrText(e)
    return { ok: false, msg }
  } finally { retireBusy.value = false }
}

// 恢复上线（退役逆操作）：重新激活 + 标脏待重索引；HA3 未删则即时可检索，否则下次维护重索引后恢复。
async function restore(d: DocItem): Promise<{ ok: boolean; msg?: string }> {
  if (retireBusy.value) return { ok: false }
  retireBusy.value = true
  try {
    if (import.meta.env.DEV && useSession().token === 'dev-preview') { d.status_badge = '排队中'; return { ok: true } }
    const r = await apiJson<{ note?: string }>('/api/kb/restore', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id }) })
    d.status_badge = '排队中'                       // 即时反映（NOT_INDEXED → 待重索引）
    await loadDocs()                                // await 重拉，把乐观「排队中」纠正为服务端权威徽章（HA3 未删则即时「已上线」；G3）
    return { ok: true, msg: r.note }
  } catch (e: any) {
    const msg = e && e.status === 403 ? (e.detail || '无权恢复该文档') : uploadErrText(e)
    return { ok: false, msg }
  } finally { retireBusy.value = false }
}

export function useKb() {
  const session = useSession()
  const ownerDepts = computed(() => session.identity?.managedOwnerDepts ?? [])
  const isKbAdmin = computed(() => session.role === 'kb_admin')
  const isDeptAdmin = computed(() => session.role === 'dept_admin')
  // 待你审核的数量（红点/角标单一来源）：kb_admin = 待审批上传 + 授权申请；dept_admin = 授权申请（其本部门文档的）。
  // 上传审批仅 kb_admin（/pending-approvals kb-only），故 dept_admin 的 approvals 恒空、不计入。
  const reviewCount = computed(() => (session.role === 'kb_admin' ? approvals.value.length : 0) + accessRequests.value.length)

  return {
    // 状态
    docs, filtered, approvals, accessRequests, accessGrants, approvalHistory, adminGrants, grantableDepts, loadingDocs, loadingMoreDocs, hasMoreDocs, docScope, q, filter, permFilter, ownerFilter, citedFilter, ownerOptions, sortKey, sortDir,
    // 多选 / 批量
    selectableVisible, selectedDocs, selectedCount, allVisibleSelected, isSelected, toggleSelect, toggleSelectAllVisible, clearSelection, bulkBusy, bulkMsg, bulkRetire, bulkSetVisibility,
    newTitle, newOwner, newPerm, newShareDepts, verCtx, uploadBusy, uploadMsg, uploadErr, uploadOk,
    dupWarn, contentDupMsg, uploadQueue, selectedNames, isBusy, retireBusy,
    accessReqDoc, accessReqBusy, requestedDocIds, myAccessReqs,
    shareCtx, shareBusy, shareTargets: SHARE_TARGETS,
    ownerDepts, isKbAdmin, isDeptAdmin, reviewCount, kbStats, kbConfig, kbInsights, kbGovernance, maxUploadMb, verHistory, loadErrors,
    // 方法
    loadDocs, loadMoreDocs, loadStats, loadConfig, loadInsights, loadGovernance, openHistory, closeHistory, setQuery, loadApprovals, sortBy, countOf,
    loadAccessRequests, approveAccess, rejectAccess, loadAccessGrants, revokeAccess, loadApprovalHistory, setScope,
    openShare, closeShare, submitShare, grantedDeptsOf, docGrantRows, setVisibility,
    visCtx, visExplain, visLoading, visErr, openVisibility, closeVisibility,
    feedbackReview, loadFeedbackReview,
    loadAdminGrants, grantDeptAdmin, revokeAdminGrant,
    openAccessRequest, closeAccessRequest, submitAccessRequest, accessStateOf, loadMyAccessRequests,
    enterVersionMode, exitVersionMode, applyPendingVersion, onFileSelected, doUpload,
    approve, reject, retire, restore,
  }
}

/** 仅供测试：重置 store。 */
export function __resetKb() {
  docs.value = []; kbStats.value = null; kbInsights.value = null; kbGovernance.value = null; kbConfig.value = null; verHistory.value = null; approvals.value = []; accessRequests.value = []; accessGrants.value = []; approvalHistory.value = []; adminGrants.value = []; grantableDepts.value = []; loadingDocs.value = false; loadingMoreDocs.value = false; hasMoreDocs.value = false; loadErrors.value = {}
  docScope.value = 'managed'; accessReqDoc.value = null; accessReqBusy.value = false; requestedDocIds.value = new Set(); myAccessReqs.value = new Map()
  q.value = ''; filter.value = ''; permFilter.value = ''; ownerFilter.value = ''; citedFilter.value = ''; sortKey.value = 'updated_at'; sortDir.value = -1
  selectedIds.value = new Set(); bulkBusy.value = false; bulkMsg.value = ''
  newTitle.value = ''; newOwner.value = ''; newPerm.value = 'dept_internal'; newShareDepts.value = []; verCtx.value = null
  shareCtx.value = null; shareBusy.value = false
  uploadBusy.value = false; uploadMsg.value = ''; uploadErr.value = ''; uploadOk.value = false
  dupWarn.value = ''; contentDupMsg.value = ''; uploadQueue.value = []; selectedNames.value = []
  inflight.value = new Set(); retireBusy.value = false
  selectedFiles = []; docsOffset = 0; docsSeq = 0; trackSeq = 0
  for (const k of Object.keys(lastLoadedAt)) delete lastLoadedAt[k]   // 重开 staleness 门（#82）
  if (qTimer) { clearTimeout(qTimer); qTimer = null }
  if (trackTimer) { clearTimeout(trackTimer); trackTimer = null }
}

/** 仅供测试：注入选中文件（绕过 input）。 */
export function __setSelectedFiles(files: File[]) { selectedFiles = files }
