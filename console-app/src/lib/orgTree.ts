import type { OrgNode } from '@/composables/useOrgSnapshot'

/**
 * 组织树选择的共享计算（OrgTreePicker 展开态与 OrgTreeSelect 收起态复用同一逻辑——
 * 审计结论：coverHint/uncoveredDirect 是"完全无声的坑"的显性化，**收起态不许丢**，
 * 且两处必须同一公式，不能各写一份漂移）。
 */
export interface PickedNode { dept_id: number; subtree: boolean }

export const directOf = (n: OrgNode) => n.direct_staff_count ?? 0

/** 已选节点覆盖的总人数(仅供概览,不去重跨节点重叠)。
 *  含下级 = d:<id> → 子树数 staff_count；仅本级 = dx:<id> → 直挂数 direct_staff_count。 */
export function coverCount(picked: PickedNode[], byId: Map<number, OrgNode>): number {
  return picked.reduce((s, p) => {
    const node = byId.get(p.dept_id)
    if (!node) return s
    return s + (p.subtree ? node.staff_count : directOf(node))
  }, 0)
}

/** 「勾了子部门、却漏掉父节点直挂的人」——人数估算看不出来，必须显式点名。 */
export function uncoveredDirectParents(
  picked: PickedNode[], byId: Map<number, OrgNode>,
): { name: string; n: number }[] {
  if (!picked.length) return []
  const pickedMap = new Map(picked.map((p) => [p.dept_id, p]))
  // 已被覆盖 = 该节点本身被勾（任一模式），或它的某个祖先被勾且是「含下级」
  const covered = (id: number): boolean => {
    let cur: number | undefined = id
    let hops = 0
    while (cur && hops++ < 20) {
      const p = pickedMap.get(cur)
      if (p && (cur === id || p.subtree)) return true
      cur = byId.get(cur)?.parent_id
    }
    return false
  }
  const out: { name: string; n: number }[] = []
  for (const p of picked) {
    const node = byId.get(p.dept_id)
    if (!node) continue
    const par = byId.get(node.parent_id)
    if (!par || directOf(par) === 0 || covered(par.dept_id)) continue
    if (!out.some((o) => o.name === par.name)) out.push({ name: par.name, n: directOf(par) })
  }
  return out
}

// ── 看板归属轴（2026-08-03 重设计,codex v3 共识）────────────────────────────────

/** 归属桶稳定键的 kind 四态。 */
export type OwnerBucketKind = 'node' | 'node_missing' | 'legacy' | 'unknown'

/** 归属桶键 → 展示名（kind-aware,codex 终稿契约）：
 *  unknown → 「未归属」（忽略 owner_label——后端原样回 'unknown'）；
 *  legacy  → deptLabel(key)（调用方传入映射函数,避免 lib 依赖 kb.ts 造环）；
 *  node    → 有效 owner_label → 快照名 → `#<id> ⚠️`。
 *  「有效」排除：空串、等于 key 本身（'node:<id>'）、纯数字串（_kb_node_names 缺行回退值）。 */
export function resolveOwnerBucket(
  key: string, ownerLabel: string | undefined, byId: Map<number, OrgNode>,
  legacyLabel: (code: string) => string,
): { label: string; kind: OwnerBucketKind; nodeId: number | null } {
  const k = (key || '').trim()
  if (!k || k === 'unknown') return { label: '未归属', kind: 'unknown', nodeId: null }
  const m = /^node:([1-9]\d*)$/.exec(k)
  if (!m) return { label: legacyLabel(k), kind: 'legacy', nodeId: null }
  const id = Number(m[1])
  const raw = (ownerLabel || '').trim()
  const meaningful = raw && raw !== k && !/^\d+$/.test(raw) ? raw : ''
  const snapName = byId.get(id)?.name ?? ''
  if (meaningful) return { label: meaningful, kind: 'node', nodeId: id }
  if (snapName) return { label: snapName, kind: 'node', nodeId: id }
  return { label: `#${id} ⚠️`, kind: 'node_missing', nodeId: id }
}

/** 文档归属 DTO → 展示名（台账/队列用；与看板 `resolveOwnerBucket` **同一口径**）。
 *
 *  ⚠️ 两处的稳定键形状**刻意不同**，别把它们合并：
 *    · 看板覆盖行 `KbDeptCoverageItem.owner_dept` = 裸组码 | `node:<id>`
 *    · 文档行     `KbDocItem.owner_key`          = `legacy:<code>` | `node:<id>`
 *  故这里只做键形归一再委托，不去放宽 resolveOwnerBucket 的正则（放宽会让
 *  `legacy:hr` 这种串在看板侧也被当成合法裸组码喂给 deptLabel，显示成 'legacy:hr'）。
 *
 *  `byId` 传空 Map 是**正常用法**：node 节点名后端已 JOIN dept_dim 直给（owner_label），
 *  台账无须为了显示再拉一次组织快照。快照只在看板侧有额外价值（做中心卷积）。
 *
 *  兼容旧响应：`owner_key` 缺失时回落裸 `owner_dept`（legacy 文档恒有值；
 *  node 文档该字段按后端契约为空 ⇒ 落「未归属」，如实反映"这个前端版本读不懂它"）。 */
export function resolveDocOwner(
  ownerKey: string | undefined, ownerDept: string | undefined,
  ownerLabel: string | undefined, byId: Map<number, OrgNode>,
  legacyLabel: (code: string) => string,
): { label: string; kind: OwnerBucketKind; nodeId: number | null } {
  const k = (ownerKey || '').trim()
  const norm = k.startsWith('legacy:') ? k.slice('legacy:'.length) : (k || (ownerDept || '').trim())
  return resolveOwnerBucket(norm, ownerLabel, byId, legacyLabel)
}

/** 节点 → 最近 depth<=1 祖先（=中心,org_sync 契约:中心 depth=1、无公司根行）。
 *  自身 depth<=1 即自身;快照缺链/超 20 跳 → null（调用方按顶层独立行处理,不猜中心）。 */
export function centerOf(id: number, byId: Map<number, OrgNode>): number | null {
  let cur = byId.get(id)
  let hops = 0
  while (cur && hops++ < 20) {
    if (cur.depth <= 1) return cur.dept_id
    cur = byId.get(cur.parent_id)
  }
  return null
}

/** 覆盖行（后端 dept_coverage 形状的最小子集——文档类可加指标 + 桶内去重指标）。 */
export interface CoverageBucket {
  owner_dept: string; owner_label?: string
  docs: number; new_month: number; pii_docs: number
  qa_hits: number; no_answer_rate: number
  wow_net?: number | null; qa_hits_7d?: number | null
  qa_wow_net?: number | null; qa_wow?: number | null
}

export interface CoverageTreeRow {
  key: string; label: string; kind: OwnerBucketKind | 'center'
  /** 可加指标（中心行=本级+子桶求和;文档各有唯一归属,可加） */
  docs: number; new_month: number; pii_docs: number; wow_net: number | null
  /** 桶内 COUNT(DISTINCT message_id) 去重指标——**跨桶不可加**（同一提问可命中多部门）。
   *  中心行恒 null（诚实「—」,绝不求和造数）;桶行为真实值。 */
  qa_hits: number | null; qa_hits_7d: number | null
  qa_wow_net: number | null; qa_wow: number | null; no_answer_rate: number | null
  children: CoverageTreeRow[]
  /** 中心行下的「（本级）」子行标记（中心自身持桶时,保证子行合计=父行） */
  selfRow?: boolean
}

/** 覆盖桶 → 中心卷积树。node 桶沿父链卷到中心;中心缺链/legacy/unknown = 顶层独立行;
 *  快照 unavailable（byId 空）⇒ 调用方应直接平铺,不用本函数装树。 */
export function rollupCoverageRows(
  rows: CoverageBucket[], byId: Map<number, OrgNode>,
  legacyLabel: (code: string) => string,
): CoverageTreeRow[] {
  const centers = new Map<number, CoverageTreeRow>()
  const flat: CoverageTreeRow[] = []
  const bucketRow = (r: CoverageBucket, res: ReturnType<typeof resolveOwnerBucket>, self = false): CoverageTreeRow => ({
    key: r.owner_dept, label: self ? `${res.label}（本级）` : res.label, kind: res.kind,
    docs: r.docs, new_month: r.new_month, pii_docs: r.pii_docs, wow_net: r.wow_net ?? null,
    qa_hits: r.qa_hits, qa_hits_7d: r.qa_hits_7d ?? null,
    qa_wow_net: r.qa_wow_net ?? null, qa_wow: r.qa_wow ?? null, no_answer_rate: r.no_answer_rate,
    children: [], selfRow: self,
  })
  for (const r of rows) {
    const res = resolveOwnerBucket(r.owner_dept, r.owner_label, byId, legacyLabel)
    const cid = res.kind === 'node' ? centerOf(res.nodeId!, byId) : null
    if (cid == null) { flat.push(bucketRow(r, res)); continue }
    let c = centers.get(cid)
    if (!c) {
      c = {
        key: `center:${cid}`, label: byId.get(cid)?.name ?? `#${cid}`, kind: 'center',
        docs: 0, new_month: 0, pii_docs: 0, wow_net: 0,
        qa_hits: null, qa_hits_7d: null, qa_wow_net: null, qa_wow: null, no_answer_rate: null,
        children: [],
      }
      centers.set(cid, c)
    }
    c.docs += r.docs; c.new_month += r.new_month; c.pii_docs += r.pii_docs
    c.wow_net = (c.wow_net ?? 0) + (r.wow_net ?? 0)
    c.children.push(bucketRow(r, res, res.nodeId === cid))
  }
  for (const c of centers.values()) c.children.sort((a, b) => b.docs - a.docs)
  return [...centers.values(), ...flat].sort((a, b) => b.docs - a.docs)
}

/** 管辖根 → 最小根集（剔除被另一允许根以祖先身份覆盖的后代根,防同一子树重复渲染；
 *  同时剔除不在快照中的 id）。null = 不过滤。 */
export function normalizeAllowedRoots(
  allowed: number[] | null | undefined, byId: Map<number, OrgNode>,
): Set<number> | null {
  if (allowed == null) return null
  const present = allowed.filter((id) => byId.has(id))
  const s = new Set(present)
  const minimal = present.filter((id) => {
    let cur = byId.get(id)?.parent_id
    let hops = 0
    while (cur && hops++ < 20) {
      if (s.has(cur)) return false
      cur = byId.get(cur)?.parent_id
    }
    return true
  })
  return new Set(minimal)
}
