// 答案是 LLM 生成的 markdown：先转义 HTML 防注入，再套白名单（标题/列表/粗体/行内码/围栏代码）。
// 与旧 console.html 的 renderMd/stripImg 行为对齐（ce3730c），纯函数、可独立单测。

const ESC: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }

export function escapeHtml(s: unknown): string {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC[c])
}

function inline(s: string): string {
  // 在【已转义】文本上加白名单行内标记，故注入安全。
  return s.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>')
}

// 轻量·语言无关的代码高亮（Atlas 配色）。在【原始】code 上分词，每个 token 文本先转义再包色 span，
// 故注入安全。识别：注释 / 字符串 / 数字 / 关键字 / 函数调用；其余原样（转义）。无外部依赖（非 hljs）。
const _KW = new Set([
  'function', 'return', 'if', 'else', 'for', 'while', 'const', 'let', 'var', 'class', 'new', 'import',
  'from', 'export', 'default', 'async', 'await', 'try', 'catch', 'finally', 'throw', 'in', 'of', 'def',
  'public', 'private', 'static', 'void', 'null', 'true', 'false', 'None', 'True', 'False', 'self', 'this',
  'select', 'insert', 'update', 'delete', 'where', 'and', 'or', 'not', 'as', 'with', 'lambda',
])
const _TOK = /(\/\/[^\n]*|#[^\n]*)|(\/\*[\s\S]*?\*\/)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)|(\b\d[\d._]*\b)|([A-Za-z_$][\w$]*)|([\s\S])/g

export function highlightCode(code: string): string {
  let out = ''
  let m: RegExpExecArray | null
  _TOK.lastIndex = 0
  while ((m = _TOK.exec(code))) {
    if (m[1] || m[2]) out += `<span class="c-comment">${escapeHtml(m[1] || m[2])}</span>`
    else if (m[3]) out += `<span class="c-str">${escapeHtml(m[3])}</span>`
    else if (m[4]) out += `<span class="c-num">${escapeHtml(m[4])}</span>`
    else if (m[5]) {
      const id = m[5]
      const call = code[_TOK.lastIndex] === '('   // 标识符后紧跟 ( → 函数调用
      out += _KW.has(id) ? `<span class="c-key">${escapeHtml(id)}</span>`
        : call ? `<span class="c-fn">${escapeHtml(id)}</span>`
        : escapeHtml(id)
    } else out += escapeHtml(m[6])
    if (m[0] === '') _TOK.lastIndex++   // 防御空匹配死循环
  }
  return out
}

function codeBlock(code: string, lang: string): string {
  const langAttr = /^[\w-]{1,20}$/.test(lang) ? ` data-lang="${lang}"` : ''
  return `<pre><code${langAttr}>${highlightCode(code)}</code></pre>`
}

const NUL = String.fromCharCode(0)   // 占位哨兵：escapeHtml/trim 都不动它，故能稳妥标记代码块行

/** LLM markdown → 安全 HTML（白名单：``` 围栏代码 / # 标题 / -|* 列表 / **粗体** / `行内码`）。 */
export function renderMd(text: unknown): string {
  // 1) 先抽取围栏代码块（多行），换成自成一行的占位符——避免内部内容被逐行 md 规则误伤。
  const blocks: string[] = []
  const src = String(text == null ? '' : text).replace(
    /```([\w-]*)[ \t]*\n?([\s\S]*?)```/g,
    (_all, lang: string, code: string) => {
      blocks.push(codeBlock(code.replace(/\n$/, ''), lang || ''))
      return `\n${NUL}CB${blocks.length - 1}${NUL}\n`
    },
  )

  // 2) 其余逐行套白名单。
  const cbRe = new RegExp(`^${NUL}CB(\\d+)${NUL}$`)
  const out: string[] = []
  escapeHtml(src).split(/\n/).forEach((raw) => {
    const line = raw.replace(/\s+$/, '')
    if (!line.trim()) return
    const cb = cbRe.exec(line.trim())
    if (cb) { out.push(blocks[Number(cb[1])]); return }
    if (/^#{1,6}\s+/.test(line)) { out.push('<h3>' + inline(line.replace(/^#{1,6}\s+/, '')) + '</h3>'); return }
    if (/^\s*[-*]\s+/.test(line)) { out.push('<p class="md-li">' + inline(line.replace(/^\s*[-*]\s+/, '')) + '</p>'); return }
    out.push('<p>' + inline(line) + '</p>')
  })
  return out.join('') || '<p></p>'
}

/**
 * 流式 chunk 是 LLM 原始 token，可能带 <<IMG:N>> 标记（图片以 content_blocks 帧权威定稿）。
 * 去完整标记 + 末尾【未到齐的半截】标记（如 "<<IM" / "<IMG:1"），避免打字途中闪出标记碎片。
 * 后端契约：单个标记可能被拆到多帧，前端绝不从流文本解析图片——只擦不解析。
 */
export function stripImg(s: unknown): string {
  return String(s == null ? '' : s)
    .replace(/<{1,2}IMG:\d+>{1,2}/g, '')
    .replace(/<{1,2}I?M?G?:?\d*$/, '')
}
