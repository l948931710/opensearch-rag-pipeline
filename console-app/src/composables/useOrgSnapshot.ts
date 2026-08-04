import { apiJson } from '@/lib/api'
import { useSession } from '@/stores/session'

/**
 * 组织快照(`/api/kb/org-tree`)的进程内缓存。
 *
 * ⚠️ 为什么必须放在**独立模块**而不是 `OrgTreePicker.vue` 里:
 * `<script setup>` 的全部顶层代码都会被编译进 `setup()` 函数体,所以写在 SFC 里的
 * `let cache = null` 是**每个组件实例的局部变量**,弹窗每开一次就重置一次 —— 2026-08-01
 * 实测连开三次弹窗打三次请求,缓存形同虚设。模块级作用域只有真·`.ts` 模块(或另开一个
 * 普通 `<script>` 块)才有。同理,`<script setup>` 里也不允许 `export`。
 *
 * 该端点要过 `_require_kb_console`(一次 RDS 往返,本机实测 ~1s),而组织快照是每日同步
 * 一次的准静态数据,一次会话里反复拉毫无意义。TTL 取 5 分钟,与服务端 `RAG_ORG_TREE_TTL_S`
 * 同量级;`synced_at` 不必即时反映(同步是每日作业,快照 >48h 才 fail-closed)。
 *
 * 【身份分区 2026-08-03】响应从纯全局数据（org 树）扩到含**用户级授权**
 * (`my_managed_node_roots`)后,缓存必须随 principal 走:401 落穿容器免登可能换人
 * (useAuth.reauth 的 syncHistoryForUser 同款场景),A 的管辖根绝不能喂给 B。
 * 分区键 = session.principalEpoch(userId 真变化即递增)+ userId 双保险
 * (测试里每 case 重建 Pinia,epoch 会从 0 重来,单靠它会误命中上个 store 的缓存)。
 * 响应返回后**复核 epoch**:期间换人 ⇒ 丢弃本响应、不落缓存,为新身份重取一次(单层防环)。
 */
export interface OrgNode {
  dept_id: number
  parent_id: number
  name: string
  depth: number
  /** 子树总人数（本节点直属 + 全部后代）—— 对应「含下级」= d:<id> */
  staff_count: number
  /** 仅直挂本节点的人数 —— 对应「仅本级」= dx:<id>。旧后端无此字段，按 0 兜底 */
  direct_staff_count?: number
}

/** 组织树本体的可用性（B3 共识）:
 *  - unavailable: org_tree=null（读失败被 200 吞掉）或 nodes 为空（DB 失败/空表,服务端
 *    此时恒带 stale=true）——**没有可信的树**,不落缓存、可重试;
 *  - stale: 有树但快照 >48h——后端管辖展开会 fail-closed,只可看不可信;
 *  - fresh: 正常。零管辖根的断言、归属自动预填都只在 fresh 下成立。 */
export type OrgStatus = 'fresh' | 'stale' | 'unavailable'

export interface OrgSnapshot {
  nodes: OrgNode[]
  stale: boolean
  synced_at: string
  status: OrgStatus
  /** 调用者的 node 管辖根三态:null=字段缺失（旧后端,unknown≠空授权）；[]=真·零授权；
   *  非空=管辖根列表。「缺键=unknown≠空集」——同 asset-diff 的仓规教训。 */
  my_managed_node_roots: number[] | null
}

const ORG_TTL_MS = 5 * 60 * 1000
let orgCache: { at: number; epoch: number; userId: string; snap: OrgSnapshot } | null = null
let orgInflight: { epoch: number; userId: string; p: Promise<OrgSnapshot> } | null = null

function parseSnapshot(r: any): OrgSnapshot {
  const t = r?.org_tree
  const nodes = (Array.isArray(t?.nodes) ? t.nodes : []) as OrgNode[]
  const status: OrgStatus = !t || nodes.length === 0 ? 'unavailable' : t.stale ? 'stale' : 'fresh'
  const roots = Array.isArray(r?.my_managed_node_roots)
    ? (r.my_managed_node_roots as unknown[]).filter(
        (v): v is number => Number.isInteger(v) && (v as number) > 0)
    : null
  return {
    nodes,
    stale: !!t?.stale,
    synced_at: t?.synced_at || '',
    status,
    my_managed_node_roots: roots,
  }
}

export async function fetchOrgSnapshot(_retried = false): Promise<OrgSnapshot> {
  const session = useSession()
  const epoch = session.principalEpoch
  const userId = session.identity?.userId ?? ''
  const hit = orgCache
  if (hit && hit.epoch === epoch && hit.userId === userId && Date.now() - hit.at < ORG_TTL_MS) {
    return hit.snap
  }
  // 并发去重:同一 principal 的多个实例共享同一 in-flight promise
  if (orgInflight && orgInflight.epoch === epoch && orgInflight.userId === userId) {
    return orgInflight.p
  }
  const entry: { epoch: number; userId: string; p: Promise<OrgSnapshot> } = {
    epoch, userId, p: undefined as unknown as Promise<OrgSnapshot>,
  }
  entry.p = (async () => {
    try {
      const r = await apiJson<any>('/api/kb/org-tree')
      // 复核 principal:请求期间 401 重登换了人 ⇒ 本响应身份归属不明,丢弃不缓存,
      // 为当前身份重取一次;再换人则上抛（单层防环）。
      if (useSession().principalEpoch !== epoch) {
        if (_retried) throw new Error('身份已切换，组织数据请重新加载')
        return fetchOrgSnapshot(true)
      }
      const snap = parseSnapshot(r)
      // unavailable 不落缓存——留给下次挂载真重试,不把故障态钉 5 分钟
      if (snap.status !== 'unavailable') {
        orgCache = { at: Date.now(), epoch, userId, snap }
      }
      return snap
    } finally {
      // 只清自己:换人后新 principal 可能已建立新 in-flight,不得误清
      if (orgInflight === entry) orgInflight = null
    }
  })()
  orgInflight = entry
  return entry.p
}

/** 组织同步刚跑完 / 测试之间复位时调用 */
export function invalidateOrgSnapshot(): void {
  orgCache = null
  orgInflight = null
}
