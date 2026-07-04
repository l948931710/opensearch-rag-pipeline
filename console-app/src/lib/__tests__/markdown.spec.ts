import { describe, expect, it } from 'vitest'
import { escapeHtml, renderMd, stripImg, highlightCode } from '@/lib/markdown'

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
  it('有序列表：1. / 1、 / 1) 带原序号（data-n），不再落成普通段落', () => {
    expect(renderMd('1. 第一步')).toBe('<p class="md-li md-oli" data-n="1">第一步</p>')
    expect(renderMd('2、第二步')).toContain('data-n="2"')
    expect(renderMd('3) 第三步')).toContain('data-n="3"')
    expect(renderMd('12. 两位序号')).toContain('data-n="12"')
  })
  it('引用：> 行渲染成 blockquote，连续行合并，不再露出字面 >', () => {
    expect(renderMd('> 引用一行')).toBe('<blockquote>引用一行</blockquote>')
    expect(renderMd('> 甲\n> 乙')).toBe('<blockquote>甲<br>乙</blockquote>')
    expect(renderMd('正文\n> 引\n后续')).toBe('<p>正文</p><blockquote>引</blockquote><p>后续</p>')
    expect(renderMd('> 引')).not.toContain('&gt;')
  })
  it('链接：仅放行显式 http(s)，其他协议不生成 <a>', () => {
    expect(renderMd('[门户](https://kb.fuling.com/x)')).toContain('<a href="https://kb.fuling.com/x" target="_blank" rel="noopener noreferrer">门户</a>')
    expect(renderMd('[x](javascript:alert(1))')).not.toContain('<a')
    // URL 里带引号也逃不出 href（已被 escapeHtml 转成 &quot;）
    expect(renderMd('[x](https://e.com/"onmouseover="p())')).not.toContain('onmouseover="')
  })
})

describe('renderMd 围栏代码块 + highlightCode', () => {
  it('``` 块 → <pre><code>，内部不被逐行 md 误伤', () => {
    const h = renderMd('说明：\n```sql\nselect * from t -- 注释\n```\n结束')
    expect(h).toContain('<pre><code data-lang="sql">')
    expect(h).toContain('<p>说明：</p>')
    expect(h).toContain('<p>结束</p>')
    // 内部 -- 注释 没被当成列表项
    expect(h).not.toContain('md-li')
  })
  it('highlightCode 给注释/字符串/数字/关键字上色且转义安全', () => {
    const out = highlightCode(`const x = "a<b" // c\nfoo(1)`)
    expect(out).toContain('<span class="c-key">const</span>')
    expect(out).toContain('<span class="c-str">&quot;a&lt;b&quot;</span>')   // 已转义
    expect(out).toContain('<span class="c-comment">// c</span>')
    expect(out).toContain('<span class="c-num">1</span>')
    expect(out).toContain('<span class="c-fn">foo</span>')                    // 标识符后紧跟 (
    expect(out).not.toContain('<b')                                          // 无未转义标签注入
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
  // #F-mm6：图级子下标 <<IMG:N.M>> 也要擦（RAG_IMG_SUBINDEX 开时避免闪碎片）
  it('去完整图级子下标标记', () => {
    expect(stripImg('a<<IMG:1.2>>b<IMG:3.1>c')).toBe('abc')
    expect(stripImg('步骤 <<IMG:3.2>> 完成')).toBe('步骤  完成')
  })
  it('去末尾未到齐的半截子下标标记', () => {
    expect(stripImg('文本 <<IMG:1.')).toBe('文本 ')
    expect(stripImg('x <<IMG:12.3')).toBe('x ')
  })
})
