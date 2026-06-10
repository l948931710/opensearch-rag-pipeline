// Markdown-subset parser for answer text blocks — the AXML port of the
// prototype's typewriter-bold fix (prototype/index.html typeText()/STEP_RE).
//
// The prototype converts **x** to <strong> BEFORE typing and emits tags as
// whole tokens so a half-revealed pair can never show. AXML has no innerHTML,
// so the equivalent (and stronger) approach used here is: parse ONCE into
// styled runs, then let the typewriter reveal PLAIN characters only — markers
// never enter the reveal stream at all.
//
// Subset (matches what the backend LLM actually emits, see llm_generator
// answer rules: **加粗** / **第N步** steps / lists; plus `#` headings as a
// degrade-to-bold fallback). Unclosed ** stays literal, same as the prototype.

// 步骤段判定（与原型 STEP_RE 逐字一致）：第N步（含未空格的「第1步」）/ ①-⑳ 圆数字
// 开头（可带 ** 前缀）/ 编号加粗列表项「1. **xxx**」。不可用裸 ^\*\* —— 否则任何
// 加粗开头的段落（如 **注意**）都会挂上步骤标尺。
const STEP_RE = /^(?:(?:\*\*)?(?:第\s*\d+\s*步|[①-⑳])|\d+[.、]\s*\*\*)/;
const BOLD_RE = /\*\*(.+?)\*\*/g;

/**
 * Parse one text block into styled paragraphs.
 *
 * @param {string} raw  block text (may contain \n\n paragraph breaks)
 * @returns {Array<{key:string, step:boolean, heading:boolean,
 *                  runs:Array<{key:string,text:string,bold:boolean,brand:boolean}>,
 *                  plainLen:number}>}
 */
export function parseTextBlock(raw) {
  // 行首列表星号/短横 → 「· 」；(?!\*) 不误伤行首 **加粗**（原型同款容错 + '-' 列表）
  const cleaned = String(raw == null ? '' : raw).replace(/^[ \t]*[*-](?!\*)\s+/gm, '· ');
  const paras = cleaned.split(/\n{2,}/).map((s) => s.trim()).filter(Boolean);

  return paras.map((para, pi) => {
    let p = para;
    let heading = false;
    if (/^#{1,6}\s+/.test(p)) {
      // 没有标题字号体系（锁定的设计只有正文/加粗），# 标题降级为加粗段
      heading = true;
      p = p.replace(/^#{1,6}\s+/, '');
    }

    // 步骤判定看原始段落（** 前缀也算，见 STEP_RE）
    const step = STEP_RE.test(para);

    const runs = [];
    let last = 0;
    let m;
    BOLD_RE.lastIndex = 0;
    while ((m = BOLD_RE.exec(p)) !== null) {
      if (m.index > last) {
        runs.push({ text: p.slice(last, m.index), bold: false, brand: false });
      }
      runs.push({ text: m[1], bold: true, brand: false });
      last = m.index + m[0].length;
    }
    if (last < p.length) {
      runs.push({ text: p.slice(last), bold: false, brand: false });
    }

    // 步骤段第一个加粗（**第N步**）标 brand 色 —— 对应原型 p.step strong:first-child
    if (step) {
      const firstBold = runs.find((r) => r.bold);
      if (firstBold) {
        firstBold.brand = true;
      }
    }

    runs.forEach((r, ri) => {
      r.key = 'r' + ri;
    });
    const plainLen = runs.reduce((n, r) => n + r.text.length, 0);
    return { key: 'p' + pi, step, heading, runs, plainLen };
  });
}

/** Total plain-character length across paragraphs (typewriter segment length). */
export function plainLength(paras) {
  return paras.reduce((n, p) => n + p.plainLen, 0);
}

/** Concatenated plain text — what the typewriter actually iterates over. */
export function plainText(paras) {
  return paras.map((p) => p.runs.map((r) => r.text).join('')).join('');
}

/**
 * Cut parsed paragraphs at `count` revealed plain characters. Runs keep their
 * bold/brand styling even when partially revealed — the typewriter can never
 * expose a literal `**`.
 */
export function sliceParas(paras, count) {
  const out = [];
  let left = count;
  for (let i = 0; i < paras.length; i++) {
    const para = paras[i];
    if (left <= 0) {
      break;
    }
    if (left >= para.plainLen) {
      out.push(para);
      left -= para.plainLen;
      continue;
    }
    const runs = [];
    for (let j = 0; j < para.runs.length && left > 0; j++) {
      const r = para.runs[j];
      if (left >= r.text.length) {
        runs.push(r);
      } else {
        runs.push({ key: r.key, text: r.text.slice(0, left), bold: r.bold, brand: r.brand });
      }
      left -= r.text.length;
    }
    out.push({ key: para.key, step: para.step, heading: para.heading, runs, plainLen: count });
    break;
  }
  return out;
}
