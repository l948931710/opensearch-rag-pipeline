import { describe, expect, it } from 'vitest'
import { escapeHtml, renderMd, stripImg } from '@/lib/markdown'

describe('escapeHtml', () => {
  it('转义 < > & " \'', () => {
    expect(escapeHtml('<a href="x">&\'')).toBe('&lt;a href=&quot;x&quot;&gt;&amp;&#39;')
  })
})

describe('renderMd（白名单 markdown，转义优先防注入）', () => {
  it('脚本被转义、不可执行', () => {
    const h = renderMd('<script>alert(1)</script>')
    expect(h).not.toContain('<script>')
    expect(h).toContain('&lt;script&gt;')
  })
  it('标题 / 列表 / 粗体 / 行内码', () => {
    expect(renderMd('## 标题')).toContain('<h3>标题</h3>')
    expect(renderMd('- 第一项')).toContain('class="md-li"')
    expect(renderMd('* 星号项')).toContain('class="md-li"')
    expect(renderMd('这是**重点**')).toContain('<strong>重点</strong>')
    expect(renderMd('用 `code` 包裹')).toContain('<code>code</code>')
  })
  it('多行合并 + 空串兜底', () => {
    expect(renderMd('一\n\n二')).toBe('<p>一</p><p>二</p>')
    expect(renderMd('')).toBe('<p></p>')
  })
})

describe('stripImg（<<IMG:N>> 含跨帧半截）', () => {
  it('去完整标记（双/单尖括号）', () => {
    expect(stripImg('a<<IMG:1>>b<IMG:2>c')).toBe('abc')
  })
  it('去末尾未到齐的半截标记（打字途中不闪碎片）', () => {
    expect(stripImg('文本 <<IM')).toBe('文本 ')
    expect(stripImg('x <IMG:1')).toBe('x ')
    expect(stripImg('y <<IMG:12')).toBe('y ')
  })
  it('正常含尖括号文本不误伤', () => {
    expect(stripImg('a < b 1>2')).toBe('a < b 1>2')
  })
})
