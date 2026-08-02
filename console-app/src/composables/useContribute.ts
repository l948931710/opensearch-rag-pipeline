import { computed, ref } from 'vue'
import { apiJson, ApiError } from '@/lib/api'
import { useSession } from '@/stores/session'
import { useDialog } from '@/composables/useDialog'
import { GROUP_LABEL, deptLabel, uploadErrText } from '@/lib/kb'

// 失败类提示统一走应用内告知框（useDialog 单例状态挂模块作用域，此处直接取用；不再用原生 alert）。
const { notice } = useDialog()

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
  // 语义归组（RAG_QA_GAP_SEMANTIC；additive，老后端缺字段自隐）：>1 = 卡片背后有 N 种问法
  phrasings?: number
  // 缺口卡上下文（2026-07-18；additive）：representative_message_id=代表提问（上下文查询键，
  // 与 source_message_id 同值）；has_context=true 才渲染「查看上下文」
  representative_message_id?: string
  has_context?: boolean
  // 本地 UI 态（非 API 字段）：本会话内刚被管理员忽略——行内原地翻转出「撤销」出路，
  // 有意不重拉列表（reload 摘除会让误点永久不可寻回：读侧排除且后端无已忽略列表端点）。
  // 撤销窗口=至下次 loadGaps()（刷新后被读侧排除自然消失，预期语义）。
  dismissed?: boolean
  // 本地 UI 态：上下文展开（懒加载缓存；null=已请求但无上文/失败）
  ctxOpen?: boolean
  ctxLoading?: boolean
  ctxTurns?: GapContextTurn[] | null
}
export interface GapContextTurn {
  question: string; answer_status: string; answer_excerpt: string; created_at: string
}
export interface GapsSummary {
  unanswered: number; answered: number; this_month: number; contributors: number
  // 审核漏斗（批次ε-3 R3，近 30 天按 reviewed_at）：null/缺省=算不出自隐（老后端兼容）
  review_accept_rate_30d?: number | null
  review_avg_hours_30d?: number | null
}
export interface ContributionItem {
  contribution_id: string; question: string; content: string; category_dept: string
  author_id: string; author_name: string
  review_status: string; ingestion_status: string; state: string
  doc_id: string | null; review_note: string; created_at: string; reviewed_at: string | null
  // 缺口溯源（批次ε-1，后端 additive 透出）：老后端无此字段 → undefined → 徽标自隐
  source_message_id?: string | null
  gap_query?: string | null
  // 失败原因（批次ε-2）：failed 行透出，作者不再瞎重试；老后端缺字段 → 兜底句
  ingestion_error?: string | null
  // 被引用数（批次ε-2 R2，cited 口径全期窗）：null/undefined=算不出 → 自隐；0=真零照显
  hits?: number | null
  // 管线徽章（批次ε-3 R1，registering 行）：台账 _kb_status_badge 词表——
  // 待审核=卡 kb_admin 放行；已隔离/未入索引=死链；缺省/None → 回落默认「已采纳·待入库」
  doc_badge?: string | null
  // 被问次数（批次ε-3 R2，仅审核队列）：近 30 天同问题的未答好提问数；null=算不出自隐，0=真零
  asks?: number | null
}
// 采纳前修订（批次ε-1）：后端 KbContributionAcceptRequest 既有契约——缺省字段=保留原值，
// 故只放【实际变更】的键，绝不传空串覆盖原文（后端 strip 后空文本会 400）。
export interface ContributionRevision { question?: string; content?: string; category_dept?: string }
export interface DeptScoreItem {
  rank: number; dept_id: number; dept_name: string; members: number; score: number
}
export interface HeroItem {
  rank: number; author_id: string; author_name: string; count: number
  hits?: number | null   // 被引用数：null=算不出 → 构成里显「—」，分数按 0 如实少算
  // 总积分（2026-08-01，预览口径 10/1/3）：score=10×采纳+1×引用+3×有效反馈；
  // 构成随行回传供副行展示（可审计）。旧后端无这些键 → 按 0 兜底、副行自隐。
  adopted?: number
  feedback?: number
  score?: number
}

interface GapsResp { items: GapItem[]; summary: GapsSummary; has_more: boolean; window_days?: number }
interface ContribListResp { items: ContributionItem[]; has_more: boolean }

// 归属分类下拉项（= 10 个 ACL 组码 → 中文）。后端 sanitize_owner_depts 为权威。
export const CONTRIB_DEPT_OPTS = Object.keys(GROUP_LABEL).map((id) => ({ id, name: deptLabel(id) }))

// ── 状态 ──
const gaps = ref<GapItem[]>([])
const gapsSummary = ref<GapsSummary | null>(null)
// 缺口滚动窗天数（批次ε-3 R3，后端下发防漂移；老后端缺字段回落 30）——
// 「待回答」不是完整积压：超窗的老缺口静默消失，卡头/空态必须标注窗口
const gapsWindowDays = ref(30)
const myContribs = ref<ContributionItem[]>([])
const pendingContribs = ref<ContributionItem[]>([])
const pendingHasMore = ref(false)   // 批次ε-1：>50 条时后端 has_more 此前被静默丢弃（假满员）
const heroes = ref<HeroItem[]>([])
const deptHeroes = ref<DeptScoreItem[]>([])       // 部门榜（同分按一级部门求和）
const myDeptHeroes = ref<HeroItem[]>([])          // 本部门个人榜
const myDeptName = ref('')
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
// 重投警示（ε-5 R1）：入库受阻行「修改重交」按成因带警示进弹窗（行内警示在弹窗打开后已离开视野）
const formWarning = ref('')
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
      { question: '如何申请生产环境的访问密钥？', asks: 5, last_days: 2, dept: 'it', kind: 'no_result', question_hash: 'h1', source_message_id: 'm1', has_pending_contribution: false, phrasings: 3, has_context: true, representative_message_id: 'm1' },
      { question: '2oz PP 杯在龙盛机上的标准速度是多少？', asks: 3, last_days: 6, dept: 'production', kind: 'refusal', question_hash: 'h2', source_message_id: 'm2', has_pending_contribution: true },
      { question: '差旅报销的发票抬头怎么填？', asks: 2, last_days: 1, dept: 'finance', kind: 'no_result', question_hash: 'h3', source_message_id: 'm3', has_pending_contribution: false },
    ],
    summary: { unanswered: 3, answered: 12, this_month: 4, contributors: 6,
               review_accept_rate_30d: 0.78, review_avg_hours_30d: 26.4 },
    window_days: 30,
    has_more: false,
  }
}
function _previewMine(): ContributionItem[] {
  return [
    { contribution_id: 'c1', question: '宿舍门禁卡丢了怎么补办？', content: '联系行政前台…', category_dept: 'admin', author_id: 'preview', author_name: '设计预览', review_status: 'accepted', ingestion_status: 'searchable', state: 'searchable', doc_id: 'DOC_1', review_note: '', created_at: '2026-06-20', reviewed_at: '2026-06-21', hits: 6 },
    { contribution_id: 'c2', question: '年假怎么申请？', content: '在 OA…', category_dept: 'hr', author_id: 'preview', author_name: '设计预览', review_status: 'pending', ingestion_status: 'none', state: 'pending', doc_id: null, review_note: '', created_at: '2026-06-26', reviewed_at: null },
    { contribution_id: 'c3', question: '模具验收单在哪下载？', content: '在 OA 表单中心搜「模具验收」…', category_dept: 'production', author_id: 'preview', author_name: '设计预览', review_status: 'rejected', ingestion_status: 'none', state: 'rejected', doc_id: null, review_note: '', created_at: '2026-06-25', reviewed_at: '2026-06-26' },
  ]
}
function _previewPending(): ContributionItem[] {
  return [
    { contribution_id: 'p1', question: '如何申请生产环境的访问密钥？', content: '提交工单到 IT，附部门负责人审批…', category_dept: 'it', author_id: 'u9', author_name: '王伟', review_status: 'pending', ingestion_status: 'none', state: 'pending', doc_id: null, review_note: '', created_at: '2026-06-27', reviewed_at: null, source_message_id: 'm1', gap_query: '生产环境访问密钥在哪申请', asks: 5 },
    { contribution_id: 'p2', question: '2oz PP 杯的模具保养周期是多久？', content: '标准周期为每生产 30 万模次做一级保养（清洁流道、检查顶针），100 万模次做二级保养（拆模检查型腔磨损、更换密封件）。\n夏季高温连续生产时一级保养提前到 25 万模次。\n保养记录填在《模具保养台账》并由当班班长签字确认，台账每月底交设备科归档。', category_dept: 'production', author_id: 'u12', author_name: '陈强', review_status: 'pending', ingestion_status: 'none', state: 'pending', doc_id: null, review_note: '', created_at: '2026-06-28', reviewed_at: null },
  ]
}
function _previewHeroes(): HeroItem[] {
  return [
    { rank: 1, author_id: 'u1', author_name: '李娜', count: 8, hits: 41, adopted: 8, feedback: 2, score: 127 },
    { rank: 2, author_id: 'u2', author_name: '张三', count: 5, hits: 0, adopted: 5, feedback: 4, score: 62 },
    { rank: 3, author_id: 'preview', author_name: '设计预览', count: 3, hits: 12, adopted: 3, feedback: 0, score: 42 },
  ]
}

// ── 加载 ──
// 批次ε-4：Top 30 排行单页化——后端本就按 asks desc 排序，只取前 30、不再翻页；
// 全集规模由 summary.unanswered 承载（统计卡+卡头徽标+截断尾注），杜绝静默截断。
async function loadGaps() {
  const s = useSession()
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    const r = _previewGaps(); gaps.value = r.items; gapsSummary.value = r.summary; return
  }
  loadingGaps.value = true; clearLoadError('gaps')
  try {
    const r = await apiJson<GapsResp>('/api/kb/gaps?limit=30', { auth: true })
    gaps.value = r.items || []
    gapsSummary.value = r.summary || null
    gapsWindowDays.value = r.window_days || 30   // 老后端缺字段=其窗口就是 30 天，回落值如实
  } catch (e) {
    gaps.value = []; gapsSummary.value = null; noteLoadError('gaps', e)
  }
  finally { loadingGaps.value = false }
}

/** 缺口卡上下文展开（2026-07-18）：懒加载该代表提问所在会话的前几轮问答。
 *  仅 has_context=true 的行会被调用；结果缓存在行上（ctxTurns），再点即折叠。 */
async function toggleGapContext(g: GapItem) {
  if (g.ctxOpen) { g.ctxOpen = false; return }
  g.ctxOpen = true
  if (g.ctxTurns !== undefined || g.ctxLoading) return   // 已加载过（含空）/在途
  const mid = g.representative_message_id || g.source_message_id
  if (!mid) { g.ctxTurns = null; return }
  g.ctxLoading = true
  try {
    const r = await apiJson<{ items: GapContextTurn[] }>(
      `/api/kb/gaps/context?message_id=${encodeURIComponent(mid)}`, { auth: true })
    g.ctxTurns = r.items?.length ? r.items : null
  } catch { g.ctxTurns = null }   // 403/404/网络失败一律显「无上文」，不打断认领流
  finally { g.ctxLoading = false }
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

async function loadPending(offset = 0) {
  const s = useSession()
  if (!s.identity?.canManage) { pendingContribs.value = []; pendingHasMore.value = false; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') { pendingContribs.value = _previewPending(); pendingHasMore.value = false; return }
  clearLoadError('pending')
  try {
    const r = await apiJson<ContribListResp>(`/api/kb/contributions/pending?limit=50&offset=${offset}`, { auth: true })
    // 批次ε-1：offset>0=「加载更多」追加；0=回首页替换（审核队列专属分页——gaps 已 ε-4 单页化）
    pendingContribs.value = offset ? [...pendingContribs.value, ...(r.items || [])] : (r.items || [])
    pendingHasMore.value = !!r.has_more
  } catch (e) {
    if (!offset) pendingContribs.value = []
    noteLoadError('pending', e)
  }
}

async function loadHeroes() {
  const s = useSession()
  if (import.meta.env.DEV && s.token === 'dev-preview') { heroes.value = _previewHeroes(); return }
  try {
    const r = await apiJson<{ items: HeroItem[]; dept_items?: DeptScoreItem[]
                              my_dept_items?: HeroItem[]; my_dept_name?: string }>(
      '/api/kb/contributions/heroes', { auth: true })
    heroes.value = r.items || []
    deptHeroes.value = r.dept_items || []
    myDeptHeroes.value = r.my_dept_items || []
    myDeptName.value = r.my_dept_name || ''
  } catch { heroes.value = []; deptHeroes.value = []; myDeptHeroes.value = []; myDeptName.value = '' }
}

// ── 弹窗 / 提交 ──
// 批次ε-2：prefill 扩展 content（被驳回「修改重交」带旧稿重开表单）；既有调用点（GapList 等）
// 不传 content → 照旧空白起草，行为不变。
function openModal(prefill?: { question?: string; content?: string; dept?: string; sourceMessageId?: string; gapQuery?: string; warning?: string }) {
  const s = useSession()
  formQuestion.value = prefill?.question || ''
  formContent.value = prefill?.content || ''
  // 默认归属：缺口建议部门（若合法）→ 否则员工本部门 → 否则第一项
  const own = s.identity?.aclGroups?.[0] || ''
  const valid = (d: string) => CONTRIB_DEPT_OPTS.some((o) => o.id === d)
  formDept.value = (prefill?.dept && valid(prefill.dept)) ? prefill.dept
    : (valid(own) ? own : (CONTRIB_DEPT_OPTS[0]?.id || ''))
  formSourceMsg.value = prefill?.sourceMessageId || ''
  formGapQuery.value = prefill?.gapQuery || ''
  formWarning.value = prefill?.warning || ''
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
// 批次ε-1：可选 revised=采纳前修订（后端既有契约）；返回是否成功——组件据此决定是否退出修订态
//（失败保留编辑内容不丢稿）。既有调用点忽略返回值，行为不变。
async function acceptContribution(c: ContributionItem, permissionLevel: 'dept_internal' | 'public' = 'dept_internal',
                                  revised?: ContributionRevision): Promise<boolean> {
  const ok = await withInflight(`ct:${c.contribution_id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { pendingContribs.value = pendingContribs.value.filter((x) => x.contribution_id !== c.contribution_id); return true }
      const r = await apiJson<{ requires_kb_admin_approval?: boolean }>(`/api/kb/contributions/${encodeURIComponent(c.contribution_id)}/accept`, { method: 'POST', auth: true, body: JSON.stringify({ permission_level: permissionLevel, ...(revised || {}) }) })
      // P2-16：dept_admin 采纳「全员公开」→ 后端登记为待审批（不再直通入库），提示放行前提
      if (r?.requires_kb_admin_approval) void notice({ title: '已采纳，等待放行', message: '全员公开的贡献需知识库管理员在「待审批」中放行后才会入库检索。' })
      // 批次α-⑤：兄弟面板联动——loadGaps() 让「待回答」的 has_pending_contribution 徽标与
      // 统计卡即时翻转（此前采纳后缺口列表原样、同一问题会被第二人重复回答）。回首页语义
      //（offset 缺省 0），不沿用「加载更多」的偏移。
      await Promise.all([loadPending(), loadMine(), loadGaps()])
      return true
    } catch (e: any) { void notice({ title: '采纳失败', message: uploadErrText(e), danger: true }); return false }
  })
  return ok === true
}
async function rejectContribution(c: ContributionItem, note: string) {
  await withInflight(`ct:${c.contribution_id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { pendingContribs.value = pendingContribs.value.filter((x) => x.contribution_id !== c.contribution_id); return }
      await apiJson(`/api/kb/contributions/${encodeURIComponent(c.contribution_id)}/reject`, { method: 'POST', auth: true, body: JSON.stringify({ note: note || null }) })
      // 批次α-⑤：驳回同样联动兄弟面板——loadMine()（审核人=作者时「我的贡献」即时显「已驳回」）
      // + loadGaps()（该问题回到可回答态，徽标翻回）。
      await Promise.all([loadPending(), loadMine(), loadGaps()])
    } catch (e: any) { void notice({ title: '驳回失败', message: uploadErrText(e), danger: true }) }
  })
}
async function retryContribution(c: ContributionItem) {
  await withInflight(`ct:${c.contribution_id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { return }
      await apiJson(`/api/kb/contributions/${encodeURIComponent(c.contribution_id)}/retry-ingestion`, { method: 'POST', auth: true, body: JSON.stringify({}) })
      await loadMine()
    } catch (e: any) { void notice({ title: '重试失败', message: uploadErrText(e), danger: true }) }
  })
}

// ── 「忽略此缺口」（schema/041，2026-07-15 拍板交 dept_admin）────────────────
// 成功后【行内翻转，不重拉】：本仓撤销类动作的 reload 范式在此不适用——读侧排除 +
// 后端无已忽略列表端点，reload 摘除会让误点永久不可寻回。失败保持原状（绝不乐观翻转）。
// 确认弹窗在组件层（工程惯例：confirm 不进 composable，spec 直调用例会挂）。
// 语义组开启时后端自动联动同组成员，前端不感知。
async function dismissGap(g: GapItem, reason: string) {
  await withInflight(`gap:${g.question_hash}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { g.dismissed = true; return }
      await apiJson('/api/kb/gaps/dismiss', { method: 'POST', auth: true, body: JSON.stringify({ question_hash: g.question_hash, question: g.question, reason: reason || '' }) })
      g.dismissed = true
    } catch (e: any) { void notice({ title: '忽略失败', message: uploadErrText(e), danger: true }) }
  })
}
async function restoreGap(g: GapItem) {
  await withInflight(`gap:${g.question_hash}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { g.dismissed = false; return }
      await apiJson('/api/kb/gaps/restore', { method: 'POST', auth: true, body: JSON.stringify({ question_hash: g.question_hash }) })
      g.dismissed = false
    } catch (e: any) { void notice({ title: '恢复失败', message: uploadErrText(e), danger: true }) }
  })
}

// ── 「已忽略」折叠区（2026-07-15 拍板补齐）：刷新后撤销的唯一 UI 入口 ─────────
export interface DismissedGap {
  question_hash: string; question_preview: string; reason: string
  dismissed_by_name: string; dismissed_at: string
}
const dismissedGaps = ref<DismissedGap[]>([])
// （分支侧 P0-D 身份指纹判废随大合并收敛；main 无运行期身份切换，指纹恒等。）
async function loadDismissed() {
  const s = useSession()
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    dismissedGaps.value = [{ question_hash: 'hd1', question_preview: '今天食堂有什么菜', reason: '闲聊噪音', dismissed_by_name: '设计预览', dismissed_at: '2026-07-14T10:00:00' }]
    return
  }
  try {
    const r = await apiJson<{ items: DismissedGap[] }>('/api/kb/gaps/dismissed', { auth: true })
    dismissedGaps.value = r.items || []
  } catch (e) {
    dismissedGaps.value = []; noteLoadError('dismissed', e)   // 404=老后端静默（noteLoadError 内建）
  }
}
// 从折叠区恢复：restore 成功 → 移出折叠区 + 重拉缺口列表（该缺口回到「待回答」——
// 与行内「撤销」的本地翻转不同，这里的行早已不在 gaps 数组里，重拉才是诚实闭环）。
async function restoreDismissed(d: DismissedGap) {
  await withInflight(`gap:${d.question_hash}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') {
        dismissedGaps.value = dismissedGaps.value.filter((x) => x.question_hash !== d.question_hash); return
      }
      await apiJson('/api/kb/gaps/restore', { method: 'POST', auth: true, body: JSON.stringify({ question_hash: d.question_hash }) })
      dismissedGaps.value = dismissedGaps.value.filter((x) => x.question_hash !== d.question_hash)
      await loadGaps()
    } catch (e: any) { void notice({ title: '恢复失败', message: uploadErrText(e), danger: true }) }
  })
}

export function useContribute() {
  const session = useSession()
  const canManage = computed(() => !!session.identity?.canManage)
  // 待你审核的贡献数（红点/角标单一来源）。
  const reviewCount = computed(() => pendingContribs.value.length)
  return {
    gaps, gapsSummary, gapsWindowDays, myContribs, pendingContribs, pendingHasMore, heroes, loadingGaps, loadErrors, isBusy,
    deptHeroes, myDeptHeroes, myDeptName,
    modalOpen, formQuestion, formContent, formDept, formWarning, submitBusy, submitErr, submitOk,
    CONTRIB_DEPT_OPTS, canManage, reviewCount,
    loadGaps, toggleGapContext, loadMine, loadPending, loadHeroes,
    openModal, closeModal, submitContribution, acceptContribution, rejectContribution, retryContribution,
    dismissGap, restoreGap, dismissedGaps, loadDismissed, restoreDismissed,
  }
}

/** 仅供测试：重置 store。（分支侧 P0-D 已升级为身份切换共用 _resetContributeState——随大合并收敛。） */
export function __resetContribute() {
  gaps.value = []; gapsSummary.value = null; gapsWindowDays.value = 30; dismissedGaps.value = []
  myContribs.value = []; pendingContribs.value = []; pendingHasMore.value = false; heroes.value = []
  loadingGaps.value = false; loadErrors.value = {}; inflight.value = new Set()
  modalOpen.value = false; formQuestion.value = ''; formContent.value = ''; formDept.value = ''
  formSourceMsg.value = ''; formGapQuery.value = ''; formWarning.value = ''; submitBusy.value = false; submitErr.value = ''; submitOk.value = false
}
