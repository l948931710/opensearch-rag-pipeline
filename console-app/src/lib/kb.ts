// 知识库管理：纯常量 + 工具（直传 OSS、报错转人话、查重文案、徽章配色）。可独立单测。
// 后端契约见 /api/kb/*；徽章【唯一真相在后端 _kb_status_badge】，前端只展示字符串 + 本地配色。

// 仅作**兜底**：真值来自 /api/kb/config 的 max_upload_bytes（useKb.maxUploadBytes 优先取它）。
// 保持与后端 kb_upload.MAX_UPLOAD_BYTES 的默认值同步，否则 config 未回的那一小段窗口里
// 客户端预检会用错数（放行了后端要 413 的文件，或反过来白拦）。
// 50 →(08-06)→ 150 →(08-05)→ 300，每次都随后端 kb_upload.MAX_UPLOAD_BYTES 同步改。
export const MAX_UPLOAD_MB = 300
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

// 部门 ACL 组码 → 中文（owner_dept 存组码）。2026-07-03 扩容 5 组（与 retriever._VALID_ACL_GROUPS 同步）。
export const GROUP_LABEL: Record<string, string> = {
  finance: '财务', it: '信息技术', marketing: '营销', production: '生产', pmc: '生产计划部',
  admin: '行政', hr: '人力资源', rd: '研发', quality: '品质技术', supply: '资材供应',
  overseas: '海外', audit: '审计', legal: '法务', engineering: '工程', corn_eco: '玉米环保',
  sales: '销售', logistics: '物流',
}
// 生产子线是合法 owner_dept（永不归并回 production）——只做展示映射，
// 不并入 GROUP_LABEL：CONTRIB_DEPT_OPTS 以 GROUP_LABEL 的键为贡献归属选项，子线不应出现在下拉里。
export const SUBDEPT_LABEL: Record<string, string> = {
  production_mold: '生产·模具', production_paper_cup: '生产·纸杯', production_thermoforming: '生产·吸塑',
  production_injection: '生产·注塑', production_straw: '生产·吸管',
  // 2026-07-20 拍板开通的三条子线(retriever._PRODUCTION_UMBRELLA_OWNERS 6→9 同批)
  production_blown_film: '生产·吹膜', production_carton: '生产·纸箱',
  production_pulp_molding: '生产·纸浆模塑',
}
export const deptLabel = (code: string) => GROUP_LABEL[code] || SUBDEPT_LABEL[code] || code

// 可见范围。
export const PERM_LABEL: Record<string, string> = {
  dept_internal: '仅本部门', public: '全公司', restricted: '受限',
}
export const permLabel = (p: string) => PERM_LABEL[p] || p

// 「谁能看到」解释器：读者来源 → 中文短标（与后端 KbVisibilityReader.via 对齐）。
export const VIA_LABEL: Record<string, string> = {
  owner: '归属部门', umbrella: '生产伞组', shared_policy: '营销共享面', grant: '跨部门授权',
}
export const viaLabel = (v: string) => VIA_LABEL[v] || v

// 角色 → 中文。
export const ROLE_LABEL: Record<string, string> = {
  kb_admin: '知识库管理员', dept_admin: '部门管理员', employee: '员工',
}

// 时间戳显示归一 → `YYYY-MM-DD HH:MM`。后端有两种源：MySQL str(datetime) 空格分隔、
// contribution.py isoformat() T 分隔——裸 `.slice(0,16)` 对后者留 T（'2026-06-20T10:15'），
// 必须先换空格再截。纯日期（'2026-06-20'）或相对文案（'刚刚'）原样通过。
export const fmtTs = (v: string | null | undefined): string => (v || '').replace('T', ' ').slice(0, 16)

// 限流拒绝原因机器码 → 中文（schema/017 qa_admission_reject.reason 枚举；rate_limiter 只落机器码，
// 前端此前无任何映射层——批次γ D3 首个消费方）。未知码原样透出，不猜。
export const ADMISSION_REASON_LABEL: Record<string, string> = {
  global_cap: '全局日熔断', per_min: '单人每分钟上限', per_day: '单人每日上限',
  thinking_quota: '深度思考日配额', thinking_anon: '匿名深度思考', thinking_off: '深度思考未开放',
  aux_per_min: '辅助接口每分钟上限',
  // 2026-08-06 补齐:auth_per_min 是当天新增的登录独立桶——**分桶的观测收益全靠这一行**
  // (台账能区分"登录挤爆"与"控制台在刷",否则又要去翻 SLS);general_* 三个是早先就漏的。
  // ⚠️ 新增 Denial reason 码时必须同步本表——tests/test_admission_reason_parity.py 会红。
  auth_per_min: '登录换令牌每分钟上限',
  general_quota: '通用问答日配额', general_anon: '匿名通用问答', general_off: '通用能力未开放',
}
export const admissionReasonLabel = (r: string) => ADMISSION_REASON_LABEL[r] || r

// 文档状态徽章 → 色调键（组件据此取 st-* 颜色）。未命中 → muted。
// 色彩语义分家（P2 同色复用修复）：待审核=良性管道等待 → 与排队中同 queue 族（原与
// 未入索引同蓝，异常/正常不可分）；已隔离=安全隔离 → 专属 hold 紫（PII 隔离≠工作流
// 驳回，原同红）；蓝 warn 从此唯一=未入索引，红 fail=处理失败/已驳回。
// ⚠️ 词表 seam（批次ε-5 R2）：键集=后端 api.py::_KB_BADGE_VOCAB 封闭集（测试
// test_kb_status_badge_closed_set 锁后端、contribute.spec「台账词表 seam 锁」锁本表）——
// 后端新增/改名徽章词必须同步这里 + MyContributions displayState 特判词。
export const BADGE_TONE: Record<string, string> = {
  已上线: 'live', 处理中: 'busy', 排队中: 'queue', 待审核: 'queue', 未入索引: 'warn',
  已隔离: 'hold', 处理失败: 'fail', 已驳回: 'fail', 已退役: 'muted', 内容未变: 'muted',
  // C8（2026-08-04）：审批放行的字节 ≠ 摄取到的字节。用 'fail' 而非 'warn'——它是
  // **不可自动重试**的安全终态，唯一出路是重新上传形成新版本，不是"等一等会好"。
  内容不符: 'fail',
}
export const badgeTone = (badge: string) => BADGE_TONE[badge] || 'muted'

// 知识贡献的 5 态徽章（state 码由后端 contribution_state 折叠 review/ingestion 两生命周期而来）
// → 文案 + 色调键（与 StatusPill 的 st-* 同一套）。与文档/队列徽章独立，勿合并；
// 但**同词同色**：待审核/已驳回 与文档徽章对齐（原 pending=蓝、rejected=灰，跨页同词不同色打乱心智）。
export const CONTRIB_STATE: Record<string, { label: string; tone: string }> = {
  pending: { label: '待审核', tone: 'queue' },
  registering: { label: '已采纳·待入库', tone: 'busy' },
  searchable: { label: '已入库', tone: 'live' },
  failed: { label: '入库失败', tone: 'fail' },
  rejected: { label: '已驳回', tone: 'fail' },
  // 批次ε-3 R1 + ε-5 R1：registering 的展示细分（后端 state 码不变，由 doc_badge 派生）——
  // 待放行=卡 kb_admin 审批（等人）；入库受阻=隔离/空块/处理失败死链（重试不自愈或作者无权重试，
  // 改稿重投）；同内容已在库=良性去重事实（muted，非故障，不给重投——原样重投仍判重复）。
  pending_approval: { label: '已采纳·待放行', tone: 'busy' },
  ingest_stalled: { label: '入库受阻', tone: 'fail' },
  ingest_skipped_duplicate: { label: '同内容已在库', tone: 'muted' },
}
export const contribStateLabel = (s: string) => CONTRIB_STATE[s]?.label || s
export const contribStateTone = (s: string) => CONTRIB_STATE[s]?.tone || 'muted'

// 窗口天数 → 展示文案（批次ε-4）：整年折自然单位「近一年」，其余沿用全站「近 N 天」惯例。
// 收敛成纯函数——别在模板里内联三元（何时特殊化的判断要可单测、不散落成魔法数字）。
export const fmtWindowDays = (days: number) => (days >= 365 ? '近一年' : `近 ${days} 天`)

// 缺口来源 → 中文短标。no_result=检索没召回（缺文档）；refusal=召回了但没答好（深度/口径差）。
export const GAP_KIND_LABEL: Record<string, string> = {
  no_result: '没有相关文档', refusal: '答案不够好',
}
export const gapKindLabel = (k: string) => GAP_KIND_LABEL[k] || ''

// 上传队列内部态（批量行用）→ 色调；与文档徽章是两套独立状态机，勿合并。
const QBADGE_TONE: Record<string, string> = {
  已提交: 'live', 失败: 'fail', 跳过: 'fail', 上传中: 'busy', 登记中: 'busy', 排队: 'queue',
}
export const qBadgeTone = (s: string) => QBADGE_TONE[s] || 'queue'

// 轮询终态：命中即停（含已退役，不含待审核——待审核要等人审，根本不轮询）。
export const TERMINAL_BADGES = ['已上线', '未入索引', '处理失败', '已隔离', '已驳回', '内容未变', '已退役']

/**
 * 直传 OSS：fetch 无法上报上传进度，故用 XHR 接 upload.onprogress。File 必须留闭包、勿进 Vue 响应式
 * （Proxy 包裹会破坏 xhr.send(file)）。timeout 55min，**必须略小于**后端
 * kb_upload.UPLOAD_TOKEN_TTL（现 60min）——反过来的话浏览器还在传、令牌已过期，
 * 用户白等满全程才拿到一个失败。两个数改一个就要改另一个。
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
    xhr.timeout = 55 * 60 * 1000
    xhr.send(file)
  })
}

/** 后端 detail 的展示净化：剥掉裸 `HTTP <code>`、压平空白、限长。
 *  **保留中文原因与 trace 号** —— trace 正是报障凭据，且只有 `detail:true` 的调用方才走到这里。 */
function safeDetail(msg: string): string {
  return msg.replace(/\bHTTP \d{3}\b/g, '').replace(/\s+/g, ' ').trim().slice(0, 140)
}

/**
 * 把 OSS/CORS/413/5xx 等技术错误转成可操作的人话。
 *
 * ⚠️ **默认不外泄 trace/HTTP 串**：本函数**员工侧的知识贡献页也在用**（useContribute），
 * 不是管理台专用。只有显式传 `{ detail: true }`（管理台，受 _require_kb_console 保护）
 * 才附上后端 detail。这条分级是原「绝不暴露」约束的收紧版，不是放弃它。
 *
 * 2026-08-04 的教训：一次**全员上传中断**（node 归属登记撞生产 owner_dept NOT NULL 漂移
 * → 1048 → 500）被这里的兜底档压成一句「请稍后重试」，排查成本几乎全花在「看不见后端
 * 到底说了什么」上。故新增两档，并且无论哪一档都把原始 status/detail 打进 console：
 *   · 429 —— 此前掉进兜底，长得和真故障一模一样（当晚为此查了一轮限流台账才排除）
 *   · 5xx —— 「服务端的锅」和「你再试试」必须可区分，否则用户会一直连点
 */
export function uploadErrText(e: any, opts?: { detail?: boolean; maxMb?: number }): string {
  const msg = (e && e.message) || String(e || '')
  const status = Number(e && e.status) || 0
  const retryAfter = Number(e && e.retryAfter) || 0
  // 原始信息恒进 devtools（不进 UI）——员工侧也能让管理员远程问一句"控制台报了什么"。
  try { console.warn('[upload] 失败', { status, detail: msg, retryAfter }) } catch { /* 无 console 环境 */ }

  // 数字取**运行时真值**（调用方传 maxUploadMb，来自 /api/kb/config），常量只兜底：
  // 上限从 50 提到 150 那次，若这里仍写死常量，服务端已放行 150 而提示还说「超过 50MB」。
  if (status === 413 || /超过大小上限|too large|413/i.test(msg)) return `文件超过上限 ${opts?.maxMb || MAX_UPLOAD_MB}MB，请压缩或拆分后重传。`
  if (status === 403 || /无权|权限|forbidden/i.test(msg)) return '你没有该操作的权限，请联系知识库管理员。'
  if (status === 429) return `操作太频繁，请等 ${retryAfter > 0 ? retryAfter : 60} 秒后再传（同时开着多个管理台页面也会占用额度）。`
  if (/OSS PUT|CORS|网络错误|超时|timeout/i.test(msg)) return '文件上传通道异常，请稍后重试；若持续失败请联系知识库管理员（可能是 OSS 跨域未放行）。'
  if (/未检测到已上传|请先完成直传|过期/i.test(msg)) return '上传未完成或链接已过期，请重新选择文件上传。'
  if (/空/.test(msg)) return '所选文件为空，请检查后重传。'
  if (/Failed to fetch|NetworkError|Load failed/i.test(msg)) return '网络中断，没能连上服务器；请检查网络后重试。'
  if (status >= 500) {
    return opts?.detail
      ? `服务端处理失败：${safeDetail(msg)}。不是你文件的问题——请把这句话原样发给知识库管理员。`
      : '服务端处理失败（不是你文件的问题），请把出错时间告知知识库管理员。'
  }
  return opts?.detail && msg && !/^HTTP \d+$/.test(msg)
    ? `上传失败：${safeDetail(msg)}；若持续失败请把这句话原样发给知识库管理员。`
    : '上传失败，请稍后重试；若持续失败请联系知识库管理员。'
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
