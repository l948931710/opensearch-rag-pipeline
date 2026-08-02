import { apiJson } from '@/lib/api'

/**
 * 组织快照(`/api/kb/org-tree`)的进程内缓存。
 *
 * ⚠️ 为什么必须放在**独立模块**而不是 `OrgTreePicker.vue` 里:
 * `<script setup>` 的全部顶层代码都会被编译进 `setup()` 函数体,所以写在 SFC 里的
 * `let cache = null` 是**每个组件实例的局部变量**,弹窗每开一次就重置一次 —— 2026-08-01
 * 实测连开三次弹窗打了三次请求,缓存形同虚设。模块级作用域只有真·`.ts` 模块(或另开一个
 * 普通 `<script>` 块)才有。同理,`<script setup>` 里也不允许 `export`。
 *
 * 该端点要过 `_require_kb_console`(一次 RDS 往返,本机实测 ~1s),而组织快照是每日同步
 * 一次的准静态数据,一次会话里反复拉毫无意义。TTL 取 5 分钟,与服务端 `RAG_ORG_TREE_TTL_S`
 * 同量级;`synced_at` 不必即时反映(同步是每日作业,快照 >48h 才 fail-closed)。
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

export interface OrgSnapshot {
  nodes: OrgNode[]
  stale: boolean
  synced_at: string
}

const ORG_TTL_MS = 5 * 60 * 1000
let orgCache: { at: number; snap: OrgSnapshot } | null = null
let orgInflight: Promise<OrgSnapshot> | null = null

export async function fetchOrgSnapshot(): Promise<OrgSnapshot> {
  if (orgCache && Date.now() - orgCache.at < ORG_TTL_MS) return orgCache.snap
  // 并发去重:多个实例同时挂载时共享同一个 in-flight promise,不打重复请求
  if (orgInflight) return orgInflight
  orgInflight = (async () => {
    const r = await apiJson<any>('/api/kb/org-tree')
    const t = r?.org_tree || {}
    const snap: OrgSnapshot = {
      nodes: (t.nodes || []) as OrgNode[],
      stale: !!t.stale,
      synced_at: t.synced_at || '',
    }
    orgCache = { at: Date.now(), snap }
    return snap
  })()
  // 失败不落缓存(orgCache 只在成功分支写),下次挂载会重试
  try {
    return await orgInflight
  } finally {
    orgInflight = null
  }
}

/** 组织同步刚跑完 / 测试之间复位时调用 */
export function invalidateOrgSnapshot(): void {
  orgCache = null
}
