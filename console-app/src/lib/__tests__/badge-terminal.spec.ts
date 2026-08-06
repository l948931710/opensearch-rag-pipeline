import { describe, it, expect } from 'vitest'
import { TERMINAL_BADGES, BADGE_TONE, badgeTone } from '../kb'

// 后端 api.py::_KB_BADGE_VOCAB 的镜像。后端加词而这里不加 ⇒ 本文件红，
// 与后端的 test_frontend_backend_badge_vocab_and_bad_badges_parity 互为犄角。
const BACKEND_VOCAB = [
  '已退役', '已隔离', '未入索引', '已上线', '处理失败',
  '已驳回', '内容未变', '待审核', '排队中', '处理中',
  '内容不符', '历史版本',
]

describe('徽章词表 seam', () => {
  it('BADGE_TONE 键集 == 后端封闭词表（漏一个词 → 该徽章静默落 muted，看不出是漏同步）', () => {
    expect(Object.keys(BADGE_TONE).sort()).toEqual([...BACKEND_VOCAB].sort())
  })

  it('「历史版本」是正常生命周期终点 → muted，绝不用 warn/fail（那是异常态的色）', () => {
    expect(badgeTone('历史版本')).toBe('muted')
  })

  it('★「内容不符」必须是终态：它是后端自陈的**不可自动重试**安全终态，'
     + '此前漏登记 ⇒ trackStatus 命中后仍轮询到 22 次上限才放弃', () => {
    expect(TERMINAL_BADGES).toContain('内容不符')
  })

  it('★「历史版本」必须是终态：轮询中的版本被另一次成功升版取代时应当即停手', () => {
    expect(TERMINAL_BADGES).toContain('历史版本')
  })

  it('终态集 ⊆ 词表；且「处理中/排队中」这类进行态绝不在其中（否则轮询秒停）', () => {
    for (const b of TERMINAL_BADGES) expect(BACKEND_VOCAB).toContain(b)
    for (const b of ['处理中', '排队中', '待审核']) expect(TERMINAL_BADGES).not.toContain(b)
  })
})
