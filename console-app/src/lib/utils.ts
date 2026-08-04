import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** shadcn 约定的类名合并工具（条件类 + tailwind 冲突去重）。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * 尊重 reduce-motion 的 scrollIntoView。
 *
 * ⚠️ tokens.css 里那条 `scroll-behavior: auto !important` 的 reduce-motion 兜底**管不到**
 * 显式传入的 behavior。按 CSSOM View 的 "perform a scroll" 算法，只有 behavior 为 'auto'
 * （含省略）时才回读 CSS 计算值；'smooth'/'instant' 直接覆盖 CSS（MDN 同述）。
 * 审计实测的后果：全仓 6 个调用点里 5 个显式传 'smooth'、完全绕过兜底，剩下 1 个不传
 * behavior 的本来就是瞬时——那条兜底对「压制显式平滑滚动」这个意图 **0/6 生效**，
 * 写得很完整却从未覆盖任何真实调用点。是静默失效，不是未实现。所以必须在 JS 侧判。
 */
export function scrollIntoViewSafely(el: Element | null | undefined, block: ScrollLogicalPosition = 'start'): void {
  if (!el) return
  const reduce = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block })
}
