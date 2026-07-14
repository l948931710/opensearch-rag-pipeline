import { computed, ref } from 'vue'
import { useSession } from '@/stores/session'
import { ApiError, apiFetch, apiJson } from '@/lib/api'
import { useDialog } from '@/composables/useDialog'
import { __resetIdentityScope, identityFingerprint, registerIdentityScopedStore, syncIdentityScope } from '@/composables/identityScope'

// 本体消解工作台（P0 PR7；后端 routes/ontology.py）。
// GET /api/ontology/workbench（open case 队列，候选已富化）+ /api/ontology/coverage（覆盖率卡片）；
// POST /api/ontology/cases/{id}/confirm|dismiss（steward 处置；授权服务端硬校验——
// kb_admin / stewardship scope 覆盖的 dept_admin）。
// RAG_ONTOLOGY_ENABLE 未开（404）或无权（403）→ supported=false，ManageView 不渲染该 tab。

export interface OntologyCandidate {
  candidate_id: string
  case_id: string
  // PR-F 受限候选（target_visible=false）三字段为常量 null 占位（零字段泄露）——UI 不得渲染
  target_object_id: string | null
  target_revision: string | null
  method: string | null
  confidence: number | null
  features_json: string | null
  canonical_ref: string | null
  title: string | null
  object_type: string | null
  target_status: string | null
  // PR-F（P0-01/06）：跨部门不可读候选 target_visible=false（零字段）；可读候选带归属/密级
  target_visible?: boolean | null
  owner_dept?: string | null
  data_classification?: string | null
}

export interface OntologyCase {
  case_id: string
  namespace: string
  raw_value: string
  norm_value: string
  object_type_hint: string | null
  status: string
  seen_count: number
  first_seen_at: string | number | null
  last_seen_at: string | number | null
  evidence_json: string | null
  candidates: OntologyCandidate[]
  steward_dept: string | null
}

export interface OntologyCoverage {
  active_identifiers: number
  auto_active: number
  open_cases: number
  resolved_cases: number
  dismissed_cases: number
  resolution_coverage: number | null
  manual_review_rate: number | null
  // PR-F：分母口径——population=源快照真实分母 / approx=处置率近似（UI 必须明示）
  denominator?: 'population' | 'approx'
  population_records?: number | null
}

export interface OntologyObjectHit {
  object_id: string
  object_type: string
  canonical_ref: string
  title: string
  status: string
  owner_dept?: string | null
  data_classification?: string | null
}

const cases = ref<OntologyCase[]>([])
const coverage = ref<OntologyCoverage | null>(null)
// null=尚未探测（tab 先不显）；false=404/403（flag 未开或无权）；true=端点在。
const supported = ref<boolean | null>(null)
const loadError = ref('')
const inflight = ref<Set<string>>(new Set())
// 批次δ F4：object_type 服务端筛选（''=全部）+ keyset 游标分页（PR-I 后端已备，前端此前
// 硬编码 limit=50 丢弃 next_cursor——积压 >50 静默截断）。
// 语义纪律：loadOntology=第一页【替换】（探测/强刷/筛选变更共用，游标归零）；
// loadMoreOntology=【追加】（绝不与 force 布尔糅进同一路径——强刷必须拿从头快照）。
const objectTypeFilter = ref('')
const nextCursor = ref<string | null>(null)
const loadingMore = ref(false)

const PAGE_LIMIT = 50
function _workbenchQuery(cursor?: string | null): string {
  const p = new URLSearchParams()
  p.set('limit', String(PAGE_LIMIT))
  if (objectTypeFilter.value) p.set('object_type', objectTypeFilter.value)
  if (cursor) p.set('cursor', cursor)
  return `/api/ontology/workbench?${p.toString()}`
}

const STALE_MS = 30_000
let lastLoadedAt = 0
// PR-I（P2「singleton 跨身份残留」）：模块级状态挂在 app 生命周期上——切换身份
// （登出/换号）时 cases/coverage/supported 会带着上个管理员的数据泄给下个身份。
// P0-D 起改挂共享 identityScope（本文件原有的 per-uid 跟踪即其前身）：键含 userId/role/ACL/token，
// 变了统一同步清空；loader 入口 lazy 对账，在途响应按身份指纹判废。

function isBusy(key: string): boolean { return inflight.value.has(key) }
async function withInflight<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
  if (inflight.value.has(key)) return undefined
  inflight.value = new Set(inflight.value).add(key)
  try { return await fn() } finally { const n = new Set(inflight.value); n.delete(key); inflight.value = n }
}

async function refreshOntologyCoverage() {
  const fp = identityFingerprint()
  try {
    const cov = await apiJson<OntologyCoverage>('/api/ontology/coverage', { auth: true })
    if (fp !== identityFingerprint()) return   // 身份已切换：旧身份的覆盖率不落地
    coverage.value = cov
  } catch { /* 覆盖率是装饰面：失败不置错、不影响队列与 tab 可见性 */ }
}

/** ?preview 满数据 mock（5 条跨两种类型）：3+2 两页演示追加分页；单条候选形态与 oc1 同源。 */
function _previewCases(): OntologyCase[] {
  const mk = (id: string, raw: string, hint: string, seen: number, title: string, ref: string): OntologyCase => ({
    case_id: id, namespace: 'u8', raw_value: raw, norm_value: raw,
    object_type_hint: hint, status: 'open', seen_count: seen,
    first_seen_at: '2026-07-09 10:00', last_seen_at: '2026-07-10 09:12', evidence_json: null,
    steward_dept: 'pmc',
    candidates: [{
      candidate_id: 'cd-' + id, case_id: id, target_object_id: 'o-' + id, target_revision: null,
      method: 'rule', confidence: 0.85, features_json: null, canonical_ref: ref,
      title, object_type: hint, target_status: 'active',
    }],
  })
  return [
    mk('oc1', 'ABC123-M', 'product', 4, '6.2口径龙虾杯', 'FLP-P-000123'),
    mk('oc2', 'CUP-12OZ-B', 'product', 3, '12oz 双淋膜纸杯', 'FLP-P-000208'),
    mk('oc3', 'MLD-88A', 'mold', 3, '88A 注塑模', 'FLP-M-000031'),
    mk('oc4', 'PP-T30', 'material', 2, 'PP 改性料 T30', 'FLP-MT-000012'),
    mk('oc5', 'BOX-660-40', 'packing_spec', 2, '660 箱规 40 只装', 'FLP-PS-000005'),
  ]
}

async function loadOntology(force = false) {
  syncIdentityScope()               // 身份切换：上个身份的队列/覆盖率/探测结果全部作废（P0-D 共享设施）
  const fp = identityFingerprint()  // 在途判废基准
  const s = useSession()
  if (!s.identity?.canManage) {
    cases.value = []
    coverage.value = null
    supported.value = false          // 非管理员：tab 不显，且不残留上个身份的 supported=true
    return
  }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    // 多页 mock（批次δ）：3+2 两页演示「加载更多」；client 侧套 objectTypeFilter 让筛选也能演示。
    const all = _previewCases()
    const filtered = objectTypeFilter.value ? all.filter((c) => c.object_type_hint === objectTypeFilter.value) : all
    cases.value = filtered.slice(0, 3)
    nextCursor.value = filtered.length > 3 ? 'preview-page-2' : null
    coverage.value = {
      active_identifiers: 1284, auto_active: 402, open_cases: 57, resolved_cases: 620,
      dismissed_cases: 38, resolution_coverage: 0.72, manual_review_rate: 0.69,
    }
    supported.value = true
    return
  }
  if (!force && Date.now() - lastLoadedAt < STALE_MS) return
  lastLoadedAt = Date.now()
  loadError.value = ''
  try {
    const r = await apiJson<{ items: OntologyCase[]; next_cursor?: string | null }>(_workbenchQuery(), { auth: true })
    if (fp !== identityFingerprint()) return   // 身份已切换：旧身份的队列整体丢弃
    cases.value = r.items || []
    nextCursor.value = r.next_cursor || null   // 满页才有游标；null=到底
    supported.value = true
    void refreshOntologyCoverage()
  } catch (e) {
    if (fp !== identityFingerprint()) return   // 同上：旧身份的失败也不落错误/探测态
    lastLoadedAt = 0
    cases.value = []
    if (e instanceof ApiError && e.status === 404) { supported.value = false; return }   // flag 未开：静默隐藏
    if (e instanceof ApiError && e.status === 403) { supported.value = false; return }   // 员工/无权：不显 tab
    loadError.value = '加载失败，请重试'
  }
}

/** 筛选变更：置新值并【强制】重拉第一页——不传 force 会被 30s staleness 门静默吞掉
 *  （批次δ 审计风险 1：筛选变了列表纹丝不动且无报错）。同值 no-op。 */
async function setOntologyObjectTypeFilter(t: string) {
  if (objectTypeFilter.value === t) return
  objectTypeFilter.value = t
  nextCursor.value = null
  await loadOntology(true)
}

/** 追加下一页（keyset 游标；offset 是后端测试点名的「边处置边翻页跳行/重行」病根，不用）。
 *  dept_admin 可能拿到「空页但仍有游标」（服务端精判在游标编码之后）——如实追加 0 条并
 *  保留继续加载的能力，绝不把空页误判成到底/错误。失败 → notice 且游标保留可重试。 */
async function loadMoreOntology(): Promise<number> {
  if (loadingMore.value || !nextCursor.value) return 0
  syncIdentityScope()
  const fp = identityFingerprint()
  const filterAtCall = objectTypeFilter.value
  const s = useSession()
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    const all = _previewCases()
    const filtered = filterAtCall ? all.filter((c) => c.object_type_hint === filterAtCall) : all
    const seen = new Set(cases.value.map((c) => c.case_id))
    const more = filtered.filter((c) => !seen.has(c.case_id))
    cases.value = [...cases.value, ...more]
    nextCursor.value = null
    return more.length
  }
  loadingMore.value = true
  try {
    const r = await apiJson<{ items: OntologyCase[]; next_cursor?: string | null }>(
      _workbenchQuery(nextCursor.value), { auth: true })
    // 身份或筛选中途变了：这页属于旧上下文，整体丢弃（新上下文已由 loadOntology(true) 重建）
    if (fp !== identityFingerprint() || filterAtCall !== objectTypeFilter.value) return 0
    const seen = new Set(cases.value.map((c) => c.case_id))
    const more = (r.items || []).filter((c) => !seen.has(c.case_id))
    cases.value = [...cases.value, ...more]
    nextCursor.value = r.next_cursor || null
    return more.length
  } catch {
    if (fp === identityFingerprint()) {
      const { notice } = useDialog()
      void notice({ title: '加载更多失败', message: '网络异常，请重试（已加载条目不受影响）。', danger: true })
    }
    return -1   // 失败 ≠ 成功空页——调用方据此区分「管辖滤空提示」与「网络失败弹窗」两种语义
  } finally {
    loadingMore.value = false
  }
}

async function postDecision(kase: OntologyCase, path: string, body: Record<string, unknown>,
                            failTitle: string): Promise<boolean> {
  syncIdentityScope()   // 处置入口对账（服务端授权仍是硬校验）
  const { notice } = useDialog()
  const done = await withInflight(`onto:${kase.case_id}`, async () => {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') {
      cases.value = cases.value.filter((x) => x.case_id !== kase.case_id)
      return true
    }
    try {
      const res = await apiFetch(`/api/ontology/cases/${kase.case_id}/${path}`, {
        method: 'POST', auth: true, body: JSON.stringify(body),
      })
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try { detail = (await res.json())?.detail || detail } catch { /* 非 JSON */ }
        void notice({ title: failTitle, message: detail, danger: true })
        if (res.status === 409) await loadOntology(true)   // 已被并发处置 → 强刷队列
        return false
      }
      cases.value = cases.value.filter((x) => x.case_id !== kase.case_id)
      void refreshOntologyCoverage()                        // 处置改变覆盖率分母
      return true
    } catch {
      void notice({ title: failTitle, message: '网络异常，请重试', danger: true })
      return false
    }
  })
  return done === true
}

/** 确认（含改指到搜索选中的对象）：铸 active 别名 + case→resolved。
 *  PR-F（P0-06 HITL）：note **必填**（服务端 400 兜底）——确认与驳回同纪律。 */
async function confirmOntologyCase(kase: OntologyCase, target: { target_object_id: string; target_revision?: string | null }, note: string) {
  return postDecision(kase, 'confirm', {
    target_object_id: target.target_object_id,
    target_revision: target.target_revision || null,
    note,
  }, '确认失败')
}

/** 驳回（终止处置；理由必填，服务端 400 兜底）。 */
async function dismissOntologyCase(kase: OntologyCase, note: string) {
  return postDecision(kase, 'dismiss', { note }, '驳回失败')
}

/** 批量驳回（PR-I P2：大积压处置）。逐条独立结果；返回成功数（失败条目留在队列）。 */
async function batchDismissOntologyCases(caseIds: string[], note: string): Promise<number> {
  syncIdentityScope()   // 处置入口对账（服务端授权仍是硬校验）
  const { notice } = useDialog()
  const done = await withInflight('onto:batch', async () => {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') {
      cases.value = cases.value.filter((x) => !caseIds.includes(x.case_id))
      return caseIds.length
    }
    try {
      const res = await apiFetch('/api/ontology/cases/batch-dismiss', {
        method: 'POST', auth: true, body: JSON.stringify({ case_ids: caseIds, note }),
      })
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try { detail = (await res.json())?.detail || detail } catch { /* 非 JSON */ }
        void notice({ title: '批量驳回失败', message: detail, danger: true })
        return 0
      }
      const body = await res.json() as { results: { case_id: string; status: string }[]; dismissed: number }
      const okIds = new Set(body.results.filter((r) => r.status === 'dismissed').map((r) => r.case_id))
      cases.value = cases.value.filter((x) => !okIds.has(x.case_id))
      void refreshOntologyCoverage()
      const failed = body.results.length - okIds.size
      if (failed > 0) {
        void notice({ title: '部分未驳回', message: `${okIds.size} 条已驳回，${failed} 条失败/已被处置（已刷新队列）`, danger: true })
        void loadOntology(true)
      }
      return okIds.size
    } catch {
      void notice({ title: '批量驳回失败', message: '网络异常，请重试', danger: true })
      return 0
    }
  })
  return done ?? 0
}

/** 手动指定时的目标对象搜索（确认/改指选择器）。 */
async function searchOntologyObjects(objectType: string, q: string): Promise<OntologyObjectHit[]> {
  try {
    const r = await apiJson<{ items: OntologyObjectHit[] }>(
      `/api/ontology/objects?object_type=${encodeURIComponent(objectType)}&q=${encodeURIComponent(q)}`,
      { auth: true })
    return r.items || []
  } catch { return [] }
}

const ontologyBacklogCount = computed(() => cases.value.length)

export function useOntology() {
  return {
    ontologyCases: cases,
    ontologyCoverage: coverage,
    ontologySupported: supported,
    ontologyError: loadError,
    ontologyBacklogCount,
    ontologyObjectTypeFilter: objectTypeFilter,
    ontologyHasMore: computed(() => !!nextCursor.value),
    ontologyLoadingMore: loadingMore,
    setOntologyObjectTypeFilter,
    loadMoreOntology,
    isOntologyBusy: isBusy,
    loadOntology,
    confirmOntologyCase,
    dismissOntologyCase,
    batchDismissOntologyCases,
    searchOntologyObjects,
  }
}

// 队列/覆盖率/探测/错误/在途/freshness 全部作废（运行期身份切换与测试复位共用）。
function _resetOntologyState() {
  cases.value = []
  coverage.value = null
  supported.value = null
  loadError.value = ''
  inflight.value = new Set()
  objectTypeFilter.value = ''      // 批次δ：筛选/游标是身份作用域状态——换号不残留上个人的筛选
  nextCursor.value = null
  loadingMore.value = false
  lastLoadedAt = 0
}

export function __resetOntology() {
  _resetOntologyState()
  __resetIdentityScope()   // 测试复位顺带忘掉已观测身份（下次 sync 首见采纳）
}

// P0-D：身份/token 变更 → 同步清空（identityScope 统一触发）。
registerIdentityScopedStore('ontology', _resetOntologyState)
