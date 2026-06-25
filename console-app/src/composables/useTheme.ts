import { ref } from 'vue'

// 亮/暗主题（Atlas Chat 双主题）。初值由 index.html 的防闪白脚本在渲染前写入 data-theme，
// 这里只读取并提供切换 + 持久化（localStorage 'fl-theme'）。
export type Theme = 'light' | 'dark'
const KEY = 'fl-theme'

function current(): Theme {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light'
}

const theme = ref<Theme>(current())

function apply(t: Theme) {
  theme.value = t
  document.documentElement.setAttribute('data-theme', t)
  try { localStorage.setItem(KEY, t) } catch { /* 隐私模式忽略 */ }
}

export function useTheme() {
  return {
    theme,
    toggle: () => apply(theme.value === 'dark' ? 'light' : 'dark'),
    set: (t: Theme) => apply(t),
  }
}
