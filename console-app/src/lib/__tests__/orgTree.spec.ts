import { describe, expect, it } from 'vitest'
import { centerOf, resolveDocOwner, resolveOwnerBucket, rollupCoverageRows, type CoverageBucket } from '@/lib/orgTree'
import { deptLabel } from '@/lib/kb'
import type { OrgNode } from '@/composables/useOrgSnapshot'

/** 生产形状（org_sync 契约：中心=depth 1、无公司根行）。 */
const NODES: OrgNode[] = [
  { dept_id: 2, parent_id: 1, name: '生产中心', depth: 1, staff_count: 800, direct_staff_count: 4 },
  { dept_id: 3, parent_id: 2, name: '注塑事业部', depth: 2, staff_count: 300, direct_staff_count: 8 },
  { dept_id: 4, parent_id: 3, name: '注塑一车间', depth: 3, staff_count: 120, direct_staff_count: 120 },
  { dept_id: 6, parent_id: 1, name: '质量中心', depth: 1, staff_count: 60, direct_staff_count: 60 },
]
const byId = new Map(NODES.map((n) => [n.dept_id, n]))
const legacy = (c: string) => ({ production: '生产', marketing: '市场' }[c] ?? c)

describe('resolveOwnerBucket — kind-aware 名称契约（codex 终稿）', () => {
  it('node + 裸数字 owner_label + 快照有名 → 快照名', () => {
    expect(resolveOwnerBucket('node:3', '123', byId, legacy))
      .toEqual({ label: '注塑事业部', kind: 'node', nodeId: 3 })
  })
  it('node + 裸数字 label + 快照缺 → #id ⚠️（node_missing）', () => {
    expect(resolveOwnerBucket('node:99', '99', byId, legacy))
      .toEqual({ label: '#99 ⚠️', kind: 'node_missing', nodeId: 99 })
  })
  it('node + 有效 label（快照缺也用）；label=key 本身/空串视为无效', () => {
    expect(resolveOwnerBucket('node:99', '纸箱事业部', byId, legacy).label).toBe('纸箱事业部')
    expect(resolveOwnerBucket('node:99', 'node:99', byId, legacy).label).toBe('#99 ⚠️')
    expect(resolveOwnerBucket('node:99', '', byId, legacy).label).toBe('#99 ⚠️')
  })
  it('resolveDocOwner：文档 DTO 的 legacy:<code> 键形归一后仍走 deptLabel', () => {
    // 台账/队列的 owner_key 带 `legacy:` 前缀，看板覆盖行的桶键是裸组码——两处刻意不同源。
    // 若直接把 owner_key 喂给 resolveOwnerBucket，会显示成字面量 'legacy:hr'。
    expect(resolveDocOwner('legacy:hr', 'hr', '', new Map(), deptLabel).label).toBe('人力资源')
    expect(resolveDocOwner('legacy:hr', 'hr', '', new Map(), deptLabel).kind).toBe('legacy')
  })
  it('resolveDocOwner：node 文档 owner_dept 为空 → 用 owner_label（后端 JOIN dept_dim 直给）', () => {
    // 这正是 2026-08-04 首篇 node 文档在台账「归属」列显示为空格子的那一格。
    const r = resolveDocOwner('node:34265162', '', '人力资源部', new Map(), deptLabel)
    expect(r.label).toBe('人力资源部')
    expect(r.kind).toBe('node')
    expect(r.nodeId).toBe(34265162)
  })
  it('resolveDocOwner：owner_key 缺失（旧响应）→ 回落裸 owner_dept；两者皆空 → 未归属', () => {
    expect(resolveDocOwner(undefined, 'marketing', '', new Map(), deptLabel).label).toBe('营销')
    expect(resolveDocOwner(undefined, '', '', new Map(), deptLabel).label).toBe('未归属')
  })
  it('resolveDocOwner：node 但 label 退化成裸 id 且无快照 → #id ⚠️（不假装知道名字）', () => {
    const r = resolveDocOwner('node:99', '', '99', new Map(), deptLabel)
    expect(r.label).toBe('#99 ⚠️')
    expect(r.kind).toBe('node_missing')
  })
  it('legacy → deptLabel；unknown/空串 → 未归属（忽略 owner_label）', () => {
    expect(resolveOwnerBucket('production', 'production', byId, legacy).label).toBe('生产')
    expect(resolveOwnerBucket('unknown', 'unknown', byId, legacy))
      .toEqual({ label: '未归属', kind: 'unknown', nodeId: null })
    expect(resolveOwnerBucket('', undefined, byId, legacy).kind).toBe('unknown')
  })
})

describe('centerOf — 最近 depth<=1 祖先', () => {
  it('深层沿链上溯；中心自身即自身；缺链 → null', () => {
    expect(centerOf(4, byId)).toBe(2)
    expect(centerOf(3, byId)).toBe(2)
    expect(centerOf(6, byId)).toBe(6)
    expect(centerOf(99, byId)).toBeNull()
  })
  it('成环不死循环（hop 上限）', () => {
    const loop = new Map<number, OrgNode>([
      [8, { dept_id: 8, parent_id: 9, name: 'a', depth: 5, staff_count: 0 }],
      [9, { dept_id: 9, parent_id: 8, name: 'b', depth: 5, staff_count: 0 }],
    ])
    expect(centerOf(8, loop)).toBeNull()
  })
})

const BUCKETS: CoverageBucket[] = [
  { owner_dept: 'node:3', owner_label: '注塑事业部', docs: 23, new_month: 3, pii_docs: 2, qa_hits: 145, no_answer_rate: 0.08, wow_net: 2, qa_hits_7d: 41, qa_wow_net: 12, qa_wow: 0.09 },
  { owner_dept: 'node:4', owner_label: '注塑一车间', docs: 9, new_month: 1, pii_docs: 0, qa_hits: 52, no_answer_rate: 0.12, wow_net: 1, qa_hits_7d: 12, qa_wow_net: -3, qa_wow: -0.05 },
  { owner_dept: 'node:6', owner_label: '质量中心', docs: 12, new_month: 0, pii_docs: 1, qa_hits: 88, no_answer_rate: 0.05, wow_net: 0, qa_hits_7d: 25, qa_wow_net: 5, qa_wow: 0.06 },
  { owner_dept: 'marketing', owner_label: 'marketing', docs: 12, new_month: 1, pii_docs: 0, qa_hits: 30, no_answer_rate: 0.2, wow_net: -1 },
  { owner_dept: 'unknown', owner_label: 'unknown', docs: 2, new_month: 0, pii_docs: 0, qa_hits: 3, no_answer_rate: 0.33, wow_net: 0 },
  { owner_dept: 'node:99', owner_label: '99', docs: 1, new_month: 0, pii_docs: 0, qa_hits: 1, no_answer_rate: 0, wow_net: 0 },
]

describe('rollupCoverageRows — 中心卷积', () => {
  const rows = rollupCoverageRows(BUCKETS, byId, legacy)

  it('事业部+车间卷到中心；可加指标求和；子行按 docs 降序', () => {
    const prod = rows.find((r) => r.label === '生产中心')!
    expect(prod.kind).toBe('center')
    expect(prod.docs).toBe(32); expect(prod.new_month).toBe(4)
    expect(prod.pii_docs).toBe(2); expect(prod.wow_net).toBe(3)
    expect(prod.children.map((c) => c.label)).toEqual(['注塑事业部', '注塑一车间'])
  })
  it('中心行非可加指标显式 null（跨桶提问去重不可加，绝不求和）', () => {
    const prod = rows.find((r) => r.label === '生产中心')!
    expect(prod.qa_hits).toBeNull(); expect(prod.qa_hits_7d).toBeNull()
    expect(prod.qa_wow_net).toBeNull(); expect(prod.qa_wow).toBeNull()
    expect(prod.no_answer_rate).toBeNull()
  })
  it('中心自身持桶 → 「（本级）」子行，父行=本级+子部门合计恒对得上', () => {
    const q = rows.find((r) => r.label === '质量中心')!
    expect(q.kind).toBe('center'); expect(q.docs).toBe(12)
    expect(q.children).toHaveLength(1)
    expect(q.children[0].label).toBe('质量中心（本级）')
    expect(q.children[0].selfRow).toBe(true)
    expect(q.children[0].qa_hits).toBe(88)          // 桶行保留真实使用量
  })
  it('legacy/unknown/快照缺链 node = 顶层独立行（不猜中心）', () => {
    expect(rows.find((r) => r.label === '市场')?.kind).toBe('legacy')
    expect(rows.find((r) => r.label === '未归属')?.kind).toBe('unknown')
    expect(rows.find((r) => r.label === '#99 ⚠️')?.kind).toBe('node_missing')
  })
  it('顶层按 docs 降序', () => {
    expect(rows[0].label).toBe('生产中心')
  })
})
