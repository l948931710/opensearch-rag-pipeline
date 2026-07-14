import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import OntologyWorkbench from '@/components/manage/OntologyWorkbench.vue'
import {
  __resetOntology,
  useOntology,
  type OntologyCase,
} from '@/composables/useOntology'

beforeEach(() => { vi.restoreAllMocks(); __resetOntology() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'admin1', name: '管理员', role: 'dept_admin', aclGroups: ['pmc'], canManage: true, managedOwnerDepts: ['pmc'], ...over }
}
function activate(id: Identity, token = 't') {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token, ready: true } } })
  setActivePinia(pinia)
  return pinia
}
function okase(over: Partial<OntologyCase> = {}): OntologyCase {
  return {
    case_id: 'oc1', namespace: 'u8', raw_value: 'ABC123-M', norm_value: 'ABC123-M',
    object_type_hint: 'product', status: 'open', seen_count: 4,
    first_seen_at: '2026-07-09 10:00:00', last_seen_at: '2026-07-10 09:12:00',
    evidence_json: null, steward_dept: 'pmc',
    candidates: [{
      candidate_id: 'cd1', case_id: 'oc1', target_object_id: 'o1', target_revision: null,
      method: 'rule', confidence: 0.85, features_json: null, canonical_ref: 'FLP-P-000123',
      title: '6.2口径龙虾杯', object_type: 'product', target_status: 'active',
    }],
    ...over,
  }
}
const COVERAGE = {
  active_identifiers: 10, auto_active: 4, open_cases: 3, resolved_cases: 6,
  dismissed_cases: 1, resolution_coverage: 0.77, manual_review_rate: 0.6,
}

describe('useOntology — 加载与端点探测', () => {
  it('200 → cases+coverage 填充 + supported=true（tab 出现的判据）', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (String(path).startsWith('/api/ontology/workbench')) {
        return { ok: true, status: 200, json: async () => ({ items: [okase()] }) }
      }
      return { ok: true, status: 200, json: async () => COVERAGE }
    }))
    const o = useOntology()
    await o.loadOntology(true)
    expect(o.ontologyCases.value.length).toBe(1)
    expect(o.ontologySupported.value).toBe(true)
    expect(o.ontologyBacklogCount.value).toBe(1)
    // coverage 是 fire-and-forget 后随：等它收敛
    await vi.waitFor(() => expect(o.ontologyCoverage.value?.active_identifiers).toBe(10))
  })

  it('404（RAG_ONTOLOGY_ENABLE 未开）→ supported=false 静默、不置错（tab 自隐）', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) })))
    const o = useOntology()
    await o.loadOntology(true)
    expect(o.ontologySupported.value).toBe(false)
    expect(o.ontologyError.value).toBe('')
  })

  it('403（员工/无权）→ 自隐；5xx → 置错可重试', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 403, json: async () => ({}) })))
    const o = useOntology()
    await o.loadOntology(true)
    expect(o.ontologySupported.value).toBe(false)

    __resetOntology()
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })))
    const b = useOntology()
    await b.loadOntology(true)
    expect(b.ontologyError.value).toContain('加载失败')
  })

  it('coverage 失败不影响队列与 tab（装饰面 best-effort）', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (String(path).startsWith('/api/ontology/workbench')) {
        return { ok: true, status: 200, json: async () => ({ items: [okase()] }) }
      }
      return { ok: false, status: 500, json: async () => ({}) }
    }))
    const o = useOntology()
    await o.loadOntology(true)
    await new Promise((r) => setTimeout(r, 0))           // 让 coverage 请求失败并被吞掉
    expect(o.ontologySupported.value).toBe(true)
    expect(o.ontologyError.value).toBe('')
    expect(o.ontologyCoverage.value).toBeNull()
  })

  it('canManage=false → 不打网络（员工不拉队列）', async () => {
    activate(identity({ canManage: false, role: 'employee' }))
    const spy = vi.fn()
    vi.stubGlobal('fetch', spy)
    await useOntology().loadOntology(true)
    expect(spy).not.toHaveBeenCalled()
  })
})

describe('useOntology — 处置提交', () => {
  it('确认 → POST confirm 带 target/note；2xx 本地移除 + 刷覆盖率', async () => {
    activate(identity())
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: any) => {
      calls.push([String(path), init])
      if (String(path).includes('/confirm')) return { ok: true, status: 200, json: async () => ({}) }
      return { ok: true, status: 200, json: async () => COVERAGE }
    }))
    const o = useOntology()
    o.ontologyCases.value = [okase(), okase({ case_id: 'oc2', raw_value: 'X9' })]
    const done = await o.confirmOntologyCase(okase(), { target_object_id: 'o1', target_revision: 'r2' }, '打样一致')
    expect(done).toBe(true)
    const post = calls.find(([p]) => p.includes('/confirm'))
    expect(post[0]).toBe('/api/ontology/cases/oc1/confirm')
    const body = JSON.parse(post[1].body)
    expect(body).toEqual({ target_object_id: 'o1', target_revision: 'r2', note: '打样一致' })
    expect(o.ontologyCases.value.map((x) => x.case_id)).toEqual(['oc2'])
    expect(calls.some(([p]) => p.startsWith('/api/ontology/coverage'))).toBe(true)
  })

  it('409（并发被抢）→ 弹错 + 强刷队列，item 不本地移除', async () => {
    activate(identity())
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: any) => {
      calls.push([String(path), init])
      if (String(path).includes('/confirm')) {
        return { ok: false, status: 409, json: async () => ({ detail: '已被并发处置' }) }
      }
      return { ok: true, status: 200, json: async () => ({ items: [okase()] }) }
    }))
    const o = useOntology()
    o.ontologyCases.value = [okase()]
    const done = await o.confirmOntologyCase(okase(), { target_object_id: 'o1' }, '并发验证')
    expect(done).toBe(false)
    expect(calls.some(([p]) => p.startsWith('/api/ontology/workbench'))).toBe(true)   // 强刷
  })

  it('驳回 → POST dismiss 带 note；2xx 本地移除', async () => {
    activate(identity())
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: any) => {
      calls.push([String(path), init])
      if (String(path).includes('/dismiss')) return { ok: true, status: 200, json: async () => ({}) }
      return { ok: true, status: 200, json: async () => COVERAGE }
    }))
    const o = useOntology()
    o.ontologyCases.value = [okase()]
    await o.dismissOntologyCase(okase(), '废弃编号')
    const post = calls.find(([p]) => p.includes('/dismiss'))
    expect(JSON.parse(post[1].body)).toEqual({ note: '废弃编号' })
    expect(o.ontologyCases.value.length).toBe(0)
  })

  it('对象搜索 → GET /api/ontology/objects 带 type/q；失败回空数组', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      expect(String(path)).toContain('object_type=product')
      expect(String(path)).toContain('q=%E9%BE%99%E8%99%BE')      // 龙虾 urlencoded
      return { ok: true, status: 200, json: async () => ({ items: [{ object_id: 'o1', object_type: 'product', canonical_ref: 'FLP-P-000001', title: '龙虾杯', status: 'active' }] }) }
    }))
    const hits = await useOntology().searchOntologyObjects('product', '龙虾')
    expect(hits.length).toBe(1)

    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('net') }))
    expect(await useOntology().searchOntologyObjects('product', 'x')).toEqual([])
  })
})

describe('OntologyWorkbench 组件', () => {
  function mountBench(items: OntologyCase[], id = identity()) {
    const pinia = activate(id)
    useOntology().ontologyCases.value = items
    return mount(OntologyWorkbench, { global: { plugins: [pinia] } })
  }

  it('空 → 显式空态（独立 tab 不自隐白屏）', () => {
    const w = mountBench([])
    expect(w.text()).toContain('当前没有待消解的编号')
  })

  it('有数据 → 编号/命名空间/观测次数/候选与置信 + 三动作按钮', () => {
    const w = mountBench([okase()])
    expect(w.text()).toContain('ABC123-M')
    expect(w.text()).toContain('u8')
    expect(w.text()).toContain('×4')
    expect(w.text()).toContain('6.2口径龙虾杯')
    expect(w.text()).toContain('rule · 85%')
    expect(w.text()).toContain('确认此候选')
    expect(w.text()).toContain('手动指定')
    expect(w.text()).toContain('驳回')
  })

  it('候选目标已退役 → 确认按钮禁用并标注', () => {
    const k = okase()
    k.candidates[0].target_status = 'retired'
    const w = mountBench([k])
    expect(w.text()).toContain('对象已retired')
    const btn = w.findAll('button').find((b) => b.text().includes('确认此候选'))!
    expect(btn.attributes('disabled')).toBeDefined()
  })

  it('覆盖率卡片渲染四指标', async () => {
    activate(identity())
    const o = useOntology()
    o.ontologyCases.value = [okase()]
    o.ontologyCoverage.value = { ...COVERAGE }
    const w = mount(OntologyWorkbench, { global: { plugins: [createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: identity(), token: 't', ready: true } } })] } })
    expect(w.text()).toContain('已确认别名')
    expect(w.text()).toContain('77%')
    expect(w.text()).toContain('待处置积压')
    expect(w.text()).toContain('60%')
  })
})

describe('PR-F HITL 契约（显式点选 / 受限候选 / 分母口径 / 证据展示）', () => {
  function mountBench(items: OntologyCase[], id = identity()) {
    const pinia = activate(id)
    useOntology().ontologyCases.value = items
    return mount(OntologyWorkbench, { global: { plugins: [pinia] } })
  }

  it('跨部门不可读候选 → 受限占位 + 确认按钮禁用（零字段泄露）', () => {
    const k = okase()
    k.candidates[0] = {
      ...k.candidates[0], target_visible: false,
      canonical_ref: null, title: null, object_type: null, target_status: null,
    }
    const w = mountBench([k])
    expect(w.text()).toContain('[受限对象]')
    expect(w.text()).not.toContain('FLP-P-000123')
    const btn = w.findAll('button').find((b) => b.text().includes('确认此候选'))!
    expect(btn.attributes('disabled')).toBeDefined()
  })

  it('候选行展示目标归属部门与非 internal 密级（处置人看得见再确认）', () => {
    const k = okase()
    k.candidates[0] = { ...k.candidates[0], target_visible: true, owner_dept: 'pmc', data_classification: 'confidential' }
    const w = mountBench([k])
    expect(w.text()).toContain('confidential')
  })

  it('evidence_json → 可展开证据快照（pretty JSON）', () => {
    const k = okase({ evidence_json: '{"source":"u8_export","row":42}' })
    const w = mountBench([k])
    expect(w.text()).toContain('证据快照')
    expect(w.text()).toContain('u8_export')
  })

  it('coverage 分母口径：population → 展示源快照分母；approx → 明示近似', async () => {
    activate(identity())
    const o = useOntology()
    o.ontologyCases.value = [okase()]
    o.ontologyCoverage.value = { ...COVERAGE, denominator: 'population', population_records: 500 }
    const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: identity(), token: 't', ready: true } } })
    let w = mount(OntologyWorkbench, { global: { plugins: [pinia] } })
    expect(w.text()).toContain('源快照 500')
    o.ontologyCoverage.value = { ...COVERAGE, denominator: 'approx', population_records: null }
    w = mount(OntologyWorkbench, { global: { plugins: [pinia] } })
    expect(w.text()).toContain('近似口径')
  })

  it('手动指定搜索结果内联渲染，点选目标（绝不自动取第一条）', async () => {
    const k = okase()
    const w = mountBench([k])
    // 直接驱动组件内部状态：两条搜索结果 → 都渲染成可点按钮
    ;(w.vm as any).manualHits = { oc1: [
      { object_id: 'o1', object_type: 'product', canonical_ref: 'FLP-P-000001', title: '龙虾杯A', status: 'active' },
      { object_id: 'o2', object_type: 'product', canonical_ref: 'FLP-P-000002', title: '龙虾杯B', status: 'active' },
    ] }
    await w.vm.$nextTick()
    expect(w.text()).toContain('搜索结果 2 条')
    expect(w.text()).toContain('不会自动选第一条')
    expect(w.text()).toContain('龙虾杯A')
    expect(w.text()).toContain('龙虾杯B')
    const rows = w.findAll('button').filter((b) => b.text().includes('龙虾杯'))
    expect(rows.length).toBe(2)
  })
})

describe('PR-I P2（身份切换重置 / 批量驳回）', () => {
  it('身份切换 → singleton 状态整体重置（不残留上个管理员的队列/覆盖率/supported）', async () => {
    activate(identity({ userId: 'admin1' }))
    const o = useOntology()
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (String(path).startsWith('/api/ontology/workbench')) {
        return { ok: true, status: 200, json: async () => ({ items: [okase()] }) }
      }
      return { ok: true, status: 200, json: async () => COVERAGE }
    }))
    await o.loadOntology(true)
    expect(o.ontologyCases.value.length).toBe(1)
    expect(o.ontologySupported.value).toBe(true)
    // 换成另一个无管理权身份：全部清空，supported=false（tab 不显）
    activate(identity({ userId: 'emp9', role: 'employee', canManage: false, managedOwnerDepts: [] }))
    await o.loadOntology(true)
    expect(o.ontologyCases.value).toEqual([])
    expect(o.ontologyCoverage.value).toBeNull()
    expect(o.ontologySupported.value).toBe(false)
  })

  it('批量驳回 → POST batch-dismiss；成功条目本地移除，部分失败强刷', async () => {
    activate(identity())
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: any) => {
      calls.push([String(path), init])
      if (String(path).includes('/batch-dismiss')) {
        return { ok: true, status: 200, json: async () => ({
          results: [
            { case_id: 'oc1', status: 'dismissed' },
            { case_id: 'oc2', status: 'conflict' },
          ], dismissed: 1,
        }) }
      }
      if (String(path).startsWith('/api/ontology/workbench')) {
        return { ok: true, status: 200, json: async () => ({ items: [okase({ case_id: 'oc2' })] }) }
      }
      return { ok: true, status: 200, json: async () => COVERAGE }
    }))
    const o = useOntology()
    o.ontologyCases.value = [okase(), okase({ case_id: 'oc2', raw_value: 'X9' })]
    const n = await o.batchDismissOntologyCases(['oc1', 'oc2'], '废弃批次')
    expect(n).toBe(1)
    const post = calls.find(([p]) => p.includes('/batch-dismiss'))
    expect(JSON.parse(post[1].body)).toEqual({ case_ids: ['oc1', 'oc2'], note: '废弃批次' })
    expect(o.ontologyCases.value.map((x) => x.case_id)).not.toContain('oc1')
    expect(calls.some(([p]) => p.startsWith('/api/ontology/workbench'))).toBe(true)  // 部分失败强刷
  })

  it('勾选集渲染批量驳回按钮（默认不显）', async () => {
    const pinia = activate(identity())
    useOntology().ontologyCases.value = [okase(), okase({ case_id: 'oc2', raw_value: 'X9' })]
    const w = mount(OntologyWorkbench, { global: { plugins: [pinia] } })
    expect(w.text()).not.toContain('批量驳回')
    const boxes = w.findAll('input[type="checkbox"]')
    expect(boxes.length).toBe(2)
    await boxes[0].setValue(true)
    await boxes[1].setValue(true)
    expect(w.text()).toContain('批量驳回（2）')
  })
})

// ── 批次δ F4：keyset 游标分页 + object_type 筛选 + 勾选交集 ────────────────────────
describe('批次δ — 翻页与筛选', () => {
  function stubPaged() {
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      const p = String(path)
      calls.push(p)
      if (p.startsWith('/api/ontology/workbench')) {
        const u = new URL('http://x' + p)
        if (u.searchParams.get('object_type') === 'mold') {
          return { ok: true, status: 200, json: async () => ({ items: [okase({ case_id: 'm1', raw_value: 'MLD-88A', object_type_hint: 'mold' })], next_cursor: null }) }
        }
        if (u.searchParams.get('cursor') === 'CUR1') {
          return { ok: true, status: 200, json: async () => ({ items: [okase({ case_id: 'oc3', raw_value: 'C-3' })], next_cursor: null }) }
        }
        return { ok: true, status: 200, json: async () => ({ items: [okase(), okase({ case_id: 'oc2', raw_value: 'B-2' })], next_cursor: 'CUR1' }) }
      }
      return { ok: true, status: 200, json: async () => COVERAGE }
    }))
    return calls
  }

  it('首页满页带 next_cursor → hasMore；loadMore 携游标【追加】并收尾（到底后 hasMore=false）', async () => {
    activate(identity())
    const calls = stubPaged()
    const o = useOntology()
    await o.loadOntology(true)
    expect(o.ontologyCases.value.map((c) => c.case_id)).toEqual(['oc1', 'oc2'])
    expect(o.ontologyHasMore.value).toBe(true)
    const n = await o.loadMoreOntology()
    expect(n).toBe(1)
    expect(o.ontologyCases.value.map((c) => c.case_id)).toEqual(['oc1', 'oc2', 'oc3'])   // 追加不替换
    expect(o.ontologyHasMore.value).toBe(false)
    expect(calls.some((p) => p.includes('cursor=CUR1'))).toBe(true)                       // keyset 游标而非 offset
    expect(calls.every((p) => !p.includes('offset='))).toBe(true)
  })

  it('筛选变更【强制】重拉第一页（30s staleness 门内也发请求）且游标归零、带 object_type 参数', async () => {
    activate(identity())
    const calls = stubPaged()
    const o = useOntology()
    await o.loadOntology(true)                       // 刚加载（lastLoadedAt=now，30s 门关着）
    const before = calls.filter((p) => p.includes('/workbench')).length
    await o.setOntologyObjectTypeFilter('mold')      // 若不 force 会被门吞掉——这是审计风险 1
    const wb = calls.filter((p) => p.includes('/workbench'))
    expect(wb.length).toBe(before + 1)
    expect(wb[wb.length - 1]).toContain('object_type=mold')   // coverage 是 fire-and-forget 后随，不取 calls 末位
    expect(o.ontologyCases.value.map((c) => c.case_id)).toEqual(['m1'])   // 替换而非追加
    expect(o.ontologyHasMore.value).toBe(false)
  })

  it('勾选交集：强刷替换整列后，角标只计仍在列的勾选（幽灵 id 不显不提交）', async () => {
    const pinia = activate(identity())
    const o = useOntology()
    o.ontologyCases.value = [okase(), okase({ case_id: 'oc2', raw_value: 'B-2' })]
    const w = mount(OntologyWorkbench, { global: { plugins: [pinia] } })
    const boxes = w.findAll('input[type="checkbox"]')
    await boxes[0].setValue(true)
    await boxes[1].setValue(true)
    expect(w.text()).toContain('批量驳回（2）')
    o.ontologyCases.value = [okase({ case_id: 'oc2', raw_value: 'B-2' })]   // 模拟 409 强刷替换
    await w.vm.$nextTick()
    expect(w.text()).toContain('批量驳回（1）')                              // oc1 成幽灵 → 不计
    expect(w.text()).not.toContain('批量驳回（2）')
  })

  it('空列表但仍有游标（dept_admin 首页被管辖精判滤空）→ 空态给「继续加载」而非装到底', async () => {
    const pinia = activate(identity())
    const o = useOntology()
    o.ontologySupported.value = true
    // hasMore 是 computed 只读——走真实 loadOntology 路径置位（首页空 items + 有游标）
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (String(path).startsWith('/api/ontology/workbench')) {
        return { ok: true, status: 200, json: async () => ({ items: [], next_cursor: 'CUR9' }) }
      }
      return { ok: true, status: 200, json: async () => COVERAGE }
    }))
    await o.loadOntology(true)
    const w = mount(OntologyWorkbench, { global: { plugins: [pinia] } })
    expect(w.find('[data-testid="onto-load-more"]').exists()).toBe(true)
    expect(w.text()).toContain('继续加载')
  })

  it('筛选 pill 不引入 checkbox、静态区不出现「批量驳回」字样（守既有单测契约）', async () => {
    const pinia = activate(identity())
    const o = useOntology()
    o.ontologyCases.value = [okase()]
    const w = mount(OntologyWorkbench, { global: { plugins: [pinia] } })
    expect(w.find('[data-testid="onto-type-filter"]').exists()).toBe(true)
    expect(w.findAll('[data-testid="onto-type-filter"] input').length).toBe(0)
    expect(w.findAll('input[type="checkbox"]').length).toBe(1)   // 仍=case 数
    expect(w.text()).not.toContain('批量驳回')                    // 未勾选不出现
  })
})
