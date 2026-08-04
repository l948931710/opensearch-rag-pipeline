import { ref, type Ref } from 'vue'

/**
 * 轻量 toast —— 「操作成功了」的非阻塞出口。
 *
 * ## 为什么不复用 useDialog().notice()
 *
 * `useDialog` 的 state/_resolve 是**模块级单槽**（`_supersede()`）：同一时刻只能存活一个
 * confirm/prompt/notice，新的会把旧的 Promise 强制以「取消」了结。若成功提示走 notice()，
 * 一个后台请求返回就能把用户正盯着的破坏性确认框（如「撤销部门管理权限」）顶掉，并让它
 * resolve 成 false —— 用户以为自己点了取消，实际是被挤掉的。故本模块与 useDialog 完全独立，
 * 不共享任何状态。
 *
 * ## 为什么 spinner 不够
 *
 * 实测按钮从 click 到 `:disabled` 生效只要 17–24ms，在途反馈本来就不缺。缺的是**成功之后**：
 * `resolveFeedback`/`resolveReviewTask` 成功即把该行从列表里 filter 掉，用户唯一的反馈是
 * 「那一行消失了」——与「我点错了」「网络没生效」「本来就没有」三种情况肉眼不可区分；
 * 处理完最后一条时空态文案还与「从来没有过」共用同一句。spinner 覆盖不到这一段。
 */

export type ToastTone = 'success' | 'error' | 'info'

export interface ToastAction {
  label: string
  /** 点击后执行，并自动关闭该条 toast */
  run: () => void
}

export interface ToastItem {
  id: number
  /**
   * 回执正文。判据不是字数而是**性质**：能一眼扫过、不读完也不影响下一步的，走这里；
   * 需要用户读完才能行动的（如批量失败的逐条明细）仍走 useDialog().notice() 模态。
   * 容器 max-w 360px 自动换行，长一点的语义说明（如索引重推那句）不会溢出。
   */
  text: string
  tone: ToastTone
  action?: ToastAction
}

export interface ToastOpts {
  tone?: ToastTone
  /** 覆盖默认停留时长（ms） */
  ttl?: number
  action?: ToastAction
}

/** 并发上限：批量操作可能连发，超出会盖住台账右下角的翻页器，故只留最新 3 条。 */
const MAX_VISIBLE = 3

/** 失败停留更久——用户往往需要读完文案才知道下一步做什么。 */
const DEFAULT_TTL: Record<ToastTone, number> = { success: 3200, info: 3200, error: 6000 }

// 模块级单例（与 useDialog 同惯例：模块作用域 ref + useXxx() 返回同一份）
const toasts = ref<ToastItem[]>([])
const timers = new Map<number, ReturnType<typeof setTimeout>>()
let seq = 0

function killTimer(id: number) {
  const t = timers.get(id)
  if (t) { clearTimeout(t); timers.delete(id) }
}

export function useToast(): {
  toasts: Ref<ToastItem[]>
  toast: (text: string, o?: ToastOpts) => number
  dismissToast: (id: number) => void
  clearToasts: () => void
} {
  function dismissToast(id: number) {
    killTimer(id)
    toasts.value = toasts.value.filter((t) => t.id !== id)
  }

  /**
   * 清空队列 + 所有定时器。
   * **这是安全项,不是清理洁癖**：队列是模块级的,换身份（401 落穿重登 / principalEpoch 翻转）
   * 后若不清,A 的「已授予部门管理员」会飘到 B 的界面上。定时器句柄也必须一并清,否则旧定时器
   * 会在新身份下去动新队列。
   */
  function clearToasts() {
    for (const id of Array.from(timers.keys())) killTimer(id)
    toasts.value = []
  }

  function toast(text: string, o: ToastOpts = {}): number {
    const tone = o.tone ?? 'success'
    const id = ++seq
    // 刻意**不做去重**：同文案连发两次就是两次操作都成功了,合并会瞒报其中一次
    const next = [...toasts.value, { id, text, tone, action: o.action }]
    while (next.length > MAX_VISIBLE) killTimer(next.shift()!.id)
    toasts.value = next
    timers.set(id, setTimeout(() => dismissToast(id), o.ttl ?? DEFAULT_TTL[tone]))
    return id
  }

  return { toasts, toast, dismissToast, clearToasts }
}
