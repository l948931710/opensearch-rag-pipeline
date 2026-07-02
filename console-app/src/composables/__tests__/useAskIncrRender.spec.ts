import { describe, expect, it } from 'vitest'
import { __incrRenderTestkit } from '@/composables/useAsk'
import { renderMd, stripImg } from '@/lib/markdown'

// H#68 流式增量渲染等价性回归：对每一个"流式前缀"，增量版（stripImgIncr/renderMdIncr）的输出必须与
// 全量参考实现（stripImg/renderMd）逐字符一致——这是"定稿帧全量权威、流式期与全量同帧等价"的地基。
// 模拟 raw 按随机大小 chunk 追加到达（复用同一 state，与 pumpTick 用法一致）。

const { newState, stripImgIncr, renderMdIncr } = __incrRenderTestkit()

/** 把 text 切成随机 1..maxStep 长度的追加序列，逐前缀断言增量 === 全量。 */
function assertEquivalence(text: string, seed = 7, maxStep = 5): void {
  // 简单 LCG（可复现，避免测试随机翻车不可重放）
  let s = seed
  const rnd = () => { s = (s * 48271) % 2147483647; return s / 2147483647 }
  const st = newState()
  let n = 0
  while (n < text.length) {
    n = Math.min(text.length, n + 1 + Math.floor(rnd() * maxStep))
    const raw = text.slice(0, n)
    const stripped = stripImgIncr(st, raw)
    expect(stripped).toBe(stripImg(raw))
    // 渲染侧再按 slice 模拟吐字泵推进（shown 单调增长；这里直接用全文，泵内亦然）
    expect(renderMdIncr(st, stripped)).toBe(renderMd(stripImg(raw)))
  }
}

describe('H#68 增量渲染 === 全量渲染（逐前缀等价）', () => {
  it('普通多段 markdown（标题/列表/粗体/行内码）', () => {
    assertEquivalence('# 标题\n\n- 第一项\n- **第二**项\n\n正文 `code` 结尾\n再来一段很长很长的中文正文，用来跨多个 chunk。\n')
  })

  it('<<IMG:N>> 完整/半截标记逐字节到达（含 "<<IMG:1>" 差一个 > 的形态）', () => {
    const text = '见下图 <<IMG:1>> 与 <IMG:23> 之后 a<b 以及末尾半截 <<IMG:4'
    for (const seed of [1, 2, 3]) assertEquivalence(text, seed, 1)   // 步长 1 = 最严苛的边界切分
    assertEquivalence(text, 11, 3)
  })

  it('围栏代码块：闭合前整体回退重渲、闭合后定稿（含围栏后追加正文）', () => {
    const text = '前置说明\n```python\ndef f():\n    return 1  # <<IMG:9>> 在代码里也照擦\n```\n围栏后正文\n- 列表项\n'
    for (const seed of [5, 9]) assertEquivalence(text, seed, 4)
    assertEquivalence(text, 3, 1)
  })

  it('永不闭合的孤儿围栏（全程回退路径）与同行多围栏', () => {
    assertEquivalence('a```b```c 同行两枚\n然后孤儿 ```\n后面全部在未闭合围栏里\n仍然要与全量一致\n', 13, 2)
  })

  it('空串/纯空白/只有换行', () => {
    assertEquivalence('\n\n  \n', 1, 1)
    const st = newState()
    expect(renderMdIncr(st, '')).toBe(renderMd(''))
  })
})
