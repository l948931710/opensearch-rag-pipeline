import { describe, expect, it } from 'vitest'
import { createSseDecoder } from '@/lib/sseDecoder'

const enc = new TextEncoder()
const b = (s: string) => enc.encode(s)

describe('createSseDecoder（refinement#7 流式边界）', () => {
  it('单块多帧粘连 → 逐帧出齐', () => {
    const d = createSseDecoder()
    const out = d.push(b('data: {"type":"session","session_id":"s1","message_id":"m1"}\n\ndata: {"type":"chunk","content":"a"}\n\n'))
    expect(out.map((e) => e.type)).toEqual(['session', 'chunk'])
    expect(out[0].session_id).toBe('s1')
    expect(out[1].content).toBe('a')
  })

  it('帧跨块切断 → 凑齐 \\n\\n 才出帧', () => {
    const d = createSseDecoder()
    expect(d.push(b('data: {"type":"chunk","content":"hi"}'))).toEqual([])   // 无分隔符，暂存
    const out = d.push(b('\n\ndata: [DONE]\n\n'))
    expect(out.map((e) => e.type)).toEqual(['chunk', '__done'])
  })

  it('中文 UTF-8 多字节跨块切断 → 不乱码', () => {
    const d = createSseDecoder()
    const full = b('data: {"type":"chunk","content":"抱歉"}\n\n')
    const k = full.findIndex((x) => x >= 0x80)   // 首个非 ASCII 字节（汉字起点）
    const cut = k + 1                            // 切在汉字中间
    expect(d.push(full.slice(0, cut))).toEqual([])
    const out = d.push(full.slice(cut))
    expect(out).toHaveLength(1)
    expect(out[0].content).toBe('抱歉')
  })

  it('[DONE] 是字面量 → __done，绝不当 JSON 解析', () => {
    const d = createSseDecoder()
    expect(d.push(b('data: [DONE]\n\n'))).toEqual([{ type: '__done' }])
  })

  it('坏 JSON 帧跳过、不抛、不打断后续', () => {
    const d = createSseDecoder()
    const out = d.push(b('data: {坏的 json\n\ndata: {"type":"chunk","content":"ok"}\n\n'))
    expect(out.map((e) => e.type)).toEqual(['chunk'])
  })

  it('非 data: 行 / 空 payload 跳过（无 event:/心跳）', () => {
    const d = createSseDecoder()
    const out = d.push(b('event: ping\n\ndata: \n\ndata: {"type":"done","guard":true}\n\n'))
    expect(out.map((e) => e.type)).toEqual(['done'])
    expect(out[0].guard).toBe(true)
  })

  it('flush 不吐半帧（断流保护）', () => {
    const d = createSseDecoder()
    d.push(b('data: {"type":"chunk","content":"partial"}'))   // 无 \n\n
    expect(d.flush()).toEqual([])
  })

  it('完整序列分散在不规则字节块里仍正确还原', () => {
    const d = createSseDecoder()
    const full = b(
      'data: {"type":"session","session_id":"s","message_id":"m"}\n\n' +
      'data: {"type":"sources","sources":[{"doc_id":"d","title":"t","level":"high","score":9}]}\n\n' +
      'data: {"type":"chunk","content":"步骤一"}\n\n' +
      'data: {"type":"done","model":"q","usage":{},"guard":false}\n\n' +
      'data: [DONE]\n\n',
    )
    const seen: string[] = []
    for (let i = 0; i < full.length; i += 7) seen.push(...d.push(full.slice(i, i + 7)).map((e) => e.type as string))
    seen.push(...d.flush().map((e) => e.type as string))
    expect(seen).toEqual(['session', 'sources', 'chunk', 'done', '__done'])
  })
})
