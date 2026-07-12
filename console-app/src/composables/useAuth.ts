import { useSession, toIdentity } from '@/stores/session'
import { apiJson } from '@/lib/api'
import { diag } from '@/lib/diag'
import { syncHistoryForUser } from '@/composables/useAsk'
import { __resetIdentityScope, syncIdentityScope } from '@/composables/identityScope'

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

// ── 桌面 ?token 刷新续存（sessionStorage，tab 级）──
// #F-console-urltoken 抹除 URL 后 token 只活在内存 → 桌面浏览器（无钉钉容器可重登）一刷新即
// 永久掉线且无恢复路径（唯一出路=讨新链接）。解法：URL 摄取时同步存 sessionStorage，刷新时恢复。
// 威胁模型不变差：不进 URL/历史/日志/截图/Referer（抹除照旧）；XSS 可读面与内存 token 等同；
// tab 关闭即清、不跨 tab；token 本身由服务端签名+TTL 兜底。401 时清除，防过期 token 死循环。
// 钉钉容器内不依赖此续存（dd 免登可随时重走），容器换证 token 不落存储（少存一处密钥）。
const SS_TOKEN_KEY = 'rag.console.token'
function persistToken(t: string) { try { sessionStorage.setItem(SS_TOKEN_KEY, t) } catch { /* 隐私模式等：降级为内存单次 */ } }
function clearPersistedToken() { try { sessionStorage.removeItem(SS_TOKEN_KEY) } catch { /* 同上 */ } }
function restorePersistedToken(): string { try { return sessionStorage.getItem(SS_TOKEN_KEY) || '' } catch { return '' } }

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
  if (urlToken) { _stashedToken = urlToken; _capturedName = qs('name'); persistToken(urlToken) }
  else { _stashedToken = restorePersistedToken() }   // 刷新路径：URL 已被抹除 → 从 tab 级续存恢复
  if (docId) _pendingVersion = { docId, owner: qs('owner'), title: qs('title') }   // 小程序升版深链
  if (urlToken || docId) scrubUrl(['token', 'name', 'doc_id', 'owner', 'title'])   // 先抹除，再发任何请求
  // #F-console-urltoken 保守加固：?token= 摄取【不可删】——钉钉容器外的桌面浏览器（index.html 无条件载入
  //   dingtalk SDK，但 requestAuthCode 仅真容器内 dd.ready 触发、桌面必超时失败）此路是【唯一】可登录路径
  //   （见 kb-upload.onWvError「请在电脑端浏览器打开」）。删摄取即破坏桌面登录。仅告警以便排查残留生产者。
  if (urlToken) console.warn('[sec] #F-console-urltoken URL 透传 token 已即时抹除；若非预期请排查链接生产者')
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
      diag('login: URL 透传/续存 token → /api/kb/whoami')
      try {
        const who = await apiJson<Record<string, any>>('/api/kb/whoami', { auth: true })
        session.setIdentity(toIdentity({ ...who, display_name: who.display_name || _capturedName }))
        syncIdentityScope()   // P0-D：身份落定即对账——上个身份的注册 store（审批队列等）同步清空
        return
      } catch (e: any) {
        // token 失效（401）：清内存+续存后【落穿】到容器免登——钉钉内无感重登；桌面（无容器）
        // 走到超时报错，且续存已清，下次刷新不再拿同一枚死 token 空转。非 401（网络等）照旧上抛。
        if (e?.status !== 401) throw e
        session.setToken(''); _stashedToken = ''; clearPersistedToken()
        syncIdentityScope()   // P0-D：token 已证失效即对账（fail-closed：下一身份未知，先清旧缓存）
        diag('login: token 失效(401) → 清续存，回退容器免登')
      }
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
  syncIdentityScope()   // P0-D：容器免登换证落定即对账（换号/角色变更 → 旧身份缓存同步清空）
}

export function useAuth() {
  const session = useSession()

  /** 唯一启动入口（单次）。重复调用返回同一 Promise，绝不二次触发免登。 */
  function init(): Promise<void> {
    if (_initPromise) return _initPromise
    _initPromise = (async () => {
      // 设计预览：仅 dev（import.meta.env.DEV）且 URL 带 ?preview 时，注入 mock 身份直接进 UI——
      // 无需钉钉容器/后端，纯看设计。?preview=employee 员工只读视图；?preview=dept 部门管理员；
      // 其余（?preview / ?preview=kb）按知识库管理员。生产构建里 DEV=false → 整段死代码消除，绝不进线上。
      const _pv = new URLSearchParams(window.location.search)
      if (import.meta.env.DEV && _pv.has('preview')) {
        const which = _pv.get('preview')   // ''/'kb' → kb_admin；'dept' → dept_admin；'employee' → 员工
        const mock = which === 'employee'
          ? { user_id: 'preview', display_name: '设计预览·员工', role: 'employee', can_manage_kb: false, acl_groups: ['marketing'], managed_owner_depts: [] }
          : which === 'dept'
            ? { user_id: 'preview', display_name: '设计预览·部门管理员', role: 'dept_admin', can_manage_kb: true, acl_groups: ['marketing', 'production'], managed_owner_depts: ['marketing'] }
            : { user_id: 'preview', display_name: '设计预览', role: 'kb_admin', can_manage_kb: true, acl_groups: ['marketing'], managed_owner_depts: ['marketing', 'hr', 'finance', 'production'] }
        session.setToken('dev-preview')
        session.setIdentity(toIdentity(mock))
        syncIdentityScope()   // P0-D：预览身份同样对账（?preview=kb→dept 切换也不残留）
        session.ready = true; session.error = ''
        diag(`DEV ?preview：注入 mock 身份（${which || 'kb_admin'}，无后端）`)
        return
      }
      try {
        captureUrlCredential()   // 幂等兜底：若 main.ts 已捕获则 no-op
        await doLogin(false)
        session.ready = true
        session.error = ''
        syncHistoryForUser(session.identity?.userId || '')   // 共享设备：清掉他人残留的本地会话历史
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
    // dev-preview 哨兵：无钉钉容器，重登必失败，且【绝不能】清掉哨兵 token——否则各 loader 的预览 mock 分支
    //（判 token==='dev-preview'）失效、?preview 数据区段全空。直接返回 false，不清不重登。
    // import.meta.env.DEV 前缀：prod 构建 DEV=false → 整句死代码消除（与 apiFetch 短路一致，不进发布包）。
    if (import.meta.env.DEV && session.token === 'dev-preview') return false
    try {
      session.setToken('')
      clearPersistedToken()   // 旧 token 已证失效：续存一并清，防刷新捡回死 token
      // P0-D：登出时点先对账——旧身份的注册 store（审批队列/台账/看板等）此刻同步清空，
      // 重登失败也不残留（fail-closed）；重登成功后 doLogin 内部会再对账一次落定新身份。
      syncIdentityScope()
      await doLogin(true)
      syncHistoryForUser(session.identity?.userId || '')   // 重登为不同用户时清掉前者残留
      return !!session.token
    } catch {
      return false
    }
  }

  return { init, reauth }
}

/** 仅供测试：重置单次守卫 + 早捕获暂存 + tab 级 token 续存 + identityScope 已观测身份。 */
export function __resetInitGuard() {
  _initPromise = null
  _captured = false
  _stashedToken = ''
  _capturedName = ''
  _pendingVersion = null
  clearPersistedToken()
  __resetIdentityScope()
}
