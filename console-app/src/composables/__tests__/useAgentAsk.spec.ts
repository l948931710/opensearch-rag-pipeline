import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSession, type Role } from '@/stores/session'
import { useAsk, __resetAsk } from '@/composables/useAsk'
import { useAgentAsk, __agentAskTestkit, __resetAgentAsk } from '@/composables/useAgentAsk'
import { syncIdentityScope } from '@/composables/identityScope'

// Agent 用户侧 canary（P0-F）回归防线：
//  · transport 帧解析（session/chunk/tool_call/tool_result/approval/done/error/[DONE]）
//  · 能力探测（404→supported=false，绝不留死入口）+ /api/agent/ask 中途 404 自动回退旧路径
//  · kill switch localStorage uid 戳（换人不复活）+ identityScope 身份切换清空
//  · 断线恢复：approval 后按 run_id 5s 轮询，终态停；「批准≠成功」回执可见
//  · 撤回（发起人自撤 rejected_terminate）

const enc = new TextEncoder()
const frame = (o: unknown) => enc.encode('data: ' + JSON.stringify(o) + '\n\n')
const DONE = enc.encode('data: [DONE]\n\n')

/** 鸭子类型的流式 Response（与 useAsk.spec 同法）。 */
function streamResp(chunks: Uint8Array[], { ok = true, status = 200 } = {}) {
  let i = 0
  const reader = {
    read: async () => (i < chunks.length ? { value: chunks[i++], done: false } : { value: undefined, done: true }),
    cancel() {},
  }
  return { ok, status, body: { getReader: () => reader }, text: async (): Promise<string> => '' }
}
function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async (): Promise<string> => JSON.stringify(body) }
}
async function waitFor(cond: () => boolean, ms = 1000) {
  const t0 = Date.now()
  while (!cond() && Date.now() - t0 < ms) await new Promise((r) => setTimeout(r, 5))
}

function setUser(uid = 'user_a', role: Role = 'employee', token = 'TKN_' + uid) {
  const s = useSession()
  s.setToken(token)
  s.setIdentity({
    userId: uid, name: uid, role, aclGroups: ['marketing'],
    canManage: role !== 'employee', managedOwnerDepts: role === 'employee' ? [] : ['marketing'],
  })
  return s
}

function runRow(over: Record<string, unknown> = {}) {
  return {
    run_id: 'r1', status: 'succeeded', thread_id: 't1', conversation_id: 'c1',
    agent_profile: 'default', turns_used: 1, tool_calls_used: 0, tokens_used: 100,
    started_at: '2026-07-11 09:00:00', ended_at: '2026-07-11 09:00:30', ...over,
  }
}
function runDetail(status: string, over: Record<string, unknown> = {}) {
  return {
    run: runRow({ status, user_id: 'user_a' }),
    steps: [], invocations: [], approval: null, ...over,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  __resetAsk()
  __resetAgentAsk()
  localStorage.clear()
  vi.restoreAllMocks()
  setUser()
})
// 尾清：撤掉测试遗留的真实轮询表与 400ms persist 定时器（环境拆除后再触发会炸 worker 定时器队列）
afterEach(() => { __resetAgentAsk(); __resetAsk(); vi.useRealTimers() })

describe('能力探测（GET /api/agent/runs 双职：探测 + 我的 runs）', () => {
  it('200 → supported=true + runs 填充（开关/运行中心入口的渲染判据）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ items: [runRow()] })))
    const a = useAgentAsk()
    await a.probeAgent()
    expect(a.agentSupported.value).toBe(true)
    expect(a.agentRuns.value.map((r) => r.run_id)).toEqual(['r1'])
  })

  it('404（RAG_AGENT_ENABLE 未开）→ supported=false 静默；模式强制关（绝不留死入口）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({}, { ok: false, status: 404 })))
    const a = useAgentAsk()
    await a.probeAgent()
    expect(a.agentSupported.value).toBe(false)
    expect(a.agentMode.value).toBe(false)
  })

  it('kill switch localStorage uid 戳：本人恢复、别人的戳作废删除', async () => {
    localStorage.setItem('fl-agent-mode', JSON.stringify({ uid: 'user_a', on: true }))
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ items: [] })))
    const a = useAgentAsk()
    await a.probeAgent()
    expect(a.agentMode.value).toBe(true)          // 本人偏好恢复

    __resetAgentAsk()
    setActivePinia(createPinia())
    setUser('user_b')
    localStorage.setItem('fl-agent-mode', JSON.stringify({ uid: 'user_a', on: true }))
    await useAgentAsk().probeAgent()
    expect(useAgentAsk().agentMode.value).toBe(false)              // 别人的偏好不复活
    expect(localStorage.getItem('fl-agent-mode')).toBeNull()       // 且戳被清
  })
})

describe('askAgent — 正常流（session→chunk/tool_call/tool_result→done→[DONE]）', () => {
  it('打字累积、工具 chip 收敛（succeeded+耗时）、阶段条真实事件序列、messageId 定稿后才提升', async () => {
    const chunks = [
      frame({ type: 'session', session_id: 's1', message_id: 'm1', run_id: 'r1' }),
      frame({ type: 'chunk', content: '先查库存' }),
      frame({ type: 'tool_call', call_id: 'c1', tool_name: 'knowledge_search', arguments: { query: '纸杯' } }),
      frame({ type: 'tool_result', call_id: 'c1', tool_name: 'knowledge_search', status: 'succeeded', elapsed_ms: 1200 }),
      frame({ type: 'chunk', content: '，结果如下。' }),
      frame({ type: 'done', usage: { total_tokens: 10 } }),
      DONE,
    ]
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (String(path) === '/api/agent/ask') return streamResp(chunks)
      if (String(path).startsWith('/api/agent/runs/')) return jsonResp(runDetail('succeeded'))
      return jsonResp({}, { ok: false, status: 404 })
    }))
    const { askAgent } = useAgentAsk()
    const { messages, asking } = useAsk()

    await askAgent('库存怎么查')

    expect(messages.value.map((m) => m.role)).toEqual(['user', 'ai'])
    const ai = messages.value[1]
    expect(ai.agent?.runId).toBe('r1')
    expect(ai.html).toContain('先查库存')
    expect(ai.html).toContain('结果如下')
    expect(ai.agent?.tools?.[0]).toMatchObject({ callId: 'c1', toolName: 'knowledge_search', status: 'succeeded', elapsedMs: 1200 })
    // 阶段条：已提交→回答中→调用工具→工具完成→回答中→完成（只映射真实事件，连续同阶段去重）
    expect((ai.agent?.stages || []).map((s) => s.key))
      .toEqual(['submitted', 'answering', 'tool', 'tool_done', 'answering', 'done'])
    expect(ai.messageId).toBe('m1')          // done 定稿后提升（反馈条判据）
    expect(ai.agent?.status).toBe('succeeded')
    expect(ai.streaming).toBe(false)
    expect(asking.value).toBe(false)
  })

  it('tool_result 四种结局逐一落到对应 chip 状态', async () => {
    const mk = (st: string, i: number) => [
      frame({ type: 'tool_call', call_id: `c${i}`, tool_name: 'u8_writeback', arguments: { qty: i } }),
      frame({ type: 'tool_result', call_id: `c${i}`, tool_name: 'u8_writeback', status: st, elapsed_ms: 5 }),
    ]
    const chunks = [
      frame({ type: 'session', session_id: 's', message_id: 'm', run_id: 'r9' }),
      ...mk('succeeded', 1), ...mk('failed', 2), ...mk('denied', 3), ...mk('pending_approval', 4),
      frame({ type: 'chunk', content: 'ok' }),
      frame({ type: 'done', usage: {} }),
      DONE,
    ]
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (String(path) === '/api/agent/ask') return streamResp(chunks)
      return jsonResp(runDetail('succeeded'))
    }))
    const { askAgent } = useAgentAsk()
    await askAgent('四种结局')
    const ai = useAsk().messages.value[1]
    expect((ai.agent?.tools || []).map((t) => t.status))
      .toEqual(['succeeded', 'failed', 'denied', 'pending_approval'])
  })
})

describe('askAgent — approval 挂起 + 断线恢复轮询', () => {
  function approvalChunks() {
    return [
      frame({ type: 'session', session_id: 's2', message_id: 'm2', run_id: 'r2' }),
      frame({ type: 'chunk', content: '需要写回 U8。' }),
      frame({ type: 'tool_call', call_id: 'c9', tool_name: 'u8_writeback', arguments: { qty: 120 } }),
      frame({ type: 'approval', approval_request_id: 'ap9', checkpoint_id: 'ck1', pending_call: { call_id: 'c9', tool_name: 'u8_writeback', arguments: { qty: 120 } } }),
      DONE,
    ]
  }

  it('approval 帧 → 挂起卡数据 + chip 转等待审批 + 流结束不算错误', async () => {
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (String(path) === '/api/agent/ask') return streamResp(approvalChunks())
      if (String(path).startsWith('/api/agent/runs/')) return jsonResp(runDetail('suspended'))
      return jsonResp({}, { ok: false, status: 404 })
    }))
    const { askAgent } = useAgentAsk()
    await askAgent('把库存写回 U8')
    const ai = useAsk().messages.value[1]
    expect(ai.agent?.approval).toMatchObject({ requestId: 'ap9', toolName: 'u8_writeback', args: { qty: 120 } })
    expect(ai.agent?.status).toBe('suspended')
    expect(ai.agent?.tools?.[0].status).toBe('pending_approval')
    expect(ai.error).toBeFalsy()               // 挂起是合法结局，不是错误
    expect(ai.messageId).toBeFalsy()           // 未定稿不出反馈条
    expect(useAsk().asking.value).toBe(false)  // 流已结束，可继续新提问
  })

  it('轮询：按节拍拉 run 详情；审批通过（succeeded）回写消息与回执后终态停（含 uncertain 透出）', async () => {
    __agentAskTestkit().setPollMs(15)          // 真实定时器缩拍（fake-timers 会与 happy-dom 内部定时互踩）
    let detailCalls = 0
    let phase: 'suspended' | 'succeeded' = 'suspended'
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      const p = String(path)
      if (p === '/api/agent/ask') return streamResp(approvalChunks())
      if (p.startsWith('/api/agent/runs/')) {
        detailCalls++
        return jsonResp(phase === 'suspended'
          ? runDetail('suspended')
          : runDetail('succeeded', {
              invocations: [
                { invocation_id: 'i1', tool_name: 'u8_writeback', status: 'succeeded' },
                { invocation_id: 'i2', tool_name: 'u8_writeback', status: 'uncertain' },
              ],
            }))
      }
      return jsonResp({}, { ok: false, status: 404 })
    }))
    const a = useAgentAsk()
    await a.askAgent('写回并挂起')
    await waitFor(() => detailCalls >= 2)      // approval 帧的立即轮询 + 至少一拍间隔（挂起态持续轮询）
    expect(__agentAskTestkit().pollTimerCount()).toBe(1)

    phase = 'succeeded'                        // 审批人在别处批准，run 服务端续跑完成
    await waitFor(() => useAsk().messages.value[1].agent?.status === 'succeeded')
    const ai = useAsk().messages.value[1]
    expect(ai.agent?.status).toBe('succeeded') // 不依赖原 SSE 也看到完成
    expect(ai.messageId).toBe('m2')            // 终态提升 messageId（反馈条可用）
    const det = a.agentRunDetails.value['r2']
    expect(det.invocations.some((x) => x.status === 'uncertain')).toBe(true)   // 回执含「结果不确定」

    // 终态停表：轮询表已摘除，再等 8+ 个节拍不再打详情接口
    await waitFor(() => __agentAskTestkit().pollTimerCount() === 0)
    const settled = detailCalls
    await new Promise((r) => setTimeout(r, 150))
    expect(detailCalls).toBe(settled)
  })
})

describe('askAgent — 兜底与错误', () => {
  it('/api/agent/ask 404（flag 中途被关）→ supported=false + 本问自动回退旧 RAG 路径（单用户气泡）', async () => {
    const legacy = [
      frame({ type: 'session', session_id: 'sL', message_id: 'mL' }),
      frame({ type: 'chunk', content: '旧路径答案' }),
      frame({ type: 'done', model: 'q', usage: {}, guard: false }),
      DONE,
    ]
    const f = vi.fn(async (path: string) => {
      if (String(path) === '/api/agent/ask') return jsonResp({ detail: 'Not Found' }, { ok: false, status: 404 })
      if (String(path) === '/api/ask/stream') return streamResp(legacy)
      return jsonResp({}, { ok: false, status: 404 })
    })
    vi.stubGlobal('fetch', f)
    const a = useAgentAsk()
    a.agentSupported.value = true
    a.agentMode.value = true

    await a.askAgent('问一下')
    await waitFor(() => !useAsk().asking.value && (useAsk().messages.value[1]?.html || '').includes('旧路径答案'))

    expect(a.agentSupported.value).toBe(false)
    expect(a.agentMode.value).toBe(false)      // 开关关回（死入口不留）
    const msgs = useAsk().messages.value
    expect(msgs.filter((m) => m.role === 'user')).toHaveLength(1)   // 复用已推的用户气泡
    expect(msgs[msgs.length - 1].agent).toBeUndefined()             // 旧路径消息（agent 占位已移除）
    expect(msgs[msgs.length - 1].html).toContain('旧路径答案')
  })

  it('429（并发满）→ 错误卡带并发文案', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 429, text: async () => 'too many' })))
    await useAgentAsk().askAgent('挤一挤')
    const ai = useAsk().messages.value[1]
    expect(ai.error).toBe(true)
    expect(ai.errorText).toContain('并发已满')
  })

  it('流内 error 帧 → 错误态 + 阶段条落「失败」', async () => {
    const chunks = [
      frame({ type: 'session', session_id: 's', message_id: 'm', run_id: 'rE' }),
      frame({ type: 'chunk', content: '一半…' }),
      frame({ type: 'error', message: 'Agent 运行失败: boom' }),
      DONE,
    ]
    vi.stubGlobal('fetch', vi.fn(async () => streamResp(chunks)))
    await useAgentAsk().askAgent('触发失败')
    const ai = useAsk().messages.value[1]
    expect(ai.error).toBe(true)
    expect(ai.errorText).toContain('Agent 运行失败')
    expect(ai.agent?.status).toBe('failed')
    expect((ai.agent?.stages || []).map((s) => s.key)).toContain('error')
  })

  it('stopAgent：只断视图不断 run——有 run_id 转轮询兜底（disconnected），无内容无 run 才算取消', async () => {
    // 环境加固（都是 happy-dom 测试环境 artifact，产品代码路径不变）：
    //  · rAF 置空 → scheduleAgentRender 走显式的"无 rAF 即时渲染"回退。happy-dom 的 rAF 在
    //    「读悬挂 + 真实定时器」窗口里与 vitest worker 定时器队列互踩，产生 node 层
    //    _idleNext 崩溃（processImmediate）——本测试不测渲染节奏，直接绕开；
    //  · AbortController 用极简替身（abort 仅置位，不经 happy-dom 事件派发）。
    vi.stubGlobal('requestAnimationFrame', undefined)
    vi.stubGlobal('cancelAnimationFrame', undefined)
    vi.stubGlobal('AbortController', class {
      signal: any = { aborted: false }
      abort() { this.signal.aborted = true }
    })
    const frames = [
      frame({ type: 'session', session_id: 's', message_id: 'm', run_id: 'rS' }),
      frame({ type: 'chunk', content: '写到一半' }),
    ]
    let i = 0
    let releaseRead!: (v: { value: Uint8Array | undefined; done: boolean }) => void
    const gate = new Promise<{ value: Uint8Array | undefined; done: boolean }>((r) => { releaseRead = r })
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (String(path) === '/api/agent/ask') {
        return {
          ok: true, status: 200, text: async (): Promise<string> => '',
          body: {
            getReader: () => ({
              // 前两帧即时到达，其后悬挂在 gate 上（模拟长流），由测试显式放行收束
              read: () => (i < frames.length ? Promise.resolve({ value: frames[i++], done: false }) : gate),
              cancel() {},
            }),
          },
        }
      }
      return jsonResp(runDetail('running'))
    }))
    const a = useAgentAsk()
    const p = a.askAgent('慢问题')
    await waitFor(() => !!useAsk().messages.value[1]?.agent?.runId)   // 前两帧落地
    const ai = useAsk().messages.value[1]
    expect(ai.agent?.runId).toBe('rS')

    a.stopAgent()
    releaseRead({ value: undefined, done: true })   // 悬挂读放行 → reader 循环按 seq 判废收束
    await p
    expect(useAsk().asking.value).toBe(false)
    expect(ai.streaming).toBe(false)
    expect(ai.agent?.disconnected).toBe(true)   // run 仍在服务端 → 轮询兜底而非报错
    expect(ai.error).toBeFalsy()
    await waitFor(() => !!a.agentRunDetails.value['rS'])   // 立即轮询已拉详情
  })
})

describe('撤回（发起人自撤）与身份纪律', () => {
  it('withdrawRun → POST /api/agent/approve rejected_terminate（幂等键=request:kind），成功后详情对齐 cancelled', async () => {
    const cancel = vi.fn()
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: any) => {
      calls.push([String(path), init])
      if (String(path) === '/api/agent/approve') return { ok: true, status: 200, body: { cancel } }
      if (String(path).startsWith('/api/agent/runs/')) return jsonResp(runDetail('cancelled'))
      return jsonResp({}, { ok: false, status: 404 })
    }))
    const a = useAgentAsk()
    const ok = await a.withdrawRun('r5', 'ap5')
    expect(ok).toBe(true)
    const post = calls.find(([p]) => p === '/api/agent/approve')
    const body = JSON.parse(post[1].body)
    expect(body).toMatchObject({ run_id: 'r5', outcome: { kind: 'rejected_terminate' }, idempotency_key: 'ap5:rejected_terminate' })
    expect(cancel).toHaveBeenCalled()          // 终止流不旁观
    expect(a.agentRunDetails.value['r5']?.run?.status).toBe('cancelled')
  })

  it('身份切换（换人）→ supported/runs/详情/开关全清 + localStorage 戳删除（identityScope 统一触发）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ items: [runRow()] })))
    const a = useAgentAsk()
    await a.probeAgent()
    a.agentMode.value = true
    localStorage.setItem('fl-agent-mode', JSON.stringify({ uid: 'user_a', on: true }))
    expect(a.agentSupported.value).toBe(true)
    expect(a.agentRuns.value).toHaveLength(1)

    setUser('user_b')
    syncIdentityScope()
    expect(a.agentSupported.value).toBeNull()   // 重新探测（B 的角色/flag 可能不同）
    expect(a.agentRuns.value).toEqual([])       // A 的 runs 不泄给 B
    expect(a.agentMode.value).toBe(false)
    expect(localStorage.getItem('fl-agent-mode')).toBeNull()
  })

  it('同人换 token（401 重登）→ 数据面作废重探测，但开关偏好保留', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ items: [runRow()] })))
    const a = useAgentAsk()
    await a.probeAgent()
    a.agentMode.value = true
    setUser('user_a', 'employee', 'TKN_user_a_v2')   // 同人换证
    syncIdentityScope()
    expect(a.agentSupported.value).toBeNull()
    expect(a.agentMode.value).toBe(true)              // 偏好不清（数据无泄露面）
  })
})
