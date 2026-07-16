// 流式增量 markdown 渲染内核（H#68，自 useAsk.ts 原样抽出——perf 批次 B §6.2 供
// Agent 流复用；行为逐字节不变，等价性由 useAskIncrRender.spec 钉死「任意前缀
// incremental === 全量 renderMd(stripImg(raw))」）。
//
// 旧实现每 tick 对全文跑 stripImg + renderMd（O(n²)）：答案越长每帧越贵。这里把两步都改成只处理增量：
//  · strip：raw 只追加 → 已定稿前缀的剥离结果缓存复用；末尾"可能长成完整 <<IMG:N>> 标记的半截"持留
//    不定稿（持留判定与 stripImg 的半截擦除同构，且持留全部完整标记的真前缀 → 标记绝不被定稿边界切开）；
//  · renderMd：逐行渲染彼此独立（``` 围栏跨行除外）→ 已完结行的 HTML 缓存复用，每 tick 只重渲最后
//    一个未完结行；若尾部处于未闭合围栏内，回退到从围栏起点所在行整段重渲（表格/列表本就逐行独立）。
// 任何越界/回退情况一律重置缓存走全量；定稿帧（finishStream/stop/content_blocks 后）永远是全量权威
// renderMd，流式期的任何视觉瑕疵都会被定稿修正。
import { renderMd } from '@/lib/markdown'

export interface PumpChState {
  stripLen: number   // raw 已定稿剥离到的位置（保证不切开任何潜在 <<IMG>> 标记）
  stripOut: string   // raw[0, stripLen) 的剥离结果（只追加）
  mdLen: number      // 已定稿渲染的源前缀长度（行边界，且前缀内围栏闭合）
  mdHtml: string     // 该前缀的 HTML（只追加）
}

export function newPumpState(): PumpChState {
  return { stripLen: 0, stripOut: '', mdLen: 0, mdHtml: '' }
}

// #F-mm6b 必须与 markdown.ts 权威 stripImg 同构：含图级子下标分支 (?:\.\d+)?，否则开
// RAG_IMG_SUBINDEX 后流式途中 <<IMG:1.2>> 既不被擦除也不被持留，明文碎片外露（收尾才清）。
const IMG_FULL_RE = /<{1,2}IMG:\d+(?:\.\d+)?>{1,2}/g
// 持留判定：末尾可能是半截 <<IMG:N>> / <<IMG:N.M>> 标记（含只差一个 ">" 的 "<<IMG:1>"、
// 只差子下标数字的 "<<IMG:1."）——这些是完整标记的真前缀，先不定稿；等后续字符到齐后
// 要么整体按完整标记删除，要么证伪后原样放行。
const IMG_HOLD_RE = /<{1,2}(IMG:\d+(?:\.\d*)?>?|I?M?G?:?\d*\.?\d*)$/

export function stripImgIncr(st: PumpChState, raw: string): string {
  if (raw.length < st.stripLen) { st.stripLen = 0; st.stripOut = ''; st.mdLen = 0; st.mdHtml = '' }   // 防御：源只增不减
  const tail = raw.slice(st.stripLen)
  const m = IMG_HOLD_RE.exec(tail.length > 64 ? tail.slice(-64) : tail)
  const upTo = raw.length - (m ? m[0].length : 0)
  if (upTo > st.stripLen) {
    st.stripOut += raw.slice(st.stripLen, upTo).replace(IMG_FULL_RE, '')
    st.stripLen = upTo
  }
  return st.stripOut   // 持留区不显示 —— 与 stripImg 擦掉末尾半截标记的口径一致
}

export function renderMdIncr(st: PumpChState, shown: string): string {
  if (shown.length < st.mdLen) { st.mdLen = 0; st.mdHtml = '' }   // 防御：拿不准回退全量
  const nl = shown.lastIndexOf('\n') + 1        // 候选定稿边界：最后一个完整行之后
  if (nl > st.mdLen) {
    // 不变式：[0, mdLen) 内围栏闭合。只扫增量里的 ``` 定奇偶；未闭合围栏 → 边界退到围栏起点所在行首。
    let open = false
    let openPos = -1
    let i = st.mdLen
    for (;;) {
      const p = shown.indexOf('```', i)
      if (p < 0 || p >= nl) break
      open = !open
      if (open) openPos = p
      i = p + 3
    }
    const bound = open ? shown.lastIndexOf('\n', openPos) + 1 : nl
    if (bound > st.mdLen) {
      const seg = shown.slice(st.mdLen, bound)
      if (seg.trim()) st.mdHtml += renderMd(seg)   // 空白段不渲（避免拼进 renderMd 的 <p></p> 空兜底）
      st.mdLen = bound
    }
  }
  const tailPart = shown.slice(st.mdLen)
  return (st.mdHtml + (tailPart.trim() ? renderMd(tailPart) : '')) || '<p></p>'
}
