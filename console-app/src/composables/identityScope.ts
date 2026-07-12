import { useSession } from '@/stores/session'

// 身份作用域 store 注册中心（P0-D「审批状态跨登录身份复用」修复）。
// 问题形态：composable 的模块级单例状态挂在 SPA 生命周期上，与登录身份无关——管理员 A 登出、
// B 登录后，B 在 30s freshness 窗口内能看到 A 的审批队列（pending tool/args/requester/scope）。
// d4eb5d4 只给 useOntology 加了跨身份重置，本文件把该模式泛化为全 composable 共享设施：
//
// 规则：凡身份作用域的模块级状态一律注册到这里；身份键（userId + role + ACL 版本 + token）
// 一变，统一【同步】清空全部注册 store——禁止任何 composable 再自搞独立的跨身份 singleton。
// 身份信号两路，互为兜底：
//  · eager —— useAuth 在 token/身份转换点（登录落定 / 401 清 token / 重登起点）显式调
//    syncIdentityScope()，登出瞬间即清（fail-closed：重登失败也不残留旧身份数据）；
//  · lazy —— 各 store 的 loader 入口先调 syncIdentityScope() 再动缓存/freshness 门，
//    即使未来出现绕过 useAuth 的身份变更路径，读取时也会先自检。
// 在途响应用 identityFingerprint()（身份指纹，不含 token）判废：loader 在 await 前取指纹、
// await 后比对，身份变了整体丢弃——防 A 的慢响应落进 B 的视图。指纹不含 token 是有意的：
// 同用户 401 重登（apiFetch 自动重试）拿回的仍是本人数据，不应误废。

export interface IdentitySwitch {
  prevUserId: string
  nextUserId: string
}

type ResetFn = (sw: IdentitySwitch) => void

const _stores: { name: string; reset: ResetFn }[] = []

// NUL(\u0000) 作拼接分隔符（与 useAsk 搜索缓存同法）：字段值不可能含 NUL，杜绝拼接歧义。
const SEP = '\u0000'

let _lastKey: string | null = null   // null=尚未观测到任何身份（首见只采纳，不触发清空）
let _lastUserId = ''                 // 上次观测到的 userId（给重置回调判"是否真换了人"）

/** 身份指纹：authenticated user ID + role + ACL 版本（组/可管部门排序拼接，顺序无关）。
 *  不含 token——供在途响应判废（同用户换 token 不废）。Pinia 未激活 → null。 */
export function identityFingerprint(): string | null {
  try {
    const s = useSession()
    const i = s.identity
    return [
      i?.userId ?? '',
      i?.role ?? '',
      [...(i?.aclGroups ?? [])].sort().join(','),
      [...(i?.managedOwnerDepts ?? [])].sort().join(','),
    ].join(SEP)
  } catch { return null }   // Pinia 尚未激活（极早期/测试外壳）：无从比较
}

/** 完整身份键 = 指纹 + token：token 变化（登出/重登/换证）也触发清空（审计验收标准 1）。 */
function currentIdentityKey(): string | null {
  const fp = identityFingerprint()
  if (fp === null) return null
  try { return fp + SEP + useSession().token } catch { return null }
}

/** 注册一个身份作用域 store 的清空函数（模块加载即注册，app 生命周期内不注销）。
 *  回调收到 {prevUserId, nextUserId}——绝大多数 store 无条件清空（忽略参数）；
 *  个别 store（如 useAsk 的本地会话历史）需要区分「换人」与「同人换 token/角色」。 */
export function registerIdentityScopedStore(name: string, reset: ResetFn): void {
  _stores.push({ name, reset })   // name 仅作自文档（排障时断点看 _stores）
}

/**
 * 对账身份键：变了 → 【同步】清空全部注册 store。首见（_lastKey=null）只采纳不清空——
 * 此刻不存在"上个身份"的数据（各 loader 入口本身就先对账，写入必然发生在采纳之后）。
 * 单个 store 清空抛错不拖累其它 store（该 store 由其 loader 的 lazy 对账再兜）。
 */
export function syncIdentityScope(): void {
  const key = currentIdentityKey()
  if (key === null) return
  if (_lastKey === null) {
    _lastKey = key
    _lastUserId = keyUserId(key)
    return
  }
  if (key === _lastKey) return
  const sw: IdentitySwitch = { prevUserId: _lastUserId, nextUserId: keyUserId(key) }
  for (const st of _stores) {
    try { st.reset(sw) } catch { /* 见函数注释 */ }
  }
  // 注意放在回调【之后】：各 __resetX（测试复位）会顺带调 __resetIdentityScope() 抹掉观测，
  // 这里最后覆写，保证运行期连续切换（A→B→C）每一跳都触发清空。
  _lastKey = key
  _lastUserId = sw.nextUserId
}

function keyUserId(key: string): string { return key.split(SEP, 1)[0] }

/** 仅供测试：忘掉已观测身份（下次 sync 重新首见采纳，不误清测试预置状态）。 */
export function __resetIdentityScope(): void {
  _lastKey = null
  _lastUserId = ''
}
