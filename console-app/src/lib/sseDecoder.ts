// 纯 SSE 帧解码器（POST /api/ask/stream 用 fetch + ReadableStream 手搓，EventSource 不支持 POST）。
// 把字节块累积、按 SSE 空行(\n\n)切帧、剥 "data: " 前缀、解析 JSON；[DONE] 字面量哨兵 → {type:'__done'}。
// 关键正确性（refinement#7，全部在此处单测）：
//   · 帧跨块切断 → buffer 暂存到 \n\n 才出帧
//   · 多帧粘在一块 → while 循环逐帧出
//   · 中文 UTF-8 多字节跨块切断 → TextDecoder({stream:true}) 增量解码不乱码
//   · [DONE] 不是 JSON → 先判等再 parse
//   · 半截/坏 JSON 帧 → 跳过不抛
// 与编排无关的边界（重复 done、<<IMG>> 跨帧）由调用方（useAsk）处理，不在解码层。

export interface SseEvent { type: string; [k: string]: unknown }

export interface SseDecoder {
  /** 推入一段字节，返回此刻已完整的事件（0..N 个）。 */
  push(chunk: Uint8Array): SseEvent[]
  /** 流结束时调用：flush TextDecoder 余字节并出齐剩余完整帧（不吐半帧）。 */
  flush(): SseEvent[]
}

function parseFrame(rawLine: string): SseEvent | null {
  const line = rawLine.trim()
  if (line.indexOf('data:') !== 0) return null            // 只认 data: 行（无 event:/id:/retry:/心跳）
  const payload = line.slice(5).trim()
  if (!payload) return null
  if (payload === '[DONE]') return { type: '__done' }      // 字面量哨兵，非 JSON
  try { return JSON.parse(payload) as SseEvent } catch { return null }  // 坏帧跳过，不打断流
}

export function createSseDecoder(): SseDecoder {
  const decoder = new TextDecoder()   // utf-8；stream:true 处理多字节跨块
  let buf = ''

  function drain(): SseEvent[] {
    const out: SseEvent[] = []
    let i: number
    while ((i = buf.indexOf('\n\n')) >= 0) {   // SSE 帧以空行分隔
      const ev = parseFrame(buf.slice(0, i))
      buf = buf.slice(i + 2)
      if (ev) out.push(ev)
    }
    return out
  }

  return {
    push(chunk) {
      buf += decoder.decode(chunk, { stream: true })
      return drain()
    },
    flush() {
      buf += decoder.decode()   // flush 残留多字节
      return drain()            // 只出完整(\n\n)帧；半帧丢弃
    },
  }
}
