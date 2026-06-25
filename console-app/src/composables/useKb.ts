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
}
export interface PendingItem {
  doc_id: string; version_no: number; title: string; original_filename: string
  owner_dept: string; permission_level: string; owner_name: string; created_at: string
}
export interface QueueRow { name: string; status: string; pct: number; msg: string; dupMsg?: string }
export interface VerCtx { doc_id: string; title: string; owner_dept: string; permission_level: string; current_version_no: number }

interface MyDocsResp { items: DocItem[]; has_more: boolean }
interface UploadUrlResp { upload_token: string; put_url: string; raw_key: string; doc_id: string; expires_in: number; requires_kb_admin_approval: boolean }
interface RegisterResp { doc_id: string; version_no: number; content_process_status: string; requires_kb_admin_approval: boolean; status_badge: string; idempotent: boolean; title: string; content_dups: DupDoc[]; content_dups_other: number }
interface DocStatusResp { status_badge: string; chunk_active: number; error_message: string }
interface RetireResp { status: string; retired: boolean; already: boolean; status_badge: string; note: string }

export type SortKey = 'updated_at' | 'current_version_no' | 'title' | 'owner_dept' | 'status_badge'

// ── 状态 ──
const docs = ref<DocItem[]>([])
const approvals = ref<PendingItem[]>([])
const loadingDocs = ref(false)
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
    const r = await apiJson<MyDocsResp>(`/api/kb/my-docs?limit=50${q.value ? `&q=${encodeURIComponent(q.value)}` : ''}`, { auth: true })
    if (seq !== docsSeq) return            // 竞态守卫：仅最新结果落地
    docs.value = r.items || []
  } catch { /* 保留旧表 */ } finally { if (seq === docsSeq) loadingDocs.value = false }
}

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
  if (file.size > MAX_UPLOAD_MB * 1048576) { uploadErr.value = `文件 ${(file.size / 1048576).toFixed(1)}MB，超过上限 ${MAX_UPLOAD_MB}MB，请压缩或拆分。`; return }
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
    if (f.size <= 0 || f.size > MAX_UPLOAD_MB * 1048576) { row.status = '跳过'; row.msg = f.size <= 0 ? '空文件' : '超过 50MB'; badN++; continue }
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

  return {
    // 状态
    docs, filtered, approvals, loadingDocs, q, filter, sortKey, sortDir,
    newTitle, newOwner, newPerm, verCtx, uploadBusy, uploadMsg, uploadErr, uploadOk,
    dupWarn, contentDupMsg, uploadQueue, selectedNames, apprBusy, retireBusy,
    ownerDepts, isKbAdmin,
    // 方法
    loadDocs, setQuery, loadApprovals, sortBy, countOf,
    enterVersionMode, exitVersionMode, onFileSelected, doUpload,
    approve, reject, retire,
  }
}

/** 仅供测试：重置 store。 */
export function __resetKb() {
  docs.value = []; approvals.value = []; loadingDocs.value = false
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
