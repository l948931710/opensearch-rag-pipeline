import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import { reactive } from 'vue'
import type { Identity } from '@/stores/session'
import { useAsk, __resetAsk, type ChatMessage } from '@/composables/useAsk'
import { useAgentAsk, __resetAgentAsk } from '@/composables/useAgentAsk'
import { useDialog } from '@/composables/useDialog'
import AgentFlow from '@/components/qa/AgentFlow.vue'
import AgentRunCard from '@/components/qa/AgentRunCard.vue'

// Agent canary 消息级 UI：工具 chip 结局收敛（P0-F tool_result 四态）、挂起卡（等待审批人处置 +
// 撤回 + 指路审批 tab）、终态回执（uncertain 显式「结果不确定，待对账」+ failed 下一步提示）。

beforeEach(() => {
  vi.restoreAllMocks()
  __resetAgentAsk()
  localStorage.clear()
  // 默认兜底 fetch：挂起卡 onMounted 即起轮询（立即拉一次详情），不 stub 会打真网络（噪声 ECONNREFUSED）
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, text: async () => '',
    json: async () => ({ run: { run_id: 'r1', status: 'suspended', user_id: 'user_a' }, steps: [], invocations: [], approval: null }),
  })))
})
// 尾清：挂起卡 onMounted 会起真实轮询表——测试结束即撤，防 mock 退场后裸 fetch
afterEach(() => { __resetAgentAsk() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'user_a', name: '员工A', role: 'employee', aclGroups: ['marketing'], canManage: false, managedOwnerDepts: [], ...over }
}
function activate(id: Identity) {
  const pinia = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 't', ready: true } } })
  setActivePinia(pinia)
  return pinia
}
const STUBS = { RouterLink: { template: '<a class="rl-stub"><slot /></a>' } }

function agentMsg(over: Record<string, unknown> = {}): ChatMessage {
  return reactive({
    id: 'a1', role: 'ai', question: 'q', raw: '', html: '', streaming: false,
    agent: { stages: [], tools: [], approval: null },
    ...over,
  }) as ChatMessage
}

describe('AgentFlow — 阶段条 + 工具 chip 结局收敛', () => {
  it('四种 tool_result 结局的 chip 文案（完成含耗时/失败/被策略拒绝/等待审批）', () => {
    activate(identity())
    const m = agentMsg({
      agent: {
        stages: [
          { key: 'submitted', label: '已提交', at: 1000 },
          { key: 'tool', label: '调用工具', at: 2000 },
          { key: 'done', label: '完成', at: 3000 },
        ],
        tools: [
          { callId: 'c1', toolName: 'knowledge_search', status: 'succeeded', elapsedMs: 1200 },
          { callId: 'c2', toolName: 'u8_writeback', status: 'failed' },
          { callId: 'c3', toolName: 'u8_writeback', status: 'denied' },
          { callId: 'c4', toolName: 'u8_writeback', status: 'pending_approval' },
        ],
        approval: null,
      },
    })
    const w = mount(AgentFlow, { props: { message: m }, global: { plugins: [activate(identity())], stubs: STUBS } })
    const text = w.text()
    expect(text).toContain('检索知识库 完成（1.2s）')   // succeeded + 耗时
    expect(text).toContain('U8 写回 失败')             // failed
    expect(text).toContain('U8 写回 被策略拒绝')        // denied
    expect(text).toContain('U8 写回 等待审批')          // pending_approval
    // 阶段条真实事件 + 已过阶段耗时
    expect(text).toContain('已提交')
    expect(text).toContain('调用工具')
    expect(w.findAll('[data-testid="agent-tool-chip"]')).toHaveLength(4)
  })

  it('进行中工具（无 tool_result）显示省略号态', () => {
    activate(identity())
    const m = agentMsg({
      streaming: true,
      agent: { stages: [{ key: 'tool', label: '调用工具', at: Date.now() }], tools: [{ callId: 'c1', toolName: 'knowledge_search', status: 'proposed' }], approval: null },
    })
    const w = mount(AgentFlow, { props: { message: m }, global: { plugins: [activate(identity())], stubs: STUBS } })
    expect(w.text()).toContain('检索知识库…')
    w.unmount()   // 心跳计时器随卸载清理
  })
})

describe('AgentRunCard — 挂起卡（等待审批人处置 + 撤回 + 审批指路）', () => {
  function suspendedMsg() {
    return agentMsg({
      agent: {
        runId: 'r1', status: 'suspended', stages: [], tools: [],
        approval: { requestId: 'ap9', toolName: 'u8_writeback', args: { qty: 120, item: 'PP 刀叉' } },
      },
    })
  }

  it('展示工具名/脱敏参数/审批单号/等待处置文案；员工不显「去审批」链接', () => {
    const pinia = activate(identity())
    const w = mount(AgentRunCard, { props: { message: suspendedMsg() }, global: { plugins: [pinia], stubs: STUBS } })
    const text = w.text()
    expect(text).toContain('需要审批才能继续')
    expect(text).toContain('u8_writeback')
    expect(text).toContain('qty=120')
    expect(text).toContain('审批单 ap9')
    expect(text).toContain('等待审批人处置')
    expect(text).toContain('撤回申请')
    expect(w.find('.rl-stub').exists()).toBe(false)   // employee 无审批面，不指路死入口
  })

  it('canManage 身份 → 「去审批」指路渲染（/manage?tab=approvals）', () => {
    const pinia = activate(identity({ role: 'dept_admin', canManage: true, managedOwnerDepts: ['marketing'] }))
    const w = mount(AgentRunCard, { props: { message: suspendedMsg() }, global: { plugins: [pinia], stubs: STUBS } })
    expect(w.find('.rl-stub').exists()).toBe(true)
    expect(w.text()).toContain('去「审批」处理')
  })

  it('撤回：确认后 POST /api/agent/approve rejected_terminate', async () => {
    const pinia = activate(identity())
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: any) => {
      calls.push([String(path), init])
      if (String(path) === '/api/agent/approve') return { ok: true, status: 200, body: { cancel: vi.fn() } }
      return { ok: true, status: 200, json: async () => ({ run: { run_id: 'r1', status: 'cancelled', user_id: 'user_a' }, steps: [], invocations: [], approval: null }) }
    }))
    const w = mount(AgentRunCard, { props: { message: suspendedMsg() }, global: { plugins: [pinia], stubs: STUBS } })
    await w.find('[data-testid="agent-withdraw"]').trigger('click')
    const dlg = useDialog()
    expect(dlg.dialog.value.open).toBe(true)          // 撤回是终止性操作 → 二次确认
    dlg.onConfirm()
    await vi.waitFor(() => expect(calls.some(([p]) => p === '/api/agent/approve')).toBe(true))
    const body = JSON.parse(calls.find(([p]) => p === '/api/agent/approve')![1].body)
    expect(body.run_id).toBe('r1')
    expect(body.outcome).toEqual({ kind: 'rejected_terminate' })
    expect(body.idempotency_key).toBe('ap9:rejected_terminate')
  })
})

describe('AgentRunCard — 终态回执（批准≠成功）', () => {
  it('succeeded + uncertain 回执 → 显式「结果不确定 + 人工对账 + 勿重复发起」', () => {
    const pinia = activate(identity())
    const { agentRunDetails } = useAgentAsk()
    agentRunDetails.value = {
      r2: {
        run: { run_id: 'r2', status: 'succeeded', user_id: 'user_a' },
        steps: [],
        invocations: [
          { invocation_id: 'i1', tool_name: 'u8_writeback', status: 'succeeded' },
          { invocation_id: 'i2', tool_name: 'u8_writeback', status: 'uncertain' },
        ],
        approval: null,
      },
    }
    const m = agentMsg({ agent: { runId: 'r2', status: 'succeeded', stages: [], tools: [], approval: { requestId: 'apX', toolName: 'u8_writeback', args: null } } })
    const w = mount(AgentRunCard, { props: { message: m }, global: { plugins: [pinia], stubs: STUBS } })
    const text = w.text()
    expect(text).toContain('审批已通过，运行完成')
    expect(text).toContain('不确定 1')
    const un = w.find('[data-testid="agent-uncertain"]')
    expect(un.exists()).toBe(true)
    expect(un.text()).toContain('结果不确定')
    expect(un.text()).toContain('人工对账')
    expect(un.text()).toContain('请勿重复发起相同操作')
  })

  it('failed → 给下一步提示而非裸状态码；expired/cancelled 如实说明未执行', () => {
    const pinia = activate(identity())
    const mk = (status: string) => mount(AgentRunCard, {
      props: { message: agentMsg({ agent: { runId: 'r3', status, stages: [], tools: [], approval: null } }) },
      global: { plugins: [pinia], stubs: STUBS },
    })
    expect(mk('failed').text()).toContain('可调整问法重新提问')
    expect(mk('expired').text()).toContain('超时视同拒绝')
    expect(mk('cancelled').text()).toContain('未执行')
  })

  it('纯文本 agent 回答（无 runId 无审批）→ 卡片不渲染（不加噪声）', () => {
    const pinia = activate(identity())
    const w = mount(AgentRunCard, { props: { message: agentMsg() }, global: { plugins: [pinia], stubs: STUBS } })
    expect(w.find('[data-testid="agent-run-card"]').exists()).toBe(false)
  })
})

describe('Composer — 深度思考 × Agent 模式并存（后端映射模型档，不再互斥）', () => {
  it('agentMode 开着时 think-toggle 仍渲染、可切换（回归：曾 v-if="!agentMode" 隐藏）', async () => {
    const Composer = (await import('@/components/qa/Composer.vue')).default
    const w = mount(Composer, {
      props: { modelValue: '', asking: false, hasMessages: false, thinking: false, agentAvailable: true, agentMode: true },
      global: { stubs: STUBS },
    })
    const think = w.find('[data-testid="think-toggle"]')
    expect(think.exists()).toBe(true)
    expect(w.find('[data-testid="agent-toggle"]').exists()).toBe(true)   // 两开关并存
    await think.trigger('click')
    expect(w.emitted('toggle-thinking')).toBeTruthy()
  })
})

// ── 批次β F3：运行中心降噪（会话标题行/回到会话/详情失败态）────────────────────────
describe('AgentRunCenter — 列表降噪 + 回会话 + 详情失败态（批次β F3）', () => {
  async function mountCenter(setup: (a: ReturnType<typeof useAgentAsk>, ask: ReturnType<typeof useAsk>) => void) {
    const pinia = activate(identity())
    const AgentRunCenter = (await import('@/components/qa/AgentRunCenter.vue')).default
    const a = useAgentAsk()
    const ask = useAsk()
    a.runCenterOpen.value = true
    setup(a, ask)
    return mount(AgentRunCenter, { global: { plugins: [pinia], stubs: STUBS } })
  }
  afterEach(() => { __resetAsk() })   // 本组种了会话，别泄给其它组

  it('列表行：conversation_id 命中本地会话 → 显示会话标题；未命中 → 回退 run_id 哈希', async () => {
    const w = await mountCenter((a, ask) => {
      ask.conversations.value = [{ id: 'c1', title: '如何申请生产环境的访问密钥？', messages: [], qaSession: '', updatedAt: 0 }]
      a.agentRuns.value = [
        { run_id: 'run_aaaaaaaaaaaaaaaa', status: 'succeeded', conversation_id: 'c1', agent_profile: 'default', started_at: '2026-07-11 09:00:00' },
        { run_id: 'run_bbbbbbbbbbbbbbbb', status: 'failed', conversation_id: 'zz_gone', agent_profile: 'default', started_at: '2026-07-10 08:00:00' },
      ] as never
    })
    const titles = w.findAll('[data-testid="run-row-title"]').map((n) => n.text())
    expect(titles[0]).toBe('如何申请生产环境的访问密钥？')
    expect(titles[1]).toContain('run_bbbbbbbb')   // 哈希回退（截断）
  })

  it('详情：回到会话按钮 → switchTo 该会话并收起抽屉', async () => {
    const w = await mountCenter((a, ask) => {
      ask.conversations.value = [{ id: 'c1', title: '密钥申请', messages: [], qaSession: '', updatedAt: 0 }]
      a.runCenterRunId.value = 'r1'
      a.agentRunDetails.value = { r1: { run: { run_id: 'r1', status: 'succeeded', conversation_id: 'c1' }, steps: [], invocations: [], approval: null } } as never
    })
    const btn = w.find('[data-testid="run-goto-conversation"]')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toContain('密钥申请')
    await btn.trigger('click')
    expect(useAsk().activeId.value).toBe('c1')
    expect(useAgentAsk().runCenterOpen.value).toBe(false)
  })

  it('回会话降级负例：conversation_id 不在本地会话 store → 按钮不渲染（诚实降级）', async () => {
    const w = await mountCenter((a, ask) => {
      ask.conversations.value = []   // 本地无该会话（他人 run / 已删 / 换设备）
      a.runCenterRunId.value = 'r1'
      a.agentRunDetails.value = { r1: { run: { run_id: 'r1', status: 'succeeded', conversation_id: 'c_gone' }, steps: [], invocations: [], approval: null } } as never
    })
    expect(w.find('[data-testid="run-goto-conversation"]').exists()).toBe(false)
  })

  it('详情失败态：detailError 置位 → 错误卡 + 重试按钮（不再永久转圈）', async () => {
    const w = await mountCenter((a) => {
      a.runCenterRunId.value = 'r9'
      a.agentRunDetailErrors.value = { r9: '运行详情拉取失败，将自动重试；也可点右上角刷新。' }
    })
    const err = w.find('[data-testid="run-detail-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('拉取失败')
    expect(err.text()).toContain('重试')
    expect(w.text()).not.toContain('正在加载运行详情')
  })
})
