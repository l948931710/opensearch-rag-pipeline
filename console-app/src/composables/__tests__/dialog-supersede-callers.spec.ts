import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'

/**
 * B4 复核（2026-08-04）：`_supersede` 把被顶掉的对话框按「取消」了结
 * （`confirm→false` / `promptText→null` / `notice→void`）。
 *
 * 那个选择只有在**每个调用点都把取消值当成"中止"**时才安全。审过全部 19 个调用点
 * （7 × promptText + 9 × confirm + notice 若干）：无一例外。这里把它变成守卫 ——
 * 一旦有人写成 `const r = await promptText(...) ?? ''` 然后照常提交，
 * 一次无关的后台 `notice()` 就能让「驳回/撤销」凭空带着空理由执行。
 */
function srcFiles(): string[] {
  const out: string[] = []
  const walk = (d: string) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name)
      if (e.isDirectory()) { if (e.name !== '__tests__') walk(p) }
      else if (/\.(ts|vue)$/.test(e.name)) out.push(p)
    }
  }
  walk(path.resolve(__dirname, '../..'))
  return out
}

describe('_supersede 的取消值在每个调用点都被当成「中止」', () => {
  it('每处 `await promptText(` 之后必须有 `=== null` 判定', () => {
    const bad: string[] = []
    let seen = 0
    for (const f of srcFiles()) {
      const lines = fs.readFileSync(f, 'utf-8').split('\n')
      lines.forEach((ln, i) => {
        if (!/await\s+promptText\(/.test(ln)) return
        seen++
        // 调用可能跨行；从调用行起看 12 行内有没有 null 判定
        const win = lines.slice(i, i + 12).join(' ')
        if (!/===\s*null|!==\s*null|==\s*null/.test(win)) {
          bad.push(`${path.relative(process.cwd(), f)}:${i + 1}`)
        }
      })
    }
    expect(seen).toBeGreaterThanOrEqual(5)   // 非空转守卫
    expect(bad, '这些 promptText 调用点没判 null —— 被顶掉的对话框会带着"取消"值继续执行').toEqual([])
  })

  it('每处 `await confirm(` 之后必须有布尔判定（`if (x)` 或 `if (!x) return`）', () => {
    const bad: string[] = []
    let seen = 0
    for (const f of srcFiles()) {
      if (f.endsWith('useDialog.ts')) continue        // 定义处，非调用点
      const lines = fs.readFileSync(f, 'utf-8').split('\n')
      lines.forEach((ln, i) => {
        const m = /(?:const|let)\s+(\w+)\s*=\s*await\s+confirm\(/.exec(ln)
        if (!m) return
        seen++
        const v = m[1]
        const win = lines.slice(i, i + 12).join(' ')
        if (!new RegExp(`if\\s*\\(\\s*!?\\s*${v}\\b`).test(win)) {
          bad.push(`${path.relative(process.cwd(), f)}:${i + 1} (${v})`)
        }
      })
    }
    expect(seen).toBeGreaterThanOrEqual(5)
    expect(bad, '这些 confirm 调用点没判返回值 —— 被顶掉的确认框会让危险操作走下去').toEqual([])
  })
})
