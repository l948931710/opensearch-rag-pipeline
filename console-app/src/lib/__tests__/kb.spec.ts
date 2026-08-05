import { describe, expect, it, vi } from 'vitest'
import { uploadErrText, buildDupMsg, fileCore, badgeTone, deptLabel, permLabel, extOf, unsupportedNames, putWithProgress } from '@/lib/kb'

describe('uploadErrText（技术错误 → 人话，绝不暴露 trace/HTTP）', () => {
  it('413/超大 → 大小提示', () => {
    expect(uploadErrText({ status: 413 })).toContain('50MB')
    expect(uploadErrText(new Error('文件超过大小上限'))).toContain('50MB')
  })
  it('403/无权 → 权限提示', () => {
    expect(uploadErrText({ status: 403 })).toContain('权限')
  })
  it('OSS PUT / CORS / 超时 → 通道异常', () => {
    expect(uploadErrText(new Error('OSS PUT 网络错误（可能是 OSS 桶未对本页来源放行 CORS PUT）'))).toContain('上传通道异常')
    expect(uploadErrText(new Error('OSS PUT 超时'))).toContain('上传通道异常')
  })
  it('未含原始 trace 串', () => {
    expect(uploadErrText(new Error('登记失败 (trace: abcd1234)'))).not.toContain('trace')
  })

  // ── 2026-08-04 全员上传中断的回归面 ──────────────────────────────────────
  // 那次 node 归属登记撞生产 owner_dept NOT NULL 漂移（1048 → 500），被兜底档压成
  // 「请稍后重试」，与"限流""网络抖动"完全不可区分。以下三档就是为了让它们分开。
  it('429 → 「太频繁」且带 Retry-After 秒数（此前掉进兜底，长得像真故障）', () => {
    const s = uploadErrText({ status: 429, message: '操作太频繁，请稍后再试', retryAfter: 37 })
    expect(s).toContain('太频繁')
    expect(s).toContain('37')
    expect(s).not.toContain('上传失败，请稍后重试')
  })
  it('429 无 Retry-After → 回落 60 秒，不出现 NaN/undefined', () => {
    const s = uploadErrText({ status: 429, message: '操作太频繁，请稍后再试' })
    expect(s).toContain('60')
    expect(s).not.toMatch(/NaN|undefined/)
  })
  it('5xx → 明说「服务端处理失败」，与「稍后重试」可区分', () => {
    const s = uploadErrText({ status: 500, message: '登记失败 (trace: abcd1234)' })
    expect(s).toContain('服务端处理失败')
    expect(s).not.toContain('trace')          // 默认档（员工侧）仍不外泄
  })
  it('5xx + detail:true（管理台）→ 附后端原因与 trace，供报障', () => {
    const s = uploadErrText({ status: 500, message: '登记失败 (trace: abcd1234)' }, { detail: true })
    expect(s).toContain('登记失败')
    expect(s).toContain('abcd1234')
  })
  it('detail:true 仍剥掉裸 HTTP 码，且不把 `HTTP 502` 当原因直出', () => {
    expect(uploadErrText({ status: 502, message: 'HTTP 502' }, { detail: true })).toContain('服务端处理失败')
    expect(uploadErrText({ status: 400, message: 'HTTP 400' }, { detail: true }))
      .toBe('上传失败，请稍后重试；若持续失败请联系知识库管理员。')
  })
  it('fetch 层网络中断 → 网络文案，不再伪装成通用失败', () => {
    expect(uploadErrText(new TypeError('Failed to fetch'))).toContain('网络中断')
  })
  it('优先级：413/403 仍先于 5xx 命中', () => {
    expect(uploadErrText({ status: 413, message: '文件超过大小上限' })).toContain('50MB')
    expect(uploadErrText({ status: 403, message: '无权上传：owner_dept_not_managed' })).toContain('权限')
  })
})

describe('buildDupMsg（ETag 内容查重提示，advisory）', () => {
  it('可见命中 → 列出《标题》（部门）', () => {
    const s = buildDupMsg([{ doc_id: 'd1', title: '年假制度', owner_dept: 'hr' }], 0)
    expect(s).toContain('《年假制度》')
    expect(s).toContain('人力资源')
    expect(s).toContain('退役')
  })
  it('范围外 → 仅计数不泄露', () => {
    const s = buildDupMsg([], 3)
    expect(s).toContain('3 篇')
    expect(s).not.toContain('《')
  })
  it('无命中 → 空串', () => {
    expect(buildDupMsg([], 0)).toBe('')
    expect(buildDupMsg(undefined, undefined)).toBe('')
  })
})

describe('fileCore / badgeTone / labels', () => {
  it('fileCore 去扩展名', () => {
    expect(fileCore('年假制度.pdf')).toBe('年假制度')
    expect(fileCore('a.b.docx')).toBe('a.b')
    expect(fileCore('noext')).toBe('noext')
  })
  it('badgeTone 映射', () => {
    expect(badgeTone('已上线')).toBe('live')
    expect(badgeTone('处理失败')).toBe('fail')
    expect(badgeTone('已隔离')).toBe('hold')   // 安全隔离专属紫，与工作流驳回红分家
    expect(badgeTone('待审核')).toBe('queue')  // 良性等待族，与异常「未入索引」(warn) 分家
    expect(badgeTone('未入索引')).toBe('warn')
    expect(badgeTone('已退役')).toBe('muted')
    expect(badgeTone('内容未变')).toBe('muted')
    expect(badgeTone('未知态')).toBe('muted')
  })
  it('deptLabel / permLabel', () => {
    expect(deptLabel('hr')).toBe('人力资源')
    expect(deptLabel('unknown')).toBe('unknown')
    expect(permLabel('dept_internal')).toBe('仅本部门')
    expect(permLabel('public')).toBe('全公司')
  })
  it('putWithProgress 发签入的 Content-Type 头（G4）；缺省则不显式设头', async () => {
    const headers: Record<string, string> = {}
    class FakeXHR {
      upload: any = {}
      status = 200; timeout = 0
      onload: any = null; onerror: any = null; ontimeout: any = null
      open() {}
      setRequestHeader(k: string, v: string) { headers[k] = v }
      send() { if (this.onload) this.onload() }
    }
    vi.stubGlobal('XMLHttpRequest', FakeXHR as any)
    await putWithProgress('https://oss/x', new File([new Uint8Array(3)], 'a.pdf'), undefined, 'application/pdf')
    expect(headers['Content-Type']).toBe('application/pdf')   // 与 URL 签名一致，否则 OSS 403

    const h2: Record<string, string> = {}
    class FakeXHR2 extends FakeXHR { setRequestHeader(k: string, v: string) { h2[k] = v } }
    vi.stubGlobal('XMLHttpRequest', FakeXHR2 as any)
    await putWithProgress('https://oss/x', new File([new Uint8Array(3)], 'a.pdf'))
    expect(h2['Content-Type']).toBeUndefined()               // 未给 → 不显式设头
  })
  it('extOf / unsupportedNames（客户端扩展名预检，G9）', () => {
    expect(extOf('a.PDF')).toBe('.pdf')          // 小写归一
    expect(extOf('a.b.docx')).toBe('.docx')      // 取最后一段
    expect(extOf('noext')).toBe('')
    expect(unsupportedNames([{ name: 'a.pdf' }, { name: 'b.png' }])).toEqual([])
    expect(unsupportedNames([{ name: 'a.pdf' }, { name: 'm.zip' }, { name: 'x.exe' }])).toEqual(['m.zip', 'x.exe'])
  })
})
