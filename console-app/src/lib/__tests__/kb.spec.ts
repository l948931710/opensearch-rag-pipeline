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
    expect(badgeTone('已隔离')).toBe('fail')
    expect(badgeTone('待审核')).toBe('warn')
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
