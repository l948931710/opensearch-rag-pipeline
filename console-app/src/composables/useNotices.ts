import { computed, ref } from 'vue'
import { apiJson } from '@/lib/api'

// 站内通知单例 store（文档升版可见范围提醒）。后端契约见 routes/notices.py；
// 数据由 DataWorks 日调作业写入 doc_update_notice（schema/065），本前端只读 + 标已读。
//
// ⚠️ UI 静默红线（Sam 2026-08-04「不想太多通知影响 UIUX」）：
//   · 无未读 = 零装饰（不出红点、不出数字）；
//   · 有未读 = 只在铃铛上出一个数字角标（99+ 封顶）；
//   · **绝不** toast / 自动展开面板 / 浏览器通知 / 内容区横幅。
//   本模块因此刻意不暴露任何"主动弹出"的 API —— 面板开合完全由用户点击驱动。
//
// 未部署本特性 / schema/065 未 apply 的后端：端点回空或 404 → 静默兜底空（与贡献域同款）。

export interface NoticeItem {
  id: number
  doc_id: string
  title: string
  version_no?: number | null
  state: string            // 'PENDING' = 未读 | 'READ' = 已读
  created_at?: string | null
}

const items = ref<NoticeItem[]>([])
const unreadCount = ref(0)
const unreadCapped = ref(false)
const loading = ref(false)
const loaded = ref(false)
let lastFetch = 0

// 轮询下限：导航切换时触发，但两次真实请求至少间隔 60s。通知是日调批产物，
// 秒级新鲜度毫无意义，而 unread 计数在服务端要逐行过 ACL 复核（成本非零）。
const MIN_INTERVAL_MS = 60_000

export function useNotices() {
  async function load(force = false) {
    const now = Date.now()
    if (!force && loaded.value && now - lastFetch < MIN_INTERVAL_MS) return
    if (loading.value) return
    loading.value = true
    try {
      const data = await apiJson<{ items?: NoticeItem[]; unread_count?: number; unread_capped?: boolean }>(
        '/api/kb/notices?limit=20',
      )
      items.value = Array.isArray(data?.items) ? data.items : []
      unreadCount.value = Number(data?.unread_count || 0)
      unreadCapped.value = Boolean(data?.unread_capped)
      lastFetch = now
      loaded.value = true
    } catch {
      // 后端未部署 / 表未 apply / 未登录：静默兜底空 —— 通知是增益功能，
      // 绝不能因为它报错而在 console 上弹任何东西。
      items.value = []
      unreadCount.value = 0
      unreadCapped.value = false
      loaded.value = true
    } finally {
      loading.value = false
    }
  }

  async function markRead(ids?: number[]) {
    const body = ids && ids.length ? { ids } : { all: true }
    // ⚠️ apiJson 的 ApiOpts extends RequestInit ⇒ body 必须 JSON.stringify，
    // 传对象请求根本不会发出且无报错（本仓踩过的坑）。
    try {
      await apiJson('/api/kb/notices/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    } catch {
      // 标已读失败不该打断用户：本地先行翻态，下次 load 会与服务端对齐。
    }
    const target = ids && ids.length ? new Set(ids) : null
    for (const it of items.value) {
      if (!target || target.has(it.id)) it.state = 'READ'
    }
    unreadCount.value = target
      ? Math.max(0, unreadCount.value - target.size)
      : 0
    if (!target) unreadCapped.value = false
  }

  return {
    items: computed(() => items.value),
    unreadCount: computed(() => unreadCount.value),
    unreadCapped: computed(() => unreadCapped.value),
    loading: computed(() => loading.value),
    loaded: computed(() => loaded.value),
    load,
    markRead,
  }
}
