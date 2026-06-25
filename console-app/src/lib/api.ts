import { useSession } from '@/stores/session'

export class ApiError extends Error {
  status: number
  detail: string
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = message
  }
}

// 401 重登回调由 useAuth 在启动时注入（避免 api ↔ useAuth 循环依赖）。
type Reauth = () => Promise<boolean>
let _reauth: Reauth | null = null
export function setReauthHandler(fn: Reauth | null) { _reauth = fn }

interface ApiOpts extends RequestInit {
  /** 默认 true：附带 Bearer。明确 false 时匿名（如 /api/hot-questions）。 */
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
  if (!res.ok) throw new ApiError((data && data.detail) || `HTTP ${res.status}`, res.status)
  return data as T
}
