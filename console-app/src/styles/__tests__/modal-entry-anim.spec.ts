/// <reference types="node" />
// ↑ 本目录在 app 的 tsconfig 覆盖范围内、其 types 不含 node，缺这行会让 `npm run typecheck`
//   报 TS2591/TS2304（而 vitest 只转译不查类型，光跑测试发现不了——退出码要分开看）。
//   试过改用 Vite 的 `?raw` 绕开，但在 vitest 里拿不到源文件原文，断言全空。
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const css = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../tokens.css'), 'utf8')

/**
 * 源码镜像门 —— 模态/toast 入场关键帧的两条不可协商约束。
 *
 * 与 tests/modal-entry.spec.ts（运行时门）**有重叠但都不冗余**。注意曾写下的分工理由是错的：
 * 以为「计算值看不出作者写没写 both」「运行时测不到起始帧」——两条都经实测证伪
 * （getComputedStyle().animationFillMode 能区分；CSSOM 能 dump 出关键帧全文）。
 *
 * 本门真正独有的是三条：
 *   1. **取值来源只在源码可见**——运行时只看得到解析后的 0.16s，有人把 var(--dur-base)
 *      改成硬编码 160ms，运行时门全绿，只有这里会红；
 *   2. **不需要浏览器与 dev server**，毫秒级出结果，比 e2e 早一个数量级；
 *   3. **无 DOM 实例也会红**——tokens.css 被改而组件尚未挂上该类时运行时门测不到，
 *      这正好补上本仓 .kb-rise-top 那类「有规则、无 DOM、硬门覆盖不到」的盲区。
 * 运行时门独有的两条（类是否真挂上、reduced-motion）见那个文件的头注释。
 */
/** 取一条裸类规则的声明块（本文件的规则都是单行、无嵌套）。 */
function ruleOf(sel: string): string {
  const m = css.match(new RegExp(`^\\${sel}\\s*\\{[^}]*\\}`, 'm'))
  if (!m) throw new Error(`未找到规则 ${sel}`)
  return m[0]
}

/** 取一个 @keyframes 的整块。 */
function keyframesOf(name: string): string {
  const m = css.match(new RegExp(`@keyframes\\s+${name}\\s*\\{[^}]*\\}[^}]*\\}|@keyframes\\s+${name}\\s*\\{[^{}]*\\{[^{}]*\\}[^{}]*\\}`))
  if (!m) throw new Error(`未找到 @keyframes ${name}`)
  return m[0]
}

const ENTRY_RULES = ['.kb-scrim', '.kb-pop', '.kb-toast-in']
const ENTRY_KEYFRAMES = ['kb-scrim-in', 'kb-pop-in', 'kb-toast-in']

describe('模态/toast 入场关键帧', () => {
  it('三条规则都存在且各自绑定对应的 keyframes', () => {
    expect(ruleOf('.kb-scrim')).toContain('kb-scrim-in')
    expect(ruleOf('.kb-pop')).toContain('kb-pop-in')
    expect(ruleOf('.kb-toast-in')).toContain('kb-toast-in')
  })

  it('【约束①】绝不带 animation-fill-mode', () => {
    // 帧冻结环境（后台/隐藏标签页、离屏面板、内嵌 webview）里动画不推进：无 fill-mode 时元素
    // 回落基础样式 = 遮罩可见、面板可见可点；写了 both/backwards 就永久停在 from 帧，而
    // fixed inset-0 遮罩此刻仍拦截全部点击 ⇒「什么都看不见且整页点不动」。
    for (const sel of ENTRY_RULES) {
      expect(ruleOf(sel), `${sel} 不得含 both/backwards/forwards`).not.toMatch(/\b(both|backwards|forwards)\b/)
    }
  })

  it('【约束②】起始帧只动 transform / background-color，绝不碰 opacity', () => {
    // 审计对 AppShell.vue 那次白屏事故给出的验收条件。与约束① 一起构成两条独立防线：
    // 即便将来有人误加了 fill-mode，起始帧里没有 opacity 也不会让元素变成不可见但仍拦点击。
    for (const name of ENTRY_KEYFRAMES) {
      expect(keyframesOf(name), `@keyframes ${name} 起始帧不得含 opacity`).not.toContain('opacity')
    }
  })

  it('不引入 Tailwind 保留命名空间 --ease-*（红线 2026-08-04）', () => {
    // Tailwind v4 的 ease-* 工具类编译成对 --ease-* 的引用；在 :root 同名声明会覆盖其
    // @theme 默认值，把 Sidebar 展开曲线静默换掉（motion-tokens 硬门 G2 抓到过）。
    expect(css).not.toMatch(/^\s*--ease-[a-z-]+\s*:/m)
  })

  it('入场时长取自 scale token，不再硬编码毫秒', () => {
    expect(ruleOf('.kb-scrim')).toContain('var(--dur-fast)')
    expect(ruleOf('.kb-pop')).toContain('var(--dur-base)')
    expect(ruleOf('.kb-toast-in')).toContain('var(--dur-base)')
    expect(css, '--dur-base 本轮起有真实消费方，必须已定义').toMatch(/--dur-base:\s*160ms/)
  })
})
