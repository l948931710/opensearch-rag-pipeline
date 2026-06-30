import { computed, ref } from 'vue'
import { apiJson, ApiError } from '@/lib/api'
import { useSession } from '@/stores/session'
import { GROUP_LABEL, deptLabel, uploadErrText } from '@/lib/kb'

// 知识贡献单例 store（员工众包问答 → 部门管理员采纳 → 走管线入库）。
// 后端契约见 api.py /api/kb/gaps · /api/kb/contributions*；身份复用 P1 session（whoami）。
// 写接口后端【现查】授权；前端 role 仅作 UI 门禁。SAE 未部署本特性时 → loader 静默兜底空。

export interface GapItem {
  question: string                  // 已脱敏的提问原文
  asks: number                      // 询问次数（去 message 扇出）
  last_days: number                 // 距最近一次提问的天数
  dept: string                      // 建议归属（仅展示）
  kind: string                      // 'no_result' | 'refusal'
  question_hash: string
  source_message_id: string
  has_pending_contribution: boolean // 已有贡献待入库（缺口仍开放）
}
export interface GapsSummary { unanswered: number; answered: number; this_month: number; contributors: number }
export interface ContributionItem {
  contribution_id: string; question: string; content: string; category_dept: string
  author_id: string; author_name: string
  review_status: string; ingestion_status: string; state: string
  doc_id: string | null; review_note: string; created_at: string; reviewed_at: string | null
}
export interface HeroItem { rank: number; author_id: string; author_name: string; count: number }

interface GapsResp { items: GapItem[]; summary: GapsSummary; has_more: boolean }
interface ContribListResp { items: ContributionItem[]; has_more: boolean }

// 归属分类下拉项（= 10 个 ACL 组码 → 中文）。后端 sanitize_owner_depts 为权威。
export const CONTRIB_DEPT_OPTS = Object.keys(GROUP_LABEL).map((id) => ({ id, name: deptLabel(id) }))

// ── 状态 ──
const gaps = ref<GapItem[]>([])
const gapsSummary = ref<GapsSummary | null>(null)
const gapsHasMore = ref(false)
const myContribs = ref<ContributionItem[]>([])
const pendingContribs = ref<ContributionItem[]>([])
const heroes = ref<HeroItem[]>([])
const loadingGaps = ref(false)
const loadErrors = ref<Record<string, string>>({})
const inflight = ref<Set<string>>(new Set())

// 贡献弹窗
const modalOpen = ref(false)
const formQuestion = ref('')
const formContent = ref('')
const formDept = ref('')
const formSourceMsg = ref('')
const formGapQuery = ref('')
const submitBusy = ref(false)
const submitErr = ref('')
const submitOk = ref(false)

function isBusy(key: string): boolean { return inflight.value.has(key) }
async function withInflight<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
  if (inflight.value.has(key)) return undefined
  inflight.value = new Set(inflight.value).add(key)
  try { return await fn() } finally { const n = new Set(inflight.value); n.delete(key); inflight.value = n }
}
function noteLoadError(key: string, e: unknown) {
  if (e instanceof ApiError && e.status === 404) { delete loadErrors.value[key]; return }   // 未上线静默
  loadErrors.value[key] = '加载失败，请重试'
}
function clearLoadError(key: string) { delete loadErrors.value[key] }

// ── DEV ?preview mock（与 useKb 同款：判 token==='dev-preview'；prod 构建 DEV=false 死代码消除）──
function _previewGaps(): GapsResp {
  return {
    items: [
      { question: '如何申请生产环境的访问密钥？', asks: 5, last_days: 2, dept: 'it', kind: 'no_result', question_hash: 'h1', source_message_id: 'm1', has_pending_contribution: false },
      { question: '2oz PP 杯在龙盛机上的标准速度是多少？', asks: 3, last_days: 6, dept: 'production', kind: 'refusal', question_hash: 'h2', source_message_id: 'm2', has_pending_contribution: true },
      { question: '差旅报销的发票抬头怎么填？', asks: 2, last_days: 1, dept: 'finance', kind: 'no_result', question_hash: 'h3', source_message_id: 'm3', has_pending_contribution: false },
    ],
    summary: { unanswered: 3, answered: 12, this_month: 4, contributors: 6 },
    has_more: false,
  }
}
function _previewMine(): ContributionItem[] {
  return [
    { contribution_id: 'c1', question: '宿舍门禁卡丢了怎么补办？', content: '联系行政前台…', category_dept: 'admin', author_id: 'preview', author_name: '设计预览', review_status: 'accepted', ingestion_status: 'searchable', state: 'searchable', doc_id: 'DOC_1', review_note: '', created_at: '2026-06-20', reviewed_at: '2026-06-21' },
    { contribution_id: 'c2', question: '年假怎么申请？', content: '在 OA…', category_dept: 'hr', author_id: 'preview', author_name: '设计预览', review_status: 'pending', ingestion_status: 'none', state: 'pending', doc_id: null, review_note: '', created_at: '2026-06-26', reviewed_at: null },
  ]
}
function _previewPending(): ContributionItem[] {
  return [
    { contribution_id: 'p1', question: '如何申请生产环境的访问密钥？', content: '提交工单到 IT，附部门负责人审批…', category_dept: 'it', author_id: 'u9', author_name: '王伟', review_status: 'pending', ingestion_status: 'none', state: 'pending', doc_id: null, review_note: '', created_at: '2026-06-27', reviewed_at: null },
  ]
}
function _previewHeroes(): HeroItem[] {
  return [
    { rank: 1, author_id: 'u1', author_name: '李娜', count: 8 },
    { rank: 2, author_id: 'u2', author_name: '张三', count: 5 },
    { rank: 3, author_id: 'preview', author_name: '设计预览', count: 3 },
  ]
}

// ── 加载 ──
async function loadGaps(offset = 0) {
  const s = useSession()
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    const r = _previewGaps(); gaps.value = r.items; gapsSummary.value = r.summary; gapsHasMore.value = false; return
  }
  loadingGaps.value = true; clearLoadError('gaps')
  try {
    const r = await apiJson<GapsResp>(`/api/kb/gaps?limit=20&offset=${offset}`, { auth: true })
    gaps.value = offset ? [...gaps.value, ...(r.items || [])] : (r.items || [])
    gapsSummary.value = r.summary || null
    gapsHasMore.value = !!r.has_more
  } catch (e) { if (!offset) { gaps.value = []; gapsSummary.value = null } ; noteLoadError('gaps', e) }
  finally { loadingGaps.value = false }
}

async function loadMine() {
  const s = useSession()
  if (import.meta.env.DEV && s.token === 'dev-preview') { myContribs.value = _previewMine(); return }
  clearLoadError('mine')
  try {
    const r = await apiJson<ContribListResp>('/api/kb/contributions/mine?limit=50', { auth: true })
    myContribs.value = r.items || []
  } catch (e) { myContribs.value = []; noteLoadError('mine', e) }
}

async function loadPending() {
  const s = useSession()
  if (!s.identity?.canManage) { pendingContribs.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') { pendingContribs.value = _previewPending(); return }
  clearLoadError('pending')
  try {
    const r = await apiJson<ContribListResp>('/api/kb/contributions/pending?limit=50', { auth: true })
    pendingContribs.value = r.items || []
  } catch (e) { pendingContribs.value = []; noteLoadError('pending', e) }
}

async function loadHeroes() {
  const s = useSession()
  if (import.meta.env.DEV && s.token === 'dev-preview') { heroes.value = _previewHeroes(); return }
  try {
    const r = await apiJson<{ items: HeroItem[] }>('/api/kb/contributions/heroes', { auth: true })
    heroes.value = r.items || []
  } catch { heroes.value = [] }
}

// ── 弹窗 / 提交 ──
function openModal(prefill?: { question?: string; dept?: string; sourceMessageId?: string; gapQuery?: string }) {
  const s = useSession()
  formQuestion.value = prefill?.question || ''
  formContent.value = ''
  // 默认归属：缺口建议部门（若合法）→ 否则员工本部门 → 否则第一项
  const own = s.identity?.aclGroups?.[0] || ''
  const valid = (d: string) => CONTRIB_DEPT_OPTS.some((o) => o.id === d)
  formDept.value = (prefill?.dept && valid(prefill.dept)) ? prefill.dept
    : (valid(own) ? own : (CONTRIB_DEPT_OPTS[0]?.id || ''))
  formSourceMsg.value = prefill?.sourceMessageId || ''
  formGapQuery.value = prefill?.gapQuery || ''
  submitErr.value = ''; submitOk.value = false
  modalOpen.value = true
}
function closeModal() { modalOpen.value = false }

async function submitContribution(): Promise<boolean> {
  if (submitBusy.value) return false
  const q = formQuestion.value.trim(); const c = formContent.value.trim()
  if (!q) { submitErr.value = '请填写问题'; return false }
  if (!c) { submitErr.value = '请填写答案/知识内容'; return false }
  submitBusy.value = true; submitErr.value = ''
  try {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') {
      myContribs.value = [{ contribution_id: 'new', question: q, content: c, category_dept: formDept.value, author_id: 'preview', author_name: '设计预览', review_status: 'pending', ingestion_status: 'none', state: 'pending', doc_id: null, review_note: '', created_at: '刚刚', reviewed_at: null }, ...myContribs.value]
      submitOk.value = true; modalOpen.value = false; return true
    }
    await apiJson('/api/kb/contributions', { method: 'POST', auth: true, body: JSON.stringify({ question: q, content: c, category_dept: formDept.value, source_message_id: formSourceMsg.value || null, gap_query: formGapQuery.value || null }) })
    submitOk.value = true; modalOpen.value = false
    await Promise.all([loadMine(), loadGaps()])
    return true
  } catch (e: any) { submitErr.value = uploadErrText(e); return false }
  finally { submitBusy.value = false }
}

// ── 审核动作（部门管理员/kb_admin）──
async function acceptContribution(c: ContributionItem, permissionLevel: 'dept_internal' | 'public' = 'dept_internal') {
  await withInflight(`ct:${c.contribution_id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { pendingContribs.value = pendingContribs.value.filter((x) => x.contribution_id !== c.contribution_id); return }
      await apiJson(`/api/kb/contributions/${encodeURIComponent(c.contribution_id)}/accept`, { method: 'POST', auth: true, body: JSON.stringify({ permission_level: permissionLevel }) })
      await Promise.all([loadPending(), loadMine()])
    } catch (e: any) { alert('采纳失败：' + uploadErrText(e)) }
  })
}
async function rejectContribution(c: ContributionItem, note: string) {
  await withInflight(`ct:${c.contribution_id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { pendingContribs.value = pendingContribs.value.filter((x) => x.contribution_id !== c.contribution_id); return }
      await apiJson(`/api/kb/contributions/${encodeURIComponent(c.contribution_id)}/reject`, { method: 'POST', auth: true, body: JSON.stringify({ note: note || null }) })
      await loadPending()
    } catch (e: any) { alert('驳回失败：' + uploadErrText(e)) }
  })
}
async function retryContribution(c: ContributionItem) {
  await withInflight(`ct:${c.contribution_id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { return }
      await apiJson(`/api/kb/contributions/${encodeURIComponent(c.contribution_id)}/retry-ingestion`, { method: 'POST', auth: true, body: JSON.stringify({}) })
      await loadMine()
    } catch (e: any) { alert('重试失败：' + uploadErrText(e)) }
  })
}

export function useContribute() {
  const session = useSession()
  const canManage = computed(() => !!session.identity?.canManage)
  // 待你审核的贡献数（红点/角标单一来源）。
  const reviewCount = computed(() => pendingContribs.value.length)
  return {
    gaps, gapsSummary, gapsHasMore, myContribs, pendingContribs, heroes, loadingGaps, loadErrors, isBusy,
    modalOpen, formQuestion, formContent, formDept, submitBusy, submitErr, submitOk,
    CONTRIB_DEPT_OPTS, canManage, reviewCount,
    loadGaps, loadMine, loadPending, loadHeroes,
    openModal, closeModal, submitContribution, acceptContribution, rejectContribution, retryContribution,
  }
}

/** 仅供测试：重置 store。 */
export function __resetContribute() {
  gaps.value = []; gapsSummary.value = null; gapsHasMore.value = false
  myContribs.value = []; pendingContribs.value = []; heroes.value = []
  loadingGaps.value = false; loadErrors.value = {}; inflight.value = new Set()
  modalOpen.value = false; formQuestion.value = ''; formContent.value = ''; formDept.value = ''
  formSourceMsg.value = ''; formGapQuery.value = ''; submitBusy.value = false; submitErr.value = ''; submitOk.value = false
}
