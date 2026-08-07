// 台账「异常」伪徽章筛选 × 自愈 watcher（2026-08-07 现网：筛选自己弹回「全部」）
//
// 判据：伪徽章的【显示条件】≠【有效性】。
//   「异常」= BAD_BADGES 的聚合筛选，其显示条件是「坏徽章 ≥2 种才值得单独一枚 chip」（只剩
//   一种时与那枚单徽章 chip 重复）；但只剩一种时它照样筛得对。DocTable 的自愈 watcher 把
//   「不值得显示」读成了「筛选值失效」，于是 faceted 计数到达那一刻把 filter 清成空串。
// 修法在 useKb.ledgerBadgeChips：当前选中压过去冗余规则（选中项永不从自己的选项列表里消失），
// 自愈规则本身不开特例 —— 故本文件的反证锚必须证明自愈对**真徽章**仍然生效。
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import DocTable from '@/components/manage/DocTable.vue'
import { useKb, __resetKb, type DocItem } from '@/composables/useKb'

beforeEach(() => { vi.restoreAllMocks(); __resetKb() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '张三', role: 'kb_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'], ...over }
}
function activate(id: Identity = identity(), token = 't') {
  const p = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token, ready: true } } })
  setActivePinia(p)
  return p
}
function doc(over: Partial<DocItem> = {}): DocItem {
  return { doc_id: 'd', title: 't', original_filename: 'f', owner_dept: 'marketing', permission_level: 'dept_internal', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '2026-06-26', can_manage: true, ...over }
}
function jsonResp(body: unknown) {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) }
}
async function waitFor(cond: () => boolean, ms = 2000) {
  const t0 = Date.now()
  while (!cond() && Date.now() - t0 < ms) await new Promise((r) => setTimeout(r, 5))
}

// 台账 mock：faceted badge_counts 由 counts 变量供给（可在用例中途改，模拟换 scope / 计数变化）。
// ⚠️ 服务端 _kb_badge_counts 的口径是「与主查询同筛选，唯独不含 badge 自身」——所以按「异常」
// 筛选并不会让 badge_counts 变形，这里照此保持不变（改成随 badge 变形就测不到真实时序了）。
function ledgerFetch(counts: () => Record<string, number>, calls: string[]) {
  return vi.fn(async (path: string) => {
    calls.push(String(path))
    const c = counts()
    const items = Object.entries(c).flatMap(([badge, n]) =>
      Array.from({ length: Math.min(n, 2) }, (_, i) => doc({ doc_id: `${badge}-${i}`, title: `${badge}-${i}`, status_badge: badge })))
    return jsonResp({ items, has_more: false, badge_counts: c })
  })
}
// 自愈 watcher 是 pre-flush 的 computed watcher，且其 setBadgeFilter('') 还会再触发一轮
// URL 同步 watcher —— 给足两拍再断言，避免"还没弹回来"被当成绿。
async function settle(w: { vm: { $nextTick: () => Promise<void> } }) {
  await w.vm.$nextTick(); await w.vm.$nextTick(); await w.vm.$nextTick()
}

describe('台账「异常」筛选不会自己弹回「全部」', () => {
  it('① 点「异常」→ faceted 计数到达后筛选仍是「异常」，chip 仍在且高亮', async () => {
    // 坏徽章只剩「未入索引」一种 ⇒ 去冗余规则本来要收起「异常」chip（现网 78 EMPTY 的形状）
    const calls: string[] = []
    vi.stubGlobal('fetch', ledgerFetch(() => ({ 已上线: 100, 未入索引: 3 }), calls))
    const w = mount(DocTable, { global: { plugins: [activate()] } })
    const kb = useKb()
    await kb.loadDocs()                      // 首屏：chips 就绪（>1 项，自愈 watcher 的门槛已过）
    await settle(w)
    expect(kb.ledgerBadgeChips.value).not.toContain('异常')   // 未选中时去冗余规则照旧生效

    calls.length = 0
    kb.setBadgeFilter('异常')                 // 待办摘要条「异常文档」chip 的动作（ManageView 同款）
    await waitFor(() => calls.some((c) => c.includes('badge=')))   // 防抖 150ms → 服务端重载
    await settle(w)

    expect(kb.filter.value).toBe('异常')      // ← 现网缺陷：这里会被自愈 watcher 清成 ''
    const chip = w.findAll('button').find((b) => b.text().startsWith('异常'))
    expect(chip).toBeTruthy()                                     // 选中项必须始终可见
    expect(chip!.text().replace(/\s+/g, '')).toBe('异常3')        // 计数走 anomalyCount（BAD_BADGES 整集）
    expect(chip!.classes()).toContain('bg-accent')                // 高亮停在「异常」而非「全部」
    const all = w.findAll('button').find((b) => b.text().startsWith('全部'))!
    expect(all.classes()).not.toContain('bg-accent')
  })

  it('② 反证锚：真徽章从可选集消失 → 自愈仍然弹回「全部」（别把规则整个改废）', async () => {
    const calls: string[] = []
    let counts: Record<string, number> = { 已上线: 100, 处理失败: 2, 未入索引: 3 }
    vi.stubGlobal('fetch', ledgerFetch(() => counts, calls))
    const w = mount(DocTable, { global: { plugins: [activate()] } })
    const kb = useKb()
    await kb.loadDocs()
    await settle(w)

    calls.length = 0
    kb.setBadgeFilter('处理失败')
    await waitFor(() => calls.some((c) => c.includes('badge=')))
    await settle(w)
    expect(kb.filter.value).toBe('处理失败')                       // 还在可选集里 → 不动它

    counts = { 已上线: 100, 未入索引: 3 }                          // 换 scope / 计数变化：该徽章没了
    await kb.loadDocs()
    await settle(w)
    expect(kb.filter.value).toBe('')                              // 自愈生效：回退「全部」避免死角
    expect(w.findAll('button').find((b) => b.text().startsWith('全部'))!.classes()).toContain('bg-accent')
  })

  it('③ 反证锚：异常数归零 → 「异常」chip 收起且自愈生效（0 计数的 chip 不显示）', async () => {
    // 「不值得显示」让位给「当前选中」，但「真的空了」不让位——自愈的不变量保持成立：
    // chip 不在可选集里 ⟺ 该筛选真的没有内容。
    const calls: string[] = []
    let counts: Record<string, number> = { 已上线: 100, 处理失败: 2, 未入索引: 3 }
    vi.stubGlobal('fetch', ledgerFetch(() => counts, calls))
    const w = mount(DocTable, { global: { plugins: [activate()] } })
    const kb = useKb()
    await kb.loadDocs()
    await settle(w)
    expect(kb.ledgerBadgeChips.value).toContain('异常')            // 坏徽章 2 种 → 值得一枚聚合 chip

    calls.length = 0
    kb.setBadgeFilter('异常')
    await waitFor(() => calls.some((c) => c.includes('badge=')))
    await settle(w)
    expect(kb.filter.value).toBe('异常')

    counts = { 已上线: 100 }                                       // 异常全部处置完 → 计数归零
    await kb.loadDocs()
    await settle(w)
    expect(kb.ledgerBadgeChips.value).not.toContain('异常')
    expect(kb.filter.value).toBe('')
  })

  it('④ 去冗余规则未被改废：未选中时 坏徽章 1 种不出「异常」chip、≥2 种才出', async () => {
    const calls: string[] = []
    let counts: Record<string, number> = { 已上线: 100, 未入索引: 3 }
    vi.stubGlobal('fetch', ledgerFetch(() => counts, calls))
    activate()
    const kb = useKb()
    await kb.loadDocs()
    expect(kb.ledgerBadgeChips.value).toEqual(['', '已上线', '未入索引'])

    counts = { 已上线: 100, 未入索引: 3, 已隔离: 2 }
    await kb.loadDocs()
    expect(kb.ledgerBadgeChips.value).toEqual(['', '已上线', '未入索引', '已隔离', '异常'])
    expect(kb.ledgerBadgeCount('异常')).toBe(5)                    // 3 + 2（BAD_BADGES 整集求和）
  })
})

// ── anomalyFilterTarget：待办条与台账 chip 的单一来源（2026-08-07 现网第二形态）──
// 现象：坏徽章只剩一种（现网 `已驳回 1`）时，点待办条「异常文档」后台账同时出现
// `已驳回 1` 与 `异常 1` —— 两枚 chip、同一篇文档、同一个数字；切回「全部」那枚又消失。
// 根因是待办条（只问「有没有异常」）与台账 chip（还问「值不值得单独一枚」）**两套存在条件**。
describe('anomalyFilterTarget — 语义冗余消除', () => {
  it('坏徽章只剩一种 ⇒ 目标就是那枚真徽章（不再绕伪徽章）', () => {
    activate()
    const { anomalyFilterTarget, anomalyCount } = useKb()
    ;(useKb() as any).kbStats = undefined
    const kb = useKb() as any
    kb.docs.value = [doc({ doc_id: 'a', status_badge: '已驳回' }), doc({ doc_id: 'b', status_badge: '排队中' })]
    expect(anomalyCount.value).toBe(1)
    expect(anomalyFilterTarget.value).toBe('已驳回')
  })

  it('坏徽章 ≥2 种 ⇒ 目标回到聚合值「异常」', () => {
    activate()
    const kb = useKb() as any
    kb.docs.value = [doc({ doc_id: 'a', status_badge: '已驳回' }), doc({ doc_id: 'b', status_badge: '已隔离' })]
    expect(kb.anomalyFilterTarget.value).toBe('异常')
  })

  it('单一坏徽章下按目标筛选：台账不出现同物两名的冗余 chip', () => {
    activate()
    const kb = useKb() as any
    kb.docs.value = [doc({ doc_id: 'a', status_badge: '已驳回' }), doc({ doc_id: 'b', status_badge: '排队中' })]
    // 模拟待办条点击（ManageView 用的就是这个值）
    kb.filter.value = kb.anomalyFilterTarget.value
    expect(kb.filter.value).toBe('已驳回')
    // 反证锚：此时 chips 里**不该**再有「异常」——它没被选中，且坏徽章只有一种
    expect(kb.ledgerBadgeChips.value).not.toContain('异常')
    expect(kb.ledgerBadgeChips.value).toContain('已驳回')
  })
})
