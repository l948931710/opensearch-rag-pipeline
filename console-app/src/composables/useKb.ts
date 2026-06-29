import { computed, ref } from 'vue'
import { apiJson } from '@/lib/api'
import { useSession } from '@/stores/session'
import {
  MAX_UPLOAD_MB, TERMINAL_BADGES, putWithProgress, uploadErrText, buildDupMsg, fileCore, type DupDoc,
} from '@/lib/kb'

// 知识库管理台单例 store。身份/可管部门复用 P1 的 session（whoami 已给 managed_owner_depts），
// 不再走旧 console 的 org-tree。所有写接口后端【现查】授权，前端 role 仅作 UI 门禁。

export interface DocItem {
  doc_id: string; title: string; original_filename: string; owner_dept: string
  permission_level: string; current_version_no: number; status: string
  status_badge: string; updated_at: string
  can_manage?: boolean   // 可操作（写作用域）；my-docs 恒 true，browse 全部门时外部门为 false
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
  questions: number; askers: number; success: number; refusal: number; cited: number; effective_rate: number
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
interface UploadUrlResp { upload_token: string; put_url: string; raw_key: string; doc_id: string; expires_in: number; requires_kb_admin_approval: boolean }
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
const sortKey = ref<SortKey>('updated_at')
const sortDir = ref<1 | -1>(-1)

// 上传表单 / 状态
const newTitle = ref('')
const newOwner = ref('')
const newPerm = ref('dept_internal')
const verCtx = ref<VerCtx | null>(null)
const uploadBusy = ref(false)
const uploadMsg = ref('')
const uploadErr = ref('')
const uploadOk = ref(false)
const dupWarn = ref('')                // 文件名级预查重
const contentDupMsg = ref('')          // ETag 内容级查重
const uploadQueue = ref<QueueRow[]>([])
const selectedNames = ref<string[]>([])
const apprBusy = ref(false)
const retireBusy = ref(false)

// File 真身【绝不进响应式】（Vue3 Proxy 会破坏 xhr.send(file)）——留模块闭包。
let selectedFiles: File[] = []
const DOCS_PAGE = 50                  // 台账翻页页大小（= 后端 limit 上限）
let docsOffset = 0                    // 当前已加载到的 offset（首屏 0；每翻一页 +DOCS_PAGE）
let docsSeq = 0
let trackSeq = 0
let qTimer: ReturnType<typeof setTimeout> | null = null
let trackTimer: ReturnType<typeof setTimeout> | null = null   // 当前轮询定时器句柄（可取消）

function sortDocs(list: DocItem[], key: SortKey, dir: 1 | -1): DocItem[] {
  return [...list].sort((a, b) => {
    let r: number
    if (key === 'current_version_no') r = (Number(a[key]) || 0) - (Number(b[key]) || 0)
    else r = String(a[key] ?? '').localeCompare(String(b[key] ?? ''))
    return r * dir
  })
}

const filtered = computed(() =>
  sortDocs(docs.value.filter((d) => !filter.value || d.status_badge === filter.value), sortKey.value, sortDir.value))

function countOf(badge: string): number {
  return badge ? docs.value.filter((d) => d.status_badge === badge).length : docs.value.length
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
  try {
    // DEV ?preview：注入 mock（含外部门 can_manage=false 行）以可视化全部门只读浏览；prod 死代码消除。
    if (import.meta.env.DEV && useSession().token === 'dev-preview') {
      const mine: DocItem[] = [
        { doc_id: 'm1', title: '营销物料使用规范 v3', original_filename: 'guideline.pdf', owner_dept: 'marketing', permission_level: 'dept_internal', current_version_no: 3, status: 'active', status_badge: '已上线', updated_at: '2026-06-26 10:00', can_manage: true },
        { doc_id: 'm2', title: '品牌 VI 手册', original_filename: 'vi.pdf', owner_dept: 'marketing', permission_level: 'public', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '2026-06-20 09:00', can_manage: true },
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
  } catch { /* 保留旧表 */ } finally { if (seq === docsSeq) loadingDocs.value = false }
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
  filter.value = ''
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
  try { kbStats.value = await apiJson<KbStats>('/api/kb/stats', { auth: true }) } catch { /* 兜底 */ }
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
      questions: 186, askers: 40, success: 143, refusal: 43, cited: 130, effective_rate: 0.769,
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
  try { kbInsights.value = await apiJson<KbInsights>('/api/kb/insights', { auth: true }) } catch { /* 兜底 */ }
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
  try { kbGovernance.value = await apiJson<KbGovernance>('/api/kb/governance', { auth: true }) } catch { /* 兜底 */ }
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

async function loadApprovals() {
  const s = useSession()
  if (s.role !== 'kb_admin') { approvals.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    approvals.value = [
      { doc_id: 'P1', version_no: 2, title: '2026 客户验厂应答模板', original_filename: '验厂应答.docx', owner_dept: 'quality', permission_level: 'public', owner_name: '李娜', created_at: '2026-06-27' },
      { doc_id: 'P2', version_no: 1, title: '外销报价单（公开版）', original_filename: '报价单.xlsx', owner_dept: 'marketing', permission_level: 'public', owner_name: '王伟', created_at: '2026-06-26' },
    ]
    return
  }
  try {
    const r = await apiJson<{ items: PendingItem[] }>('/api/kb/pending-approvals', { auth: true })
    approvals.value = r.items || []
  } catch { approvals.value = [] }
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
  const MAX = 22
  const poll = async () => {
    if (mySeq !== trackSeq) return                 // 被新上传/操作作废
    tries++
    try {
      const s = await apiJson<DocStatusResp>(`/api/kb/doc-status?doc_id=${encodeURIComponent(docId)}&version=${versionNo}`, { auth: true })
      if (mySeq !== trackSeq) return               // await 期间被作废
      patchRow(docId, s.status_badge)
      if (TERMINAL_BADGES.includes(s.status_badge)) {
        if (s.status_badge === '处理失败') { uploadOk.value = false; uploadErr.value = `入库失败：${s.error_message || ''}（${docId} v${versionNo}）`; uploadMsg.value = '' }
        else if (s.status_badge === '已上线') uploadMsg.value = `已上线（${s.chunk_active} 段）`
        void loadDocs()
        return
      }
    } catch { /* 轮询本身失败：重试到上限 */ }
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
    const body = isVer
      ? { action: 'version', doc_id: verCtx.value!.doc_id, owner_dept: verCtx.value!.owner_dept, permission_level: verCtx.value!.permission_level, filename: file.name, title: newTitle.value || undefined }
      : { action: 'new', filename: file.name, owner_dept: newOwner.value, permission_level: newPerm.value, title: newTitle.value || undefined }
    uploadMsg.value = '申请上传地址…'
    const u = await apiJson<UploadUrlResp>('/api/kb/upload-url', { method: 'POST', auth: true, body: JSON.stringify(body) })
    uploadMsg.value = '上传文件到 OSS… 0%'
    await putWithProgress(u.put_url, file, (pct) => { uploadMsg.value = `上传文件到 OSS… ${pct}%` })
    uploadMsg.value = '登记…'
    const r = await apiJson<RegisterResp>('/api/kb/register', { method: 'POST', auth: true, body: JSON.stringify({ upload_token: u.upload_token }) })
    uploadOk.value = true
    uploadMsg.value = `已提交：${r.title || file.name} v${r.version_no}（${r.status_badge}${r.requires_kb_admin_approval ? '，待审批' : ''}）`
    contentDupMsg.value = buildDupMsg(r.content_dups, r.content_dups_other)
    newTitle.value = ''; dupWarn.value = ''; selectedFiles = []; selectedNames.value = []
    if (isVer) exitVersionMode()
    void loadDocs(); void loadApprovals()
    if (!r.requires_kb_admin_approval) trackStatus(r.doc_id, r.version_no)   // 待审批不轮询
  } catch (e: any) { uploadErr.value = uploadErrText(e); uploadMsg.value = '' } finally { uploadBusy.value = false }
}

async function uploadBatch(files: File[]) {
  uploadErr.value = ''; uploadOk.value = false; contentDupMsg.value = ''; uploadMsg.value = ''
  trackSeq++
  const rows: QueueRow[] = files.map((f) => ({ name: f.name, status: '排队', pct: 0, msg: '' }))
  uploadQueue.value = rows
  uploadBusy.value = true
  let okN = 0, badN = 0
  for (let i = 0; i < files.length; i++) {
    const f = files[i], row = rows[i]
    if (f.size <= 0 || f.size > maxUploadBytes.value) { row.status = '跳过'; row.msg = f.size <= 0 ? '空文件' : `超过 ${maxUploadMb.value}MB`; badN++; continue }
    try {
      row.status = '上传中'
      const u = await apiJson<UploadUrlResp>('/api/kb/upload-url', { method: 'POST', auth: true, body: JSON.stringify({ action: 'new', filename: f.name, owner_dept: newOwner.value, permission_level: newPerm.value }) })
      await putWithProgress(u.put_url, f, (pct) => { row.pct = pct; row.msg = `${pct}%` })
      row.status = '登记中'; row.msg = ''
      const r = await apiJson<RegisterResp>('/api/kb/register', { method: 'POST', auth: true, body: JSON.stringify({ upload_token: u.upload_token }) })
      row.status = '已提交'; row.msg = `v${r.version_no}（${r.status_badge}）`
      const dm = buildDupMsg(r.content_dups, r.content_dups_other); if (dm) row.dupMsg = dm
      okN++
    } catch (e: any) { row.status = '失败'; row.msg = uploadErrText(e); badN++ }
  }
  uploadBusy.value = false
  uploadMsg.value = `${okN} 成功${badN ? `，${badN} 失败/跳过` : ''}`
  void loadDocs(); void loadApprovals()
}

function doUpload() {
  if (uploadBusy.value) return
  if (!selectedFiles.length) { uploadErr.value = '请先选择文件。'; return }
  if (verCtx.value || selectedFiles.length === 1) void uploadSingle(selectedFiles[0])
  else void uploadBatch(selectedFiles)
}

async function approve(d: PendingItem) {
  if (apprBusy.value) return
  apprBusy.value = true
  try {
    await apiJson('/api/kb/approve', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id, version_no: d.version_no }) })
    await loadApprovals(); await loadDocs()
  } catch (e: any) { alert('通过失败：' + uploadErrText(e)) } finally { apprBusy.value = false }
}

async function reject(d: PendingItem, reason: string) {
  if (apprBusy.value) return
  apprBusy.value = true
  try {
    await apiJson('/api/kb/reject', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id, version_no: d.version_no, reason }) })
    await loadApprovals(); await loadDocs()
  } catch (e: any) { alert('驳回失败：' + uploadErrText(e)) } finally { apprBusy.value = false }
}

// ── 授权申请（Phase C，审批人侧）──
// 数据源 /api/kb/access-requests 尚未上线 → 静默兜底空（不报错、不打扰）。DEV ?preview 注入 mock 可视化。
async function loadAccessRequests() {
  const s = useSession()
  if (!s.identity?.canManage) { accessRequests.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    accessRequests.value = [
      { id: 'ar1', doc_id: 'D1', doc_title: '营销物料使用规范 v3', owner_dept: 'marketing', requester_dept: 'production', requester_name: '王伟', permission_level: 'dept_internal', reason: '生产部包装设计需引用营销规范，确保对外物料一致。', created_at: '2026-06-26' },
      { id: 'ar2', doc_id: 'D2', doc_title: '客户投诉处理 SOP', owner_dept: 'marketing', requester_dept: 'quality', requester_name: '李娜', permission_level: 'dept_internal', reason: '品质部需对照投诉闭环流程。', created_at: '2026-06-25' },
    ]
    return
  }
  try {
    const r = await apiJson<{ items: AccessRequestItem[] }>('/api/kb/access-requests', { auth: true })
    accessRequests.value = r.items || []
  } catch { accessRequests.value = [] }   // 端点未上线/出错 → 静默空，不阻断
}

async function approveAccess(d: AccessRequestItem) {
  if (apprBusy.value) return
  apprBusy.value = true
  try {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') { accessRequests.value = accessRequests.value.filter((x) => x.id !== d.id); return }
    await apiJson('/api/kb/access-requests/approve', { method: 'POST', auth: true, body: JSON.stringify({ id: d.id }) })
    await loadAccessRequests()
  } catch (e: any) { alert('授权失败：' + uploadErrText(e)) } finally { apprBusy.value = false }
}

async function rejectAccess(d: AccessRequestItem, reason: string) {
  if (apprBusy.value) return
  apprBusy.value = true
  try {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') { accessRequests.value = accessRequests.value.filter((x) => x.id !== d.id); return }
    await apiJson('/api/kb/access-requests/reject', { method: 'POST', auth: true, body: JSON.stringify({ id: d.id, reason }) })
    await loadAccessRequests()
  } catch (e: any) { alert('驳回失败：' + uploadErrText(e)) } finally { apprBusy.value = false }
}

// 已授权清单（审批人侧 · approved 存量）：后端 /api/kb/access-grants 未上线 → 静默兜底空；DEV ?preview 注入 mock。
async function loadAccessGrants() {
  const s = useSession()
  if (!s.identity?.canManage) { accessGrants.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    accessGrants.value = [
      { id: 'ag1', doc_id: 'D1', doc_title: '营销物料使用规范 v3', owner_dept: 'marketing', requester_dept: 'production', requester_name: '王伟', permission_level: 'dept_internal', reason: '生产部包装设计需引用营销规范。', decided_at: '2026-06-26' },
      { id: 'ag2', doc_id: 'D2', doc_title: '客户投诉处理 SOP', owner_dept: 'marketing', requester_dept: 'quality', requester_name: '李娜', permission_level: 'dept_internal', reason: '品质部对照投诉闭环流程。', decided_at: '2026-06-25' },
    ]
    return
  }
  try {
    const r = await apiJson<{ items: AccessGrantItem[] }>('/api/kb/access-grants', { auth: true })
    accessGrants.value = r.items || []
  } catch { accessGrants.value = [] }   // 端点未上线/出错 → 静默空，不阻断
}

// 撤销【已批准】的跨部门授权（approved→revoked）：后端同事务收窄 allowed_depts 投影 + 标脏，stage-3 收回放行。
async function revokeAccess(g: AccessGrantItem, reason: string) {
  if (apprBusy.value) return
  apprBusy.value = true
  try {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') { accessGrants.value = accessGrants.value.filter((x) => x.id !== g.id); return }
    await apiJson('/api/kb/access-requests/revoke', { method: 'POST', auth: true, body: JSON.stringify({ id: g.id, reason }) })
    await loadAccessGrants()
  } catch (e: any) { alert('撤销失败：' + uploadErrText(e)) } finally { apprBusy.value = false }
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
  try {
    const r = await apiJson<{ items: AdminItem[]; grantable_owner_depts: string[] }>('/api/kb/admin-grants', { auth: true })
    adminGrants.value = r.items || []
    grantableDepts.value = r.grantable_owner_depts || []
  } catch { adminGrants.value = []; grantableDepts.value = [] }   // 端点未上线/非 kb_admin → 静默空
}

// 授予/更新一名部门管理员（owner_depts = 权威全集,提交即覆盖）。成功返回 true。
async function grantDeptAdmin(userId: string, userName: string, ownerDepts: string[], note: string): Promise<boolean> {
  if (apprBusy.value) return false
  apprBusy.value = true
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
  } catch (e: any) { alert('授予失败：' + uploadErrText(e)); return false } finally { apprBusy.value = false }
}

// 撤销：ownerDept 指定→撤该一项；为空→撤全部并降级 employee。
async function revokeAdminGrant(userId: string, ownerDept = ''): Promise<void> {
  if (apprBusy.value) return
  apprBusy.value = true
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
  } catch (e: any) { alert('撤销失败：' + uploadErrText(e)) } finally { apprBusy.value = false }
}

async function retire(d: DocItem): Promise<{ ok: boolean; msg?: string }> {
  if (retireBusy.value) return { ok: false }
  retireBusy.value = true
  try {
    const r = await apiJson<RetireResp>('/api/kb/retire', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id }) })
    d.status_badge = '已退役'                       // 即时反映行
    void loadDocs()
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
    d.status_badge = '排队中'                       // 即时反映（NOT_INDEXED → 待重索引）；loadDocs 复算权威态
    void loadDocs()
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
    docs, filtered, approvals, accessRequests, accessGrants, adminGrants, grantableDepts, loadingDocs, loadingMoreDocs, hasMoreDocs, docScope, q, filter, sortKey, sortDir,
    newTitle, newOwner, newPerm, verCtx, uploadBusy, uploadMsg, uploadErr, uploadOk,
    dupWarn, contentDupMsg, uploadQueue, selectedNames, apprBusy, retireBusy,
    accessReqDoc, accessReqBusy, requestedDocIds, myAccessReqs,
    ownerDepts, isKbAdmin, isDeptAdmin, reviewCount, kbStats, kbConfig, kbInsights, kbGovernance, maxUploadMb, verHistory,
    // 方法
    loadDocs, loadMoreDocs, loadStats, loadConfig, loadInsights, loadGovernance, openHistory, closeHistory, setQuery, loadApprovals, sortBy, countOf,
    loadAccessRequests, approveAccess, rejectAccess, loadAccessGrants, revokeAccess, setScope,
    loadAdminGrants, grantDeptAdmin, revokeAdminGrant,
    openAccessRequest, closeAccessRequest, submitAccessRequest, accessStateOf, loadMyAccessRequests,
    enterVersionMode, exitVersionMode, applyPendingVersion, onFileSelected, doUpload,
    approve, reject, retire, restore,
  }
}

/** 仅供测试：重置 store。 */
export function __resetKb() {
  docs.value = []; kbStats.value = null; kbInsights.value = null; kbGovernance.value = null; kbConfig.value = null; verHistory.value = null; approvals.value = []; accessRequests.value = []; accessGrants.value = []; adminGrants.value = []; grantableDepts.value = []; loadingDocs.value = false; loadingMoreDocs.value = false; hasMoreDocs.value = false
  docScope.value = 'managed'; accessReqDoc.value = null; accessReqBusy.value = false; requestedDocIds.value = new Set(); myAccessReqs.value = new Map()
  q.value = ''; filter.value = ''; sortKey.value = 'updated_at'; sortDir.value = -1
  newTitle.value = ''; newOwner.value = ''; newPerm.value = 'dept_internal'; verCtx.value = null
  uploadBusy.value = false; uploadMsg.value = ''; uploadErr.value = ''; uploadOk.value = false
  dupWarn.value = ''; contentDupMsg.value = ''; uploadQueue.value = []; selectedNames.value = []
  apprBusy.value = false; retireBusy.value = false
  selectedFiles = []; docsOffset = 0; docsSeq = 0; trackSeq = 0
  if (qTimer) { clearTimeout(qTimer); qTimer = null }
  if (trackTimer) { clearTimeout(trackTimer); trackTimer = null }
}

/** 仅供测试：注入选中文件（绕过 input）。 */
export function __setSelectedFiles(files: File[]) { selectedFiles = files }
