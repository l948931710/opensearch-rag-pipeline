import { useSession } from '@/stores/session'

export class ApiError extends Error {
  status: number
  detail: string
  /** 秒；0 = 响应未带 Retry-After。后端限流拒绝（rate_limiter.Denial）恒带该头，
   *  上传/贡献侧据此出「请等 N 秒」而不是含糊的「稍后重试」。 */
  retryAfter: number
  constructor(message: string, status: number, retryAfter = 0) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = message
    this.retryAfter = retryAfter
  }
}

/** `Retry-After` 头 → 秒（只认 delta-seconds 形态；HTTP-date 形态与缺失均回 0）。
 *
 *  ⚠️ **全程 fail-safe，绝不抛**：本函数在 `apiJson` 的抛错路径上被调用，一旦它自己抛异常，
 *  非 2xx 就会变成 `TypeError` 而不是 `ApiError` —— 下游所有 `e.status === 403/404` 的
 *  分支会一起失效（404 静默兜底变成显错、403 文案退化成通用兜底）。加这层守卫时被
 *  既有单测当场抓住（mock 的 Response 没有 headers），这里留个记号别再摘掉。 */
function parseRetryAfter(res: Response): number {
  try {
    const raw = (res?.headers?.get?.('Retry-After') || '').trim()
    if (!/^\d+$/.test(raw)) return 0
    const n = Number(raw)
    return Number.isFinite(n) && n > 0 ? Math.min(n, 3600) : 0
  } catch { return 0 }
}

// 401 重登回调由 useAuth 在启动时注入（避免 api ↔ useAuth 循环依赖）。
type Reauth = () => Promise<boolean>
let _reauth: Reauth | null = null
export function setReauthHandler(fn: Reauth | null) { _reauth = fn }

interface ApiOpts extends RequestInit {
  /** 默认 true：有 token 即附 Bearer、无 token 自然匿名（后端各端点自带匿名兜底）。
   *  明确 false 仅用于**必须**匿名的调用（B5/P2-09 后 hot-questions 已改默认——
   *  带身份才有部门 cohort，auth:false 会把功能钉死在静态兜底）。 */
  auth?: boolean
}

function buildInit(opts: ApiOpts): RequestInit {
  const session = useSession()
  const headers = new Headers(opts.headers || {})
  if (opts.body != null && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (opts.auth !== false && session.token) headers.set('Authorization', `Bearer ${session.token}`)
  return { ...opts, headers }
}

/**
 * 同源 fetch + Bearer。401 且尚未重试过 → 触发一次重登再重试（仅一次，防循环）。
 * 返回原始 Response（流式/SSE 调用方自取 body）。
 */
export async function apiFetch(path: string, opts: ApiOpts = {}, _retried = false): Promise<Response> {
  // DEV 设计预览（?preview，token 哨兵 'dev-preview'）契约 = 无后端、纯看设计。带 auth 的请求若真打网络会被
  // vite 代理到 FastAPI 拿 401 → 触发 reauth → 清掉哨兵 token → 各 loader 的 dev-preview mock 分支
  //（判 token==='dev-preview'）随之失效、数据区段全空。故带 auth 的预览请求直接合成 503 短路：不打网络、
  // 不重登、不动 token。有 mock 分支的 loader 早在 token 判定处短路、不会到这里；无 mock 分支的安静兜底空。
  // prod 构建 DEV=false → 整段死代码消除。
  if (import.meta.env.DEV && opts.auth !== false && useSession().token === 'dev-preview') {
    return new Response(JSON.stringify({ detail: 'dev-preview: 无后端' }),
      { status: 503, headers: { 'Content-Type': 'application/json' } })
  }
  const res = await fetch(path, buildInit(opts))
  if (res.status === 401 && !_retried && _reauth) {
    const ok = await _reauth()
    if (ok) return apiFetch(path, opts, true)
  }
  return res
}

/** JSON 调用：非 2xx 抛 ApiError（带 status + 后端 detail）。 */
export async function apiJson<T = any>(path: string, opts: ApiOpts = {}): Promise<T> {
  const res = await apiFetch(path, opts)
  let data: any = null
  try { data = await res.json() } catch { /* 非 JSON 响应 */ }
  if (!res.ok) throw new ApiError((data && data.detail) || `HTTP ${res.status}`, res.status, parseRetryAfter(res))
  return data as T
}
