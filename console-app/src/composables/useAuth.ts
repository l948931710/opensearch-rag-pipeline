import { useSession, toIdentity } from '@/stores/session'
import { apiJson } from '@/lib/api'

// 本企业 corpId（非密钥，可硬编码兜底）。钉钉「PC 端访问地址」注入的 H5 拿不到 corpId 时用它，
// 否则 requestAuthCode 报 'corpId is illegal'。URL 带 ?corpId= 时优先。
const CORP_ID_FALLBACK = 'dingcafb3fdca0e8380a'

declare global {
  interface Window { dd?: any }
}

/** 单次 init 守卫（修正#6）：App.vue 唯一触发，store/router-guard 不再各自打 authCode（防重复烧配额）。 */
let _initPromise: Promise<void> | null = null

export function qs(name: string): string {
  const m = new RegExp('[?&]' + name + '=([^&]+)').exec(window.location.search)
  return m ? decodeURIComponent(m[1]) : ''
}

/**
 * 从地址栏抹除敏感 query 参数（修正#4）：token / name 读取后立即 replaceState 清掉，
 * 防 token 进入浏览器历史、日志、截图、Referer、监控。保留路径/hash/其它参数。
 */
export function scrubUrl(params: string[]) {
  const url = new URL(window.location.href)
  let changed = false
  for (const p of params) if (url.searchParams.has(p)) { url.searchParams.delete(p); changed = true }
  if (changed) {
    const next = url.pathname + (url.searchParams.toString() ? '?' + url.searchParams.toString() : '') + url.hash
    window.history.replaceState(window.history.state, '', next)
  }
}

/**
 * 钉钉容器内取一次性免登 authCode（5 分钟有效）。无 dd → 立即抛「非钉钉环境」。
 * ⚠️ SDK 已加载但【不在钉钉客户端】时 dd.ready 回调永不触发，故加超时兜底，避免页面永挂「正在登录」。
 */
function getAuthCode(corpId: string, timeoutMs = 4000): Promise<string> {
  return new Promise((resolve, reject) => {
    const dd = window.dd
    if (!dd || !dd.ready) { reject(new Error('非钉钉环境（请在钉钉客户端中打开本页面）')); return }
    let settled = false
    const finish = (fn: () => void) => { if (!settled) { settled = true; clearTimeout(timer); fn() } }
    const timer = setTimeout(() => finish(() => reject(new Error('未能完成免登，请在钉钉客户端中打开本页面'))), timeoutMs)
    dd.error((err: any) => finish(() => reject(new Error('dd.error：' + safeJson(err)))))
    dd.ready(() => {
      dd.runtime.permission.requestAuthCode({
        corpId,
        onSuccess: (res: any) => finish(() => (res && res.code ? resolve(res.code) : reject(new Error('requestAuthCode 未返回 code')))),
        onFail: (err: any) => finish(() => reject(new Error('requestAuthCode 失败：' + safeJson(err)))),
      })
    })
  })
}

function safeJson(v: unknown): string { try { return JSON.stringify(v) } catch { return String(v) } }

/**
 * 执行一次登录：
 *  ① URL 透传 token（小程序 web-view）优先 → 存 token、抹 URL、whoami 取权威身份。
 *  ② 否则钉钉容器内 requestAuthCode → /api/auth/dingtalk 换证。
 * force=true（401 重登）跳过 ① 直接重走容器免登。
 */
async function doLogin(force: boolean): Promise<void> {
  const session = useSession()

  if (!force) {
    const urlToken = qs('token')
    if (urlToken) {
      const name = qs('name')
      session.setToken(urlToken)
      scrubUrl(['token', 'name'])                       // 先抹除，再发请求（即使 whoami 失败 token 也已离开 URL）
      const who = await apiJson<Record<string, any>>('/api/kb/whoami', { auth: true })
      session.setIdentity(toIdentity({ ...who, display_name: who.display_name || name }))
      return
    }
  }

  const corpId = qs('corpId') || qs('corpid') || CORP_ID_FALLBACK
  const code = await getAuthCode(corpId)
  const data = await apiJson<Record<string, any>>('/api/auth/dingtalk', {
    method: 'POST', auth: false, body: JSON.stringify({ auth_code: code }),
  })
  if (!data || !data.token) throw new Error('换取令牌失败')
  session.setToken(data.token)
  session.setIdentity(toIdentity(data))
}

export function useAuth() {
  const session = useSession()

  /** 唯一启动入口（单次）。重复调用返回同一 Promise，绝不二次触发免登。 */
  function init(): Promise<void> {
    if (_initPromise) return _initPromise
    _initPromise = (async () => {
      try {
        await doLogin(false)
        session.ready = true
        session.error = ''
      } catch (e: any) {
        session.ready = false
        session.error = e?.message || '登录失败'
      }
    })()
    return _initPromise
  }

  /** 401 重登：清旧 token，强制重走容器免登一次。成功返回 true。 */
  async function reauth(): Promise<boolean> {
    try {
      session.setToken('')
      await doLogin(true)
      return !!session.token
    } catch {
      return false
    }
  }

  return { init, reauth }
}

/** 仅供测试：重置单次守卫。 */
export function __resetInitGuard() { _initPromise = null }
