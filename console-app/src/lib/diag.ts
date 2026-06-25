import { ref } from 'vue'

// 轻量诊断环形日志（parity-4）：?debug=1 时由 DebugPanel 展示。把免登/接口失败逐条打点，
// 便于真机定位（本部署反复遇到的裸 IP+HTTP、web-view 业务域名未登记、免登抖动等）。
// 注意：?debug 不在 scrubUrl 清单内，故抹除 token 后仍保留，调试期可持续生效。

export interface DiagLine { seq: number; msg: string }

const lines = ref<DiagLine[]>([])
let seq = 0

export function diag(msg: string): void {
  lines.value.push({ seq: ++seq, msg })
  if (lines.value.length > 100) lines.value.shift()   // 环形上限
}

export function diagLines() { return lines }

export function debugEnabled(): boolean {
  try { return new URLSearchParams(window.location.search).has('debug') } catch { return false }
}

/** 仅供测试。 */
export function __resetDiag(): void { lines.value = []; seq = 0 }
