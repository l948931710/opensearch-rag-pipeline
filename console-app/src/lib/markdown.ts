// 答案是 LLM 生成的 markdown：先转义 HTML 防注入，再套白名单（标题/列表/粗体/行内码）。
// 与旧 console.html 的 renderMd/stripImg 行为对齐（ce3730c），纯函数、可独立单测。

const ESC: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }

export function escapeHtml(s: unknown): string {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC[c])
}

function inline(s: string): string {
  // 在【已转义】文本上加白名单行内标记，故注入安全。
  return s.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>')
}

/** LLM markdown → 安全 HTML（白名单：# 标题 / -|* 列表 / **粗体** / `行内码`）。 */
export function renderMd(text: unknown): string {
  const out: string[] = []
  escapeHtml(text).split(/\n/).forEach((raw) => {
    const line = raw.replace(/\s+$/, '')
    if (!line.trim()) return
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
