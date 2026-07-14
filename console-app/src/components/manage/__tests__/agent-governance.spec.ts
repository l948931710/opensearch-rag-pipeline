import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import AgentGovernance from '@/components/manage/AgentGovernance.vue'
import {
  __resetAgentGovernance,
  useAgentGovernance,
  type AgentInvocationRow,
  type AgentToolRow,
} from '@/composables/useAgentGovernance'

// 批次β F1+F2：Agent 治理（kill switch + uncertain 对账）。契约固化：
//  · kb_admin 专属：非 kb_admin 不打网络、supported=false（tab 自隐）
//  · 404/403 → supported=false 静默；5xx → loadError 可重试
//  · toggle 按 tool_name 整体翻转（响应只回 {tool_name,status}→本地全版本翻转）
//  · resolve note 必填（前端拦 + 服务端 422 兜底）；409 → 强刷队列
//  · 渲染真值源=items[].status（disabled 数组不作渲染源）

const promptTextMock = vi.hoisted(() => vi.fn())
const confirmMock = vi.hoisted(() => vi.fn())
const noticeMock = vi.hoisted(() => vi.fn())
vi.mock('@/composables/useDialog', () => ({
  useDialog: () => ({ confirm: confirmMock, promptText: promptTextMock, notice: noticeMock }),
}))
const openRunCenterMock = vi.hoisted(() => vi.fn())
vi.mock('@/composables/useAgentAsk', () => ({
  useAgentAsk: () => ({ openRunCenter: openRunCenterMock }),
}))

beforeEach(() => {
  vi.restoreAllMocks(); vi.unstubAllGlobals()
  promptTextMock.mockReset(); confirmMock.mockReset(); noticeMock.mockReset(); openRunCenterMock.mockReset()
  __resetAgentGovernance()
})

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'admin1', name: '管理员', role: 'kb_admin', aclGroups: ['production'], canManage: true, managedOwnerDepts: ['production'], ...over }
}
function activate(id: Identity, token = 't') {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token, ready: true } } })
  setActivePinia(pinia)
  return pinia
}
function toolRow(over: Partial<AgentToolRow> = {}): AgentToolRow {
  return {
    tool_name: 'u8_writeback', version: '1.0', risk_level: 'high_write', permission_scope: 'approval',
    owner_team: 'erp', status: 'active', registered_by: 'system', created_at: '2026-07-05 09:00', ...over,
  }
}
function invRow(over: Partial<AgentInvocationRow> = {}): AgentInvocationRow {
  return {
    invocation_id: 'inv1', run_id: 'run_abc123', step_no: 3, tool_name: 'u8_writeback', status: 'uncertain',
    policy_decision: 'require_approval', approval_request_id: 'ap1', idempotency_key: 'k1', args_digest: 'a1b2c3d4',
    error_text: 'stale executing（进程崩溃/超时僵尸，900s 无收尾）', started_at: '2026-07-13 16:02', ended_at: null, ...over,
  }
}
function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}

describe('useAgentGovernance — 加载与端点探测', () => {
  it('200 → tools/drift/uncertain 填充 + supported=true（tab 出现判据）', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async (p: string) => {
      if (String(p).includes('/api/agent/tools')) return jsonResp({ items: [toolRow()], disabled: [], drift: ['spec 漂移（代码 ≠ DB）: x@1.0'] })
      return jsonResp({ items: [invRow()] })
    }))
    const g = useAgentGovernance()
    await g.loadAgentGovernance(true)
    expect(g.agentTools.value.length).toBe(1)
    expect(g.agentToolDrift.value.length).toBe(1)
    expect(g.agentUncertain.value.length).toBe(1)
    expect(g.agentGovSupported.value).toBe(true)
  })

  it('404（RAG_AGENT_ENABLE 未开）→ supported=false 静默不置错；403 同', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({}, { ok: false, status: 404 })))
    const g = useAgentGovernance()
    await g.loadAgentGovernance(true)
    expect(g.agentGovSupported.value).toBe(false)
    expect(g.agentGovError.value).toBe('')

    __resetAgentGovernance()
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({}, { ok: false, status: 403 })))
    const g2 = useAgentGovernance()
    await g2.loadAgentGovernance(true)
    expect(g2.agentGovSupported.value).toBe(false)
  })

  it('5xx → loadError 置错可重试（supported 不误翻）', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({}, { ok: false, status: 500 })))
    const g = useAgentGovernance()
    await g.loadAgentGovernance(true)
    expect(g.agentGovError.value).toContain('加载失败')
  })

  it('非 kb_admin → 不打网络、supported=false（dept_admin 也无入口）', async () => {
    activate(identity({ role: 'dept_admin' }))
    const spy = vi.fn()
    vi.stubGlobal('fetch', spy)
    const g = useAgentGovernance()
    await g.loadAgentGovernance(true)
    expect(spy).not.toHaveBeenCalled()
    expect(g.agentGovSupported.value).toBe(false)
  })

  it('响应缺 disabled/drift 键（异构 mock/降级网关）→ (x||[]) 兜底不崩', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ items: [] })))   // 无 disabled/drift 键
    const g = useAgentGovernance()
    await g.loadAgentGovernance(true)
    expect(g.agentToolDrift.value).toEqual([])
    expect(g.agentGovSupported.value).toBe(true)
  })
})

describe('useAgentGovernance — kill switch 与对账处置', () => {
  it('toggleTool → POST body {tool_name,disabled,reason}；成功后同名全部版本本地翻转', async () => {
    activate(identity())
    const calls: Array<[string, any]> = []
    vi.stubGlobal('fetch', vi.fn(async (p: string, init?: any) => {
      calls.push([String(p), init])
      return jsonResp({ tool_name: 'u8_writeback', status: 'disabled', rows: 2 })
    }))
    const g = useAgentGovernance()
    g.agentTools.value = [toolRow({ version: '1.0' }), toolRow({ version: '1.1' }), toolRow({ tool_name: 'knowledge_search', risk_level: 'read_only' })]
    const ok = await g.toggleTool('u8_writeback', true, '误触发风险，先全局停用')
    expect(ok).toBe(true)
    expect(calls[0][0]).toBe('/api/agent/tools/toggle')
    const body = JSON.parse(calls[0][1].body)
    expect(body).toEqual({ tool_name: 'u8_writeback', disabled: true, reason: '误触发风险，先全局停用' })
    // 同名两个版本一起翻转（服务端按 tool_name 整体生效），别的工具不动
    expect(g.agentTools.value.filter((t) => t.tool_name === 'u8_writeback').every((t) => t.status === 'disabled')).toBe(true)
    expect(g.agentTools.value.find((t) => t.tool_name === 'knowledge_search')?.status).toBe('active')
  })

  it('resolveInvocation 成功 → POST {invocation_id,resolution,note} + 本地移除该行', async () => {
    activate(identity())
    const calls: Array<[string, any]> = []
    vi.stubGlobal('fetch', vi.fn(async (p: string, init?: any) => {
      calls.push([String(p), init])
      return jsonResp({ invocation_id: 'inv1', status: 'succeeded' })
    }))
    const g = useAgentGovernance()
    g.agentUncertain.value = [invRow(), invRow({ invocation_id: 'inv2' })]
    const ok = await g.resolveInvocation(invRow(), 'confirmed_succeeded', '已到 U8 核实单据 20260713-001 已落库')
    expect(ok).toBe(true)
    const body = JSON.parse(calls[0][1].body)
    expect(body.resolution).toBe('confirmed_succeeded')
    expect(body.note).toContain('U8')
    expect(g.agentUncertain.value.map((x) => x.invocation_id)).toEqual(['inv2'])
  })

  it('resolve 409（已被处置/状态已变）→ 弹错并强刷队列', async () => {
    activate(identity())
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (p: string) => {
      calls.push(String(p))
      if (String(p).includes('/resolve')) return jsonResp({ detail: '该调用不在 uncertain 态' }, { ok: false, status: 409 })
      return jsonResp({ items: [], disabled: [], drift: [] })
    }))
    const g = useAgentGovernance()
    g.agentUncertain.value = [invRow()]
    const ok = await g.resolveInvocation(invRow(), 'confirmed_failed', '核实未落库')
    expect(ok).toBe(false)
    expect(noticeMock).toHaveBeenCalled()
    expect(calls.some((p) => p.includes('/api/agent/tools'))).toBe(true)   // 强刷触发重拉
  })
})

describe('AgentGovernance 组件', () => {
  it('supported=false → 「未开启/无权」诚实页（深链兜底），不渲染空态假象', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({}, { ok: false, status: 404 })))
    const g = useAgentGovernance()
    await g.loadAgentGovernance(true)
    const w = mount(AgentGovernance, { global: { plugins: [activate(identity())] } })
    expect(w.text()).toContain('Agent 治理未开启或无权访问')
    expect(w.text()).not.toContain('工具注册表')
  })

  it('有数据 → 工具分组行（risk/status pill + 版本聚合）+ 漂移条 + uncertain 行', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async (p: string) => {
      if (String(p).includes('/api/agent/tools')) return jsonResp({ items: [toolRow({ version: '1.0' }), toolRow({ version: '1.1' }), toolRow({ tool_name: 'ontology_resolve', status: 'disabled', risk_level: 'low_write' })], disabled: ['ontology_resolve'], drift: ['DB 有记录但代码未注册: legacy@0.9'] })
      return jsonResp({ items: [invRow()] })
    }))
    const g = useAgentGovernance()
    await g.loadAgentGovernance(true)
    const w = mount(AgentGovernance, { global: { plugins: [activate(identity())] } })
    expect(w.text()).toContain('u8_writeback')
    expect(w.text()).toContain('v1.0 / v1.1')            // 同名版本聚合为一行
    expect(w.text()).toContain('高风险写')
    expect(w.text()).toContain('已停用')                  // ontology_resolve 行
    expect(w.find('[data-testid="agent-gov-drift"]').text()).toContain('legacy@0.9')
    expect(w.text()).toContain('stale executing')         // uncertain 行错误文案
    expect(w.text()).toContain('核实为成功')
  })

  it('停用走必填理由 promptText；取消 → 零请求', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async (p: string) => {
      if (String(p).includes('/api/agent/tools')) return jsonResp({ items: [toolRow()], disabled: [], drift: [] })
      return jsonResp({ items: [] })
    }))
    const g = useAgentGovernance()
    await g.loadAgentGovernance(true)
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    promptTextMock.mockResolvedValue(null)   // 取消
    const w = mount(AgentGovernance, { global: { plugins: [activate(identity())] } })
    await w.find('[data-testid="tool-toggle-u8_writeback"]').trigger('click')
    await vi.waitFor(() => expect(promptTextMock).toHaveBeenCalled())
    expect(String(promptTextMock.mock.calls[0][0].message)).toContain('u8_writeback')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('对账空 note → 前端拦截（notice）不发请求', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async (p: string) => {
      if (String(p).includes('/api/agent/tools')) return jsonResp({ items: [], disabled: [], drift: [] })
      return jsonResp({ items: [invRow()] })
    }))
    const g = useAgentGovernance()
    await g.loadAgentGovernance(true)
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    promptTextMock.mockResolvedValue('   ')   // 空白 note
    const w = mount(AgentGovernance, { global: { plugins: [activate(identity())] } })
    const btn = w.findAll('button').find((b) => b.text().includes('核实为成功'))!
    await btn.trigger('click')
    await vi.waitFor(() => expect(noticeMock).toHaveBeenCalled())
    expect(String(noticeMock.mock.calls[0][0].title)).toContain('核实依据')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('「查看运行」→ openRunCenter(run_id)（uncertain 行无原始参数，详情在运行时间线）', async () => {
    activate(identity())
    vi.stubGlobal('fetch', vi.fn(async (p: string) => {
      if (String(p).includes('/api/agent/tools')) return jsonResp({ items: [], disabled: [], drift: [] })
      return jsonResp({ items: [invRow()] })
    }))
    const g = useAgentGovernance()
    await g.loadAgentGovernance(true)
    const w = mount(AgentGovernance, { global: { plugins: [activate(identity())] } })
    const btn = w.findAll('button').find((b) => b.text().includes('查看运行'))!
    await btn.trigger('click')
    expect(openRunCenterMock).toHaveBeenCalledWith('run_abc123')
  })
})
