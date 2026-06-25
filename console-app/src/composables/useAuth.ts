import { useSession, toIdentity } from '@/stores/session'
import { apiJson } from '@/lib/api'
import { diag } from '@/lib/diag'

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

// 早捕获的暂存：token 暂存在模块级（捕获时 Pinia 可能尚未创建，不能落 store），name 作 whoami 兜底显示名。
let _stashedToken = ''
let _capturedName = ''
let _captured = false

/** 升版深链暂存（小程序「上传新版本」→ /console-next/?doc_id=&owner=&title=）。 */
export interface PendingVersion { docId: string; owner: string; title: string }
let _pendingVersion: PendingVersion | null = null

/**
 * 【最早期】从 URL 捕获透传令牌（?token=）与升版深链（?doc_id=&owner=&title=）→ 暂存 + 立即抹除（修正#4）。
 * 关键时序：必须在 `@/router` 被 import（createWebHistory 读 location）【之前】执行 ——
 * 故由 `@/boot/capture` 作为 main.ts 第一个 import 触发。否则 router 在模块加载时就快照了带
 * token 的 URL，并在初始导航 finalize 时把这些参数写回地址栏（token 重新出现在历史/日志/截图）。
 * 此刻 Pinia 可能还没创建，所以只暂存到模块变量、不碰 store；token 由 doLogin 再注入 store。
 * 幂等：_captured 守卫 + 抹除后 URL 已无这些参数，重复调用 no-op（init 起始也会再调一次兜底）。
 */
export function captureUrlCredential(): void {
  if (_captured) return
  _captured = true
  const urlToken = qs('token')
  const docId = qs('doc_id')
  if (urlToken) { _stashedToken = urlToken; _capturedName = qs('name') }
  if (docId) _pendingVersion = { docId, owner: qs('owner'), title: qs('title') }   // 小程序升版深链
  if (urlToken || docId) scrubUrl(['token', 'name', 'doc_id', 'owner', 'title'])   // 先抹除，再发任何请求
  diag(`capture: token=${urlToken ? 'set' : '-'} pendingVer=${docId || '-'}`)
}

/** 是否有待处理的升版深链（App 据此在就绪后路由到 /manage）。不清除。 */
export function hasPendingVersion(): boolean { return !!_pendingVersion }
/** 取走升版深链（ManageView 加载文档后消费一次）。 */
export function consumePendingVersion(): PendingVersion | null {
  const p = _pendingVersion
  _pendingVersion = null
  return p
}

/**
 * 执行一次登录：
 *  ① 早捕获的透传 token（_stashedToken）→ 注入 store → whoami 取权威身份。
 *  ② 否则钉钉容器内 requestAuthCode → /api/auth/dingtalk 换证。
 * force=true（401 重登）跳过 ① 直接重走容器免登。
 */
async function doLogin(force: boolean): Promise<void> {
  const session = useSession()

  if (!force) {
    if (!session.token && _stashedToken) session.setToken(_stashedToken)
    if (session.token) {
      diag('login: URL 透传 token → /api/kb/whoami')
      const who = await apiJson<Record<string, any>>('/api/kb/whoami', { auth: true })
      session.setIdentity(toIdentity({ ...who, display_name: who.display_name || _capturedName }))
      return
    }
  }

  const corpId = qs('corpId') || qs('corpid') || CORP_ID_FALLBACK
  diag(`login: 容器免登 requestAuthCode（corpId=${corpId}${force ? ', 401 重登' : ''}）`)
  const code = await getAuthCode(corpId)
  diag('login: authCode 取得 → /api/auth/dingtalk 换证')
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
      // 设计预览：仅 dev（import.meta.env.DEV）且 URL 带 ?preview 时，注入 mock 管理员身份直接进 UI——
      // 无需钉钉容器/后端，纯看设计。生产构建里 DEV=false → 整段死代码消除，绝不进线上。
      if (import.meta.env.DEV && new URLSearchParams(window.location.search).has('preview')) {
        session.setToken('dev-preview')
        session.setIdentity(toIdentity({
          user_id: 'preview', display_name: '设计预览', role: 'kb_admin', can_manage_kb: true,
          acl_groups: ['marketing'], managed_owner_depts: ['marketing', 'hr', 'finance', 'production'],
        }))
        session.ready = true; session.error = ''
        diag('DEV ?preview：注入 mock 身份（无后端）')
        return
      }
      try {
        captureUrlCredential()   // 幂等兜底：若 main.ts 已捕获则 no-op
        await doLogin(false)
        session.ready = true
        session.error = ''
        diag(`login OK: role=${session.role} canManage=${session.canManage}`)
      } catch (e: any) {
        session.ready = false
        session.error = e?.message || '登录失败'
        diag(`login FAIL: ${e?.message || e}`)
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

/** 仅供测试：重置单次守卫 + 早捕获暂存。 */
export function __resetInitGuard() {
  _initPromise = null
  _captured = false
  _stashedToken = ''
  _capturedName = ''
  _pendingVersion = null
}
