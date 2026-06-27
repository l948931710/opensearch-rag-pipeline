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
export interface KbStats { total: number; active: number; retired: number; by_badge: Record<string, number> }
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
const kbConfig = ref<KbConfig | null>(null) // 后端能力配置（上传上限/类型）；缺省用常量兜底
const maxUploadBytes = computed(() => kbConfig.value?.max_upload_bytes || MAX_UPLOAD_MB * 1048576)
const maxUploadMb = computed(() => Math.round(maxUploadBytes.value / 1048576))
const verHistory = ref<{ doc: DocItem | null; versions: VersionItem[]; loading: boolean; error: string } | null>(null)
const approvals = ref<PendingItem[]>([])
const accessRequests = ref<AccessRequestItem[]>([])   // 授权申请队列（审批人侧）
const loadingDocs = ref(false)
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

async function loadDocs() {
  const seq = ++docsSeq
  loadingDocs.value = true
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
      return
    }
    // 作用域分流：全部门走只读 browse（排除 restricted、带 can_manage），本部门走 my-docs。
    const base = docScope.value === 'all' ? '/api/kb/browse?scope=all&limit=50' : '/api/kb/my-docs?limit=50'
    const r = await apiJson<MyDocsResp>(base + (q.value ? `&q=${encodeURIComponent(q.value)}` : ''), { auth: true })
    if (seq !== docsSeq) return            // 竞态守卫：仅最新结果落地
    docs.value = r.items || []
  } catch { /* 保留旧表 */ } finally { if (seq === docsSeq) loadingDocs.value = false }
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
    for (const it of (r.items || [])) m.set(it.doc_id, { status: it.status, sync_state: it.sync_state })
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
  // 概览真实口径（总数/状态分布）；失败则前端兜底用已加载文档计数（docs.length / countOf）。
  try { kbStats.value = await apiJson<KbStats>('/api/kb/stats', { auth: true }) } catch { /* 兜底 */ }
}

async function loadConfig() {
  // 上传上限/类型走后端权威，避免硬编码漂移（失败则用 MAX_UPLOAD_MB 常量兜底）。
  try { kbConfig.value = await apiJson<KbConfig>('/api/kb/config', { auth: true }) } catch { /* 兜底 */ }
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
  if (useSession().role !== 'kb_admin') { approvals.value = []; return }
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
    docs, filtered, approvals, accessRequests, loadingDocs, docScope, q, filter, sortKey, sortDir,
    newTitle, newOwner, newPerm, verCtx, uploadBusy, uploadMsg, uploadErr, uploadOk,
    dupWarn, contentDupMsg, uploadQueue, selectedNames, apprBusy, retireBusy,
    accessReqDoc, accessReqBusy, requestedDocIds, myAccessReqs,
    ownerDepts, isKbAdmin, isDeptAdmin, reviewCount, kbStats, kbConfig, maxUploadMb, verHistory,
    // 方法
    loadDocs, loadStats, loadConfig, openHistory, closeHistory, setQuery, loadApprovals, sortBy, countOf,
    loadAccessRequests, approveAccess, rejectAccess, setScope,
    openAccessRequest, closeAccessRequest, submitAccessRequest, accessStateOf, loadMyAccessRequests,
    enterVersionMode, exitVersionMode, applyPendingVersion, onFileSelected, doUpload,
    approve, reject, retire,
  }
}

/** 仅供测试：重置 store。 */
export function __resetKb() {
  docs.value = []; kbStats.value = null; kbConfig.value = null; verHistory.value = null; approvals.value = []; accessRequests.value = []; loadingDocs.value = false
  docScope.value = 'managed'; accessReqDoc.value = null; accessReqBusy.value = false; requestedDocIds.value = new Set(); myAccessReqs.value = new Map()
  q.value = ''; filter.value = ''; sortKey.value = 'updated_at'; sortDir.value = -1
  newTitle.value = ''; newOwner.value = ''; newPerm.value = 'dept_internal'; verCtx.value = null
  uploadBusy.value = false; uploadMsg.value = ''; uploadErr.value = ''; uploadOk.value = false
  dupWarn.value = ''; contentDupMsg.value = ''; uploadQueue.value = []; selectedNames.value = []
  apprBusy.value = false; retireBusy.value = false
  selectedFiles = []; docsSeq = 0; trackSeq = 0
  if (qTimer) { clearTimeout(qTimer); qTimer = null }
  if (trackTimer) { clearTimeout(trackTimer); trackTimer = null }
}

/** 仅供测试：注入选中文件（绕过 input）。 */
export function __setSelectedFiles(files: File[]) { selectedFiles = files }
