import { computed, ref } from 'vue'
import { useSession } from '@/stores/session'
import { ApiError, apiFetch, apiJson } from '@/lib/api'
import { useDialog } from '@/composables/useDialog'

// 本体消解工作台（P0 PR7；后端 routes/ontology.py）。
// GET /api/ontology/workbench（open case 队列，候选已富化）+ /api/ontology/coverage（覆盖率卡片）；
// POST /api/ontology/cases/{id}/confirm|dismiss（steward 处置；授权服务端硬校验——
// kb_admin / stewardship scope 覆盖的 dept_admin）。
// RAG_ONTOLOGY_ENABLE 未开（404）或无权（403）→ supported=false，ManageView 不渲染该 tab。

export interface OntologyCandidate {
  candidate_id: string
  case_id: string
  target_object_id: string
  target_revision: string | null
  method: string
  confidence: number
  features_json: string | null
  canonical_ref: string | null
  title: string | null
  object_type: string | null
  target_status: string | null
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
}

export interface OntologyObjectHit {
  object_id: string
  object_type: string
  canonical_ref: string
  title: string
  status: string
}

const cases = ref<OntologyCase[]>([])
const coverage = ref<OntologyCoverage | null>(null)
// null=尚未探测（tab 先不显）；false=404/403（flag 未开或无权）；true=端点在。
const supported = ref<boolean | null>(null)
const loadError = ref('')
const inflight = ref<Set<string>>(new Set())

const STALE_MS = 30_000
let lastLoadedAt = 0

function isBusy(key: string): boolean { return inflight.value.has(key) }
async function withInflight<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
  if (inflight.value.has(key)) return undefined
  inflight.value = new Set(inflight.value).add(key)
  try { return await fn() } finally { const n = new Set(inflight.value); n.delete(key); inflight.value = n }
}

async function refreshOntologyCoverage() {
  try {
    coverage.value = await apiJson<OntologyCoverage>('/api/ontology/coverage', { auth: true })
  } catch { /* 覆盖率是装饰面：失败不置错、不影响队列与 tab 可见性 */ }
}

async function loadOntology(force = false) {
  const s = useSession()
  if (!s.identity?.canManage) { cases.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    cases.value = [{
      case_id: 'oc1', namespace: 'u8', raw_value: 'ABC123-M', norm_value: 'ABC123-M',
      object_type_hint: 'product', status: 'open', seen_count: 4,
      first_seen_at: '2026-07-09 10:00', last_seen_at: '2026-07-10 09:12', evidence_json: null,
      steward_dept: 'pmc',
      candidates: [{
        candidate_id: 'cd1', case_id: 'oc1', target_object_id: 'o1', target_revision: null,
        method: 'rule', confidence: 0.85, features_json: null, canonical_ref: 'FLP-P-000123',
        title: '6.2口径龙虾杯', object_type: 'product', target_status: 'active',
      }],
    }]
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
    const r = await apiJson<{ items: OntologyCase[] }>('/api/ontology/workbench?limit=50', { auth: true })
    cases.value = r.items || []
    supported.value = true
    void refreshOntologyCoverage()
  } catch (e) {
    lastLoadedAt = 0
    cases.value = []
    if (e instanceof ApiError && e.status === 404) { supported.value = false; return }   // flag 未开：静默隐藏
    if (e instanceof ApiError && e.status === 403) { supported.value = false; return }   // 员工/无权：不显 tab
    loadError.value = '加载失败，请重试'
  }
}

async function postDecision(kase: OntologyCase, path: string, body: Record<string, unknown>,
                            failTitle: string): Promise<boolean> {
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

/** 确认（含改指到搜索选中的对象）：铸 active 别名 + case→resolved。 */
async function confirmOntologyCase(kase: OntologyCase, target: { target_object_id: string; target_revision?: string | null }, note?: string) {
  return postDecision(kase, 'confirm', {
    target_object_id: target.target_object_id,
    target_revision: target.target_revision || null,
    note: note || null,
  }, '确认失败')
}

/** 驳回（终止处置；理由必填，服务端 400 兜底）。 */
async function dismissOntologyCase(kase: OntologyCase, note: string) {
  return postDecision(kase, 'dismiss', { note }, '驳回失败')
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
    isOntologyBusy: isBusy,
    loadOntology,
    confirmOntologyCase,
    dismissOntologyCase,
    searchOntologyObjects,
  }
}

export function __resetOntology() {
  cases.value = []
  coverage.value = null
  supported.value = null
  loadError.value = ''
  inflight.value = new Set()
  lastLoadedAt = 0
}
