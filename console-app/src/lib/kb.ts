// 知识库管理：纯常量 + 工具（直传 OSS、报错转人话、查重文案、徽章配色）。可独立单测。
// 后端契约见 /api/kb/*；徽章【唯一真相在后端 _kb_status_badge】，前端只展示字符串 + 本地配色。

export const MAX_UPLOAD_MB = 50   // 必须与后端 kb_upload.MAX_UPLOAD_BYTES 对齐（否则传完才 413）
export const UPLOAD_ACCEPT = '.pdf,.docx,.xlsx,.pptx,.jpg,.jpeg,.png'
// 受支持扩展名（= UPLOAD_ACCEPT 拆分；后端 validate_filename 为权威，前端仅预检省一次失败往返）。
export const UPLOAD_EXTS = UPLOAD_ACCEPT.split(',')

/** 取文件扩展名（小写，含点），无扩展名返回 ''。 */
export function extOf(filename: string): string {
  const m = /\.[^.]+$/.exec(String(filename || ''))
  return m ? m[0].toLowerCase() : ''
}

/** 列出不在受支持扩展名内的文件名（拖拽绕过 input accept 时的客户端预检）。 */
export function unsupportedNames(files: Array<{ name: string }>): string[] {
  return files.filter((f) => !UPLOAD_EXTS.includes(extOf(f.name))).map((f) => f.name)
}

// 部门 ACL 组码 → 中文（owner_dept 存组码）。
export const GROUP_LABEL: Record<string, string> = {
  finance: '财务', it: '信息技术', marketing: '营销', production: '生产', pmc: '计划PMC',
  admin: '行政', hr: '人力资源', rd: '研发', quality: '品质技术', supply: '资材供应',
}
export const deptLabel = (code: string) => GROUP_LABEL[code] || code

// 可见范围。
export const PERM_LABEL: Record<string, string> = {
  dept_internal: '仅本部门', public: '全公司', restricted: '受限',
}
export const permLabel = (p: string) => PERM_LABEL[p] || p

// 角色 → 中文。
export const ROLE_LABEL: Record<string, string> = {
  kb_admin: '知识库管理员', dept_admin: '部门管理员', employee: '员工',
}

// 文档状态徽章 → 色调键（组件据此取 st-* 颜色）。未命中 → muted。
const BADGE_TONE: Record<string, string> = {
  已上线: 'live', 处理中: 'busy', 排队中: 'queue', 待审核: 'warn',
  已隔离: 'fail', 处理失败: 'fail', 已驳回: 'fail', 已退役: 'muted', 内容未变: 'muted',
}
export const badgeTone = (badge: string) => BADGE_TONE[badge] || 'muted'

// 上传队列内部态（批量行用）→ 色调；与文档徽章是两套独立状态机，勿合并。
const QBADGE_TONE: Record<string, string> = {
  已提交: 'live', 失败: 'fail', 跳过: 'fail', 上传中: 'busy', 登记中: 'busy', 排队: 'queue',
}
export const qBadgeTone = (s: string) => QBADGE_TONE[s] || 'queue'

// 轮询终态：命中即停（含已退役，不含待审核——待审核要等人审，根本不轮询）。
export const TERMINAL_BADGES = ['已上线', '处理失败', '已隔离', '已驳回', '内容未变', '已退役']

/**
 * 直传 OSS：fetch 无法上报上传进度，故用 XHR 接 upload.onprogress。File 必须留闭包、勿进 Vue 响应式
 * （Proxy 包裹会破坏 xhr.send(file)）。timeout 25min（略小于令牌 30min TTL）。
 */
export function putWithProgress(url: string, file: File, onProg?: (pct: number) => void, contentType?: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', url, true)
    // G4：upload-url 已把 Content-Type 签入 URL → 必须发完全一致的头，否则 OSS 签名校验 403。
    if (contentType) xhr.setRequestHeader('Content-Type', contentType)
    if (xhr.upload) {
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProg) onProg(Math.round((e.loaded * 100) / e.total)) }
    }
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300
      ? resolve()
      : reject(new Error('OSS PUT 失败 HTTP ' + xhr.status)))
    xhr.onerror = () => reject(new Error('OSS PUT 网络错误（可能是 OSS 桶未对本页来源放行 CORS PUT）'))
    xhr.ontimeout = () => reject(new Error('OSS PUT 超时'))
    xhr.timeout = 25 * 60 * 1000
    xhr.send(file)
  })
}

/** 把 OSS/CORS/413/trace 等技术错误转成管理员可操作的人话（绝不暴露原始 HTTP/trace 串）。 */
export function uploadErrText(e: any): string {
  const msg = (e && e.message) || String(e || '')
  const status = e && e.status
  if (status === 413 || /超过大小上限|too large|413/i.test(msg)) return `文件超过上限 ${MAX_UPLOAD_MB}MB，请压缩或拆分后重传。`
  if (status === 403 || /无权|权限|forbidden/i.test(msg)) return '你没有该操作的权限，请联系知识库管理员。'
  if (/OSS PUT|CORS|网络错误|超时|timeout/i.test(msg)) return '文件上传通道异常，请稍后重试；若持续失败请联系知识库管理员（可能是 OSS 跨域未放行）。'
  if (/未检测到已上传|请先完成直传|过期/i.test(msg)) return '上传未完成或链接已过期，请重新选择文件上传。'
  if (/空/.test(msg)) return '所选文件为空，请检查后重传。'
  return '上传失败，请稍后重试；若持续失败请联系知识库管理员。'
}

export interface DupDoc { doc_id: string; title: string; owner_dept: string }

/** register 返回的 ETag 内容查重 → 提示文案（advisory，不阻断上传）。无命中返回空串。 */
export function buildDupMsg(dups: DupDoc[] | undefined, other: number | undefined): string {
  const parts: string[] = []
  if (dups && dups.length) {
    const names = dups.map((d) => `《${d.title || d.doc_id}》（${deptLabel(d.owner_dept)}）`).join('、')
    parts.push(`相同内容的文档已存在：${names}。如确属重复，可在「我的文档」对其退役。`)
  }
  if (other && other > 0) parts.push(`另有 ${other} 篇相同内容在你管理范围外的部门。`)
  return parts.join(' ')
}

/** 文件名去扩展名取 core（用于文件名级预查重 onFile）。 */
export function fileCore(filename: string): string {
  return String(filename || '').replace(/\.[^.]+$/, '').trim()
}
