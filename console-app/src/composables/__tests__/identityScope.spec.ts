import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSession, type Role } from '@/stores/session'
import { registerIdentityScopedStore, syncIdentityScope, __resetIdentityScope, type IdentitySwitch } from '@/composables/identityScope'
import { useAgentApprovals, __resetAgentApprovals } from '@/composables/useAgentApprovals'
import { useOntology, __resetOntology } from '@/composables/useOntology'
import { useKb, __resetKb } from '@/composables/useKb'
import { useContribute, __resetContribute } from '@/composables/useContribute'
import { useAsk, __resetAsk } from '@/composables/useAsk'
import { useAuth, __resetInitGuard } from '@/composables/useAuth'

// P0-D「审批状态跨登录身份复用」回归防线（2026-07-10 审计确认 / 07-11 复核）：
// useAgentApprovals 等模块级单例 + 30s freshness 与身份无关——A 登出、B 登录后 B 在窗口内
// 能看到 A 的 pending tool/args/requester/scope。修复=identityScope 统一注册：
// 身份键（userId+role+ACL 版本+token）一变同步清空；loader 入口 lazy 对账；在途响应按指纹判废。
// 覆盖审计验收清单：logout / 401 re-login / switch-account / role refresh 四景 + 在途竞态。

function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}
async function waitFor(cond: () => boolean, ms = 1000) {
  const t0 = Date.now()
  while (!cond() && Date.now() - t0 < ms) await new Promise((r) => setTimeout(r, 5))
}

function setUser(uid: string, role: Role = 'kb_admin', groups: string[] = ['production'], token = 'TKN_' + uid) {
  const s = useSession()
  s.setToken(token)
  s.setIdentity({
    userId: uid, name: uid, role,
    aclGroups: groups, canManage: role !== 'employee',
    managedOwnerDepts: role === 'employee' ? [] : groups,
  })
  return s
}

function agentItem(id: string, by: string) {
  return {
    request_id: id, run_id: 'r' + id, call_id: 'c1', tool_name: 'u8_writeback', tool_version: '1.0',
    proposed_args: { qty: 120, item: 'PP 刀叉 8寸' }, args_digest: 'd', render_summary: 'u8_writeback(item, qty)',
    requested_by: by, requested_dept: 'production', approver_scope: 'production', status: 'pending',
    expires_at: null, created_at: null, decided_at: null,
  }
}

function bearerOf(init?: any): string {
  const h = init?.headers
  const v = h instanceof Headers ? (h.get('Authorization') || '') : ''
  return v.replace('Bearer ', '')
}

/** 按 Bearer 分流的 fetch：A 的 token（含重登新证 TKN_admin_a_v2）拿 A 的队列、
 *  其他 token 拿 B 的队列——证明「落进 store 的是谁的数据」。 */
function identityAwareFetch() {
  return vi.fn(async (path: string, init?: any) => {
    const mine = bearerOf(init).startsWith('TKN_admin_a')
    if (path.startsWith('/api/agent/approvals')) {
      return jsonResp({ items: mine ? [agentItem('a1', 'user_wang'), agentItem('a2', 'user_li')] : [agentItem('b1', 'user_zhao')] })
    }
    if (path.startsWith('/api/kb/pending-approvals')) {
      return jsonResp({ items: mine ? [{ doc_id: 'PA', version_no: 1, title: 'A 部门薪酬方案', original_filename: 'a.docx', owner_dept: 'hr', permission_level: 'public', owner_name: '张', created_at: '2026-07-10' }] : [] })
    }
    if (path.startsWith('/api/kb/contributions/pending')) {
      return jsonResp({ items: mine ? [{ contribution_id: 'ct1', question: 'A 的待审贡献', content: 'x', category_dept: 'hr', author_id: 'u9', author_name: '王', review_status: 'pending', ingestion_status: 'none', state: 'pending', doc_id: null, review_note: '', created_at: '2026-07-10', reviewed_at: null }] : [] })
    }
    if (path.startsWith('/api/ontology/workbench')) {
      return jsonResp({ items: mine ? [{ case_id: 'oc1', namespace: 'u8', raw_value: 'ABC-1', norm_value: 'ABC-1', object_type_hint: 'product', status: 'open', seen_count: 2, first_seen_at: null, last_seen_at: null, evidence_json: null, candidates: [], steward_dept: 'pmc' }] : [] })
    }
    if (path.startsWith('/api/ontology/coverage')) {
      return jsonResp({ active_identifiers: 10, auto_active: 5, open_cases: 1, resolved_cases: 3, dismissed_cases: 0, resolution_coverage: 0.5, manual_review_rate: 0.4 })
    }
    return jsonResp({}, { ok: false, status: 404 })
  })
}

function agentCalls(f: ReturnType<typeof vi.fn>): number {
  return f.mock.calls.filter((c) => String(c[0]).startsWith('/api/agent/approvals')).length
}

beforeEach(() => {
  setActivePinia(createPinia())
  __resetAgentApprovals()
  __resetOntology()
  __resetKb()
  __resetContribute()
  __resetAsk()
  __resetInitGuard()
  __resetIdentityScope()
  localStorage.clear()
  vi.restoreAllMocks()
  delete (window as any).dd
})
afterEach(() => { delete (window as any).dd })

describe('identityScope — 键语义', () => {
  it('首见采纳不清空；同键幂等；换 userId 触发清空并携带 prev/next', () => {
    const calls: IdentitySwitch[] = []
    registerIdentityScopedStore('spy', (sw) => calls.push(sw))
    setUser('admin_a')
    syncIdentityScope()                    // 首见：只采纳
    syncIdentityScope()                    // 同键：no-op
    expect(calls).toEqual([])
    setUser('admin_b')
    syncIdentityScope()
    expect(calls).toEqual([{ prevUserId: 'admin_a', nextUserId: 'admin_b' }])
  })

  it('同人换 token 也触发清空（验收 1：「身份或 token 改变同步清空」）', () => {
    const calls: IdentitySwitch[] = []
    registerIdentityScopedStore('spy-token', (sw) => calls.push(sw))
    setUser('admin_a', 'kb_admin', ['hr'], 'T1')
    syncIdentityScope()
    setUser('admin_a', 'kb_admin', ['hr'], 'T2')   // 同人同角色，仅换证
    syncIdentityScope()
    expect(calls).toEqual([{ prevUserId: 'admin_a', nextUserId: 'admin_a' }])
  })

  it('role/ACL 版本进键：同人改角色触发；ACL 组仅换序不触发（排序归一）', () => {
    const calls: IdentitySwitch[] = []
    registerIdentityScopedStore('spy-acl', (sw) => calls.push(sw))
    setUser('admin_a', 'kb_admin', ['hr', 'finance'])
    syncIdentityScope()
    setUser('admin_a', 'kb_admin', ['finance', 'hr'])   // 集合相同仅换序 → 同一 ACL 版本
    syncIdentityScope()
    expect(calls).toEqual([])
    setUser('admin_a', 'dept_admin', ['hr', 'finance'])  // 角色刷新
    syncIdentityScope()
    expect(calls).toHaveLength(1)
  })
})

describe('P0-D — Agent 审批队列跨身份复用（审计原景）', () => {
  it('switch-account：A 载入后 30s freshness 窗口内 B 登录——入口【同步】清空且强制重拉，B 绝不见 A 的 pending', async () => {
    const f = identityAwareFetch()
    vi.stubGlobal('fetch', f)
    const { agentApprovals, loadAgentApprovals } = useAgentApprovals()

    setUser('admin_a')
    await loadAgentApprovals()
    expect(agentApprovals.value.map((x) => x.request_id)).toEqual(['a1', 'a2'])   // A 的队列（含 requester/args）

    setUser('admin_b')                       // 换号（lazy 兜底路径：不经 useAuth）
    const p = loadAgentApprovals()           // 修复前：30s 内直接吃 A 的缓存返回
    expect(agentApprovals.value).toEqual([]) // 【同步】清空——首个 await 之前 B 已看不到 A 的任何条目
    await p
    expect(agentCalls(f)).toBe(2)            // freshness 门随身份重开：真的重新拉了
    expect(agentApprovals.value.map((x) => x.request_id)).toEqual(['b1'])
    expect(agentApprovals.value.some((x) => x.requested_by === 'user_wang')).toBe(false)
  })

  it('logout（fail-closed）：reauth 起点即清——重登失败也不残留 A 的队列', async () => {
    vi.stubGlobal('fetch', identityAwareFetch())
    const { agentApprovals, loadAgentApprovals } = useAgentApprovals()
    setUser('admin_a')
    await loadAgentApprovals()
    expect(agentApprovals.value).toHaveLength(2)

    const ok = await useAuth().reauth()      // 无 dd 容器 → 容器免登必失败
    expect(ok).toBe(false)
    expect(useSession().token).toBe('')
    expect(agentApprovals.value).toEqual([])  // 登出瞬间已清空（不是等到下次 load）
  })

  it('401 re-login（同人换证）：审批缓存清空 + freshness 门重开；问答历史按 uid 戳保留', async () => {
    const f = identityAwareFetch()
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: any) => {
      if (path === '/api/auth/dingtalk') {
        return jsonResp({ token: 'TKN_admin_a_v2', user_id: 'admin_a', display_name: 'A', role: 'kb_admin', can_manage_kb: true, acl_groups: ['production'], managed_owner_depts: ['production'] })
      }
      return f(path, init)
    }))
    ;(window as any).dd = {
      ready: (cb: () => void) => cb(),
      error: () => {},
      runtime: { permission: { requestAuthCode: (o: any) => o.onSuccess({ code: 'C1' }) } },
    }

    setUser('admin_a')
    const { agentApprovals, loadAgentApprovals } = useAgentApprovals()
    await loadAgentApprovals()
    expect(agentApprovals.value).toHaveLength(2)
    // 同人的本地问答历史（uid 戳 = admin_a）
    const askApi = useAsk()
    localStorage.setItem('fl-conversations', JSON.stringify({ uid: 'admin_a', activeId: 'c1', conversations: [] }))
    askApi.conversations.value.push({ id: 'c1', title: 'A 的会话', messages: [{ id: 'u1', role: 'user', text: '上月产量' } as any], qaSession: '', updatedAt: Date.now() })

    const ok = await useAuth().reauth()       // 同一用户拿到新 token
    expect(ok).toBe(true)
    expect(useSession().token).toBe('TKN_admin_a_v2')
    expect(agentApprovals.value).toEqual([])                       // token 变 → 审批缓存作废
    expect(askApi.conversations.value.map((c) => c.title)).toEqual(['A 的会话'])   // 同人：历史保留，不误清

    await loadAgentApprovals()                 // 30s 内再拉：门已重开，不吃旧缓存
    expect(agentApprovals.value.map((x) => x.request_id)).toEqual(['a1', 'a2'])
  })

  it('role refresh：同人角色被刷新（kb_admin→dept_admin）→ 队列/探测态清空', async () => {
    vi.stubGlobal('fetch', identityAwareFetch())
    const { agentApprovals, agentApprovalsSupported, loadAgentApprovals } = useAgentApprovals()
    setUser('admin_a')
    await loadAgentApprovals()
    expect(agentApprovals.value).toHaveLength(2)
    expect(agentApprovalsSupported.value).toBe(true)

    // whoami 复核把角色刷成 dept_admin（doLogin 落定身份后会调 syncIdentityScope，这里等价模拟）
    setUser('admin_a', 'dept_admin', ['production'], 'TKN_admin_a')
    syncIdentityScope()
    expect(agentApprovals.value).toEqual([])
    expect(agentApprovalsSupported.value).toBeNull()   // 探测态也不残留（tab 显隐重新探测）
  })

  it('员工身份切入：supported/队列不残留，且按 canManage 门禁不再发请求', async () => {
    const f = identityAwareFetch()
    vi.stubGlobal('fetch', f)
    const { agentApprovals, agentApprovalsSupported, loadAgentApprovals } = useAgentApprovals()
    setUser('admin_a')
    await loadAgentApprovals()
    expect(agentApprovalsSupported.value).toBe(true)

    setUser('emp_c', 'employee', ['marketing'])
    await loadAgentApprovals()
    expect(agentApprovals.value).toEqual([])
    expect(agentApprovalsSupported.value).toBeNull()   // 入口对账已清，员工分支不再置 true
    expect(agentCalls(f)).toBe(1)                       // 员工不打审批接口
  })

  it('在途竞态：A 的慢响应在切到 B 之后才回——按身份指纹整体丢弃，不落 B 的视图', async () => {
    let resolveA!: (v: any) => void
    const pending = new Promise((r) => { resolveA = r })
    const f = vi.fn(async (path: string, init?: any) => {
      if (path.startsWith('/api/agent/approvals')) {
        if (bearerOf(init) === 'TKN_admin_a') return pending as any
        return jsonResp({ items: [agentItem('b1', 'user_zhao')] })
      }
      return jsonResp({}, { ok: false, status: 404 })
    })
    vi.stubGlobal('fetch', f)
    const { agentApprovals, agentApprovalsSupported, loadAgentApprovals } = useAgentApprovals()

    setUser('admin_a')
    const p = loadAgentApprovals()            // A 的请求悬在途中
    setUser('admin_b')                        // 身份已切换
    resolveA(jsonResp({ items: [agentItem('a1', 'user_wang')] }))
    await p
    expect(agentApprovals.value).toEqual([])            // A 的响应被判废
    expect(agentApprovalsSupported.value).toBeNull()

    await loadAgentApprovals()                // B 自己的加载照常
    expect(agentApprovals.value.map((x) => x.request_id)).toEqual(['b1'])
  })
})

describe('统一规则 — approval/run/ontology 及其它模块级 store 同步清空', () => {
  it('切账号一次 sync：ontology / kb 审批 / 贡献待审全部作废（禁止独立 singleton 漂移）', async () => {
    vi.stubGlobal('fetch', identityAwareFetch())
    setUser('admin_a')

    const onto = useOntology()
    const kb = useKb()
    const contrib = useContribute()
    await onto.loadOntology()
    await kb.loadApprovals()
    await contrib.loadPending()
    await waitFor(() => onto.ontologyCoverage.value !== null)   // coverage 是 fire-and-forget，等它落地
    expect(onto.ontologyCases.value).toHaveLength(1)
    expect(onto.ontologySupported.value).toBe(true)
    expect(kb.approvals.value).toHaveLength(1)
    expect(kb.queuesSettled.value).toBe(true)
    expect(contrib.pendingContribs.value).toHaveLength(1)

    setUser('admin_b')
    syncIdentityScope()                        // eager 一次（useAuth 在真实登录点就是这么做的）
    expect(onto.ontologyCases.value).toEqual([])
    expect(onto.ontologyCoverage.value).toBeNull()
    expect(onto.ontologySupported.value).toBeNull()
    expect(kb.approvals.value).toEqual([])
    expect(kb.queuesSettled.value).toBe(false)  // 空态文案不再误显「真无待办」
    expect(contrib.pendingContribs.value).toEqual([])
  })

  it('kb 30s staleness 门随身份重开：B 的 loadApprovals 不吃 A 的新鲜度', async () => {
    const f = identityAwareFetch()
    vi.stubGlobal('fetch', f)
    const kb = useKb()
    setUser('admin_a')
    await kb.loadApprovals()
    expect(kb.approvals.value.map((x) => x.doc_id)).toEqual(['PA'])

    setUser('admin_b')
    await kb.loadApprovals()                   // force=false：修复前 30s 内直接 return（沿用 A 的队列）
    const kbCalls = f.mock.calls.filter((c) => String(c[0]).startsWith('/api/kb/pending-approvals')).length
    expect(kbCalls).toBe(2)
    expect(kb.approvals.value).toEqual([])     // B 的真实队列（空），而非 A 的薪酬方案审批单
  })

  it('useAsk：换人清会话/草稿（uid 戳），同人 sync 完全不动', () => {
    setUser('user_a', 'employee', ['marketing'])
    syncIdentityScope()                        // 采纳 A
    const ask = useAsk()
    localStorage.setItem('fl-conversations', JSON.stringify({ uid: 'user_a', activeId: 'c1', conversations: [] }))
    ask.conversations.value.push({ id: 'c1', title: 'A 的部门问题', messages: [{ id: 'u1', role: 'user', text: '内部返工率' } as any], qaSession: '', updatedAt: Date.now() })
    ask.draft.value = '还没发出去的敏感草稿'

    setUser('user_a', 'employee', ['marketing'], 'TKN_user_a_v2')   // 同人换 token
    syncIdentityScope()
    expect(ask.conversations.value.map((c) => c.title)).toEqual(['A 的部门问题'])   // 历史保留
    expect(ask.draft.value).toBe('还没发出去的敏感草稿')

    setUser('user_b', 'employee', ['production'])                   // 真换人
    syncIdentityScope()
    expect(ask.draft.value).toBe('')
    expect(ask.conversations.value.every((c) => c.messages.length === 0)).toBe(true)   // A 的问答不残留
    expect(ask.conversations.value.some((c) => c.title === 'A 的部门问题')).toBe(false)
    expect(localStorage.getItem('fl-conversations')).toBeNull()     // 本地持久化一并清
  })
})
