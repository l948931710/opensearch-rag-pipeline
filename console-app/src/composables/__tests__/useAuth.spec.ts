import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuth, scrubUrl, qs, captureUrlCredential, __resetInitGuard } from '@/composables/useAuth'
import { useSession } from '@/stores/session'

function jsonRes(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}
function setUrl(search: string) {
  window.history.replaceState(null, '', '/console-next/' + search)
}

beforeEach(() => {
  setActivePinia(createPinia())
  __resetInitGuard()
  setUrl('')
  delete (window as any).dd
  vi.restoreAllMocks()
})
afterEach(() => { delete (window as any).dd })

describe('scrubUrl（修正#4：token 读后从 URL 抹除）', () => {
  it('删除 token/name，保留其它参数与路径', () => {
    setUrl('?token=SECRET&name=%E5%BC%A0%E4%B8%89&doc_id=DOC_1')
    scrubUrl(['token', 'name'])
    expect(window.location.search).not.toContain('token')
    expect(window.location.search).not.toContain('name')
    expect(window.location.search).toContain('doc_id=DOC_1')
    expect(window.location.pathname).toBe('/console-next/')
  })
})

describe('qs', () => {
  it('读取 query 并解码', () => {
    setUrl('?name=%E9%A2%84%E8%A7%88')
    expect(qs('name')).toBe('预览')
    expect(qs('absent')).toBe('')
  })
})

describe('captureUrlCredential（早捕获：先抹 URL、token 暂存而非立即落 store）', () => {
  it('抹除 URL token/name；不落 store；随后 init 用暂存 token 走 whoami', async () => {
    setUrl('?token=EARLY&name=%E5%BC%A0')
    captureUrlCredential()                       // 模拟 main 第一个 import（router 加载前）的早调用
    expect(window.location.search).not.toContain('EARLY') // 已立即抹除（先于任何请求）
    expect(window.location.search).not.toContain('name')
    expect(useSession().token).toBe('')          // 早捕获不碰 store（彼时 Pinia 可能尚未创建）

    const fetchMock = vi.fn().mockResolvedValue(jsonRes({
      user_id: 'u', role: 'kb_admin', can_manage_kb: true, // display_name 缺省 → 用早捕获的 name 兜底
    }))
    vi.stubGlobal('fetch', fetchMock)
    await useAuth().init()
    expect(useSession().token).toBe('EARLY')      // doLogin 注入暂存 token
    expect(fetchMock.mock.calls[0][0]).toBe('/api/kb/whoami')
    expect(useSession().identity?.name).toBe('张') // 兜底显示名
  })

  it('幂等：第二次 capture 不再改动（已捕获守卫）', () => {
    setUrl('?token=ONCE')
    captureUrlCredential()
    setUrl('?token=AGAIN')                         // 即便 URL 又出现 token
    captureUrlCredential()                         // 守卫拦下，不二次暂存
    expect(window.location.search).toContain('AGAIN') // 第二次未抹除（已被守卫短路）
  })
})

describe('init — URL token 透传路径', () => {
  it('存 token、抹 URL、whoami 取权威身份', async () => {
    setUrl('?token=TKN123&name=%E5%BC%A0%E4%B8%89')
    const fetchMock = vi.fn().mockResolvedValue(jsonRes({
      user_id: 'u1', display_name: '张三', role: 'kb_admin', can_manage_kb: true,
      acl_groups: ['marketing'], managed_owner_depts: ['marketing'],
    }))
    vi.stubGlobal('fetch', fetchMock)

    await useAuth().init()
    const s = useSession()
    expect(s.token).toBe('TKN123')
    expect(s.ready).toBe(true)
    expect(s.identity?.role).toBe('kb_admin')
    expect(s.canManage).toBe(true)
    // token 已离开地址栏（防泄露）
    expect(window.location.search).not.toContain('TKN123')
    // 调的是 whoami（带 Bearer）
    expect(fetchMock.mock.calls[0][0]).toBe('/api/kb/whoami')
    expect((fetchMock.mock.calls[0][1].headers as Headers).get('Authorization')).toBe('Bearer TKN123')
  })
})

describe('init — 单次守卫（修正#6）', () => {
  it('重复调用只触发一次免登', async () => {
    setUrl('?token=TKN')
    const fetchMock = vi.fn().mockResolvedValue(jsonRes({ user_id: 'u', role: 'employee', can_manage_kb: false }))
    vi.stubGlobal('fetch', fetchMock)
    const auth = useAuth()
    await Promise.all([auth.init(), auth.init(), auth.init()])
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe('init — 钉钉容器内 requestAuthCode 换证路径', () => {
  it('无 URL token → requestAuthCode → /api/auth/dingtalk 换 token+身份', async () => {
    setUrl('')
    ;(window as any).dd = {
      ready: (cb: () => void) => cb(),
      error: () => {},
      runtime: { permission: { requestAuthCode: (o: any) => o.onSuccess({ code: 'CODE9' }) } },
    }
    const fetchMock = vi.fn().mockResolvedValue(jsonRes({
      token: 'SRV_TKN', user_id: 'u2', display_name: '李四', role: 'dept_admin', can_manage_kb: true, acl_groups: ['hr'],
    }))
    vi.stubGlobal('fetch', fetchMock)

    await useAuth().init()
    const s = useSession()
    expect(s.token).toBe('SRV_TKN')
    expect(s.identity?.role).toBe('dept_admin')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/auth/dingtalk')
    // 换证请求体带 auth_code，且匿名（无 Bearer）
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ auth_code: 'CODE9' })
    expect((fetchMock.mock.calls[0][1].headers as Headers).has('Authorization')).toBe(false)
  })
})

describe('init — 非钉钉环境优雅失败', () => {
  it('无 token 且无 dd → error 文案，ready=false', async () => {
    setUrl('')
    await useAuth().init()
    const s = useSession()
    expect(s.ready).toBe(false)
    expect(s.error).toContain('钉钉')
  })
})

describe('init — SDK 已加载但 dd.ready 永不触发（非钉钉浏览器）', () => {
  it('超时兜底：落「请在钉钉客户端中打开」而非永挂「正在登录」', async () => {
    vi.useFakeTimers()
    setUrl('')
    ;(window as any).dd = { ready: () => { /* 永不回调 */ }, error: () => {}, runtime: { permission: { requestAuthCode: () => {} } } }
    const p = useAuth().init()
    await vi.advanceTimersByTimeAsync(4200)
    await p
    const s = useSession()
    expect(s.ready).toBe(false)
    expect(s.error).toContain('钉钉')
    vi.useRealTimers()
  })
})

describe('reauth — 401 重登走容器免登', () => {
  it('清旧 token、重走 requestAuthCode、成功返回 true', async () => {
    const s = useSession()
    s.setToken('OLD')
    ;(window as any).dd = {
      ready: (cb: () => void) => cb(),
      error: () => {},
      runtime: { permission: { requestAuthCode: (o: any) => o.onSuccess({ code: 'C2' }) } },
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonRes({ token: 'FRESH', user_id: 'u', role: 'employee', can_manage_kb: false })))
    const ok = await useAuth().reauth()
    expect(ok).toBe(true)
    expect(s.token).toBe('FRESH')
  })
})
