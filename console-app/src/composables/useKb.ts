import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { apiJson, ApiError } from '@/lib/api'
import { useSession } from '@/stores/session'
import { useDialog } from '@/composables/useDialog'
import { useToast } from '@/composables/useToast'
import type { OrgSnapshot } from '@/composables/useOrgSnapshot'
import { scrollIntoViewSafely } from '@/lib/utils'
import {
  GROUP_LABEL, MAX_UPLOAD_MB, TERMINAL_BADGES, deptLabel, putWithProgress, uploadErrText, buildDupMsg, fileCore, unsupportedNames, type DupDoc,
} from '@/lib/kb'

// 知识库管理台单例 store。身份/可管部门复用 P1 的 session（whoami 已给 managed_owner_depts），
// 不再走旧 console 的 org-tree。所有写接口后端【现查】授权，前端 role 仅作 UI 门禁。

// 失败/结果类提示统一走应用内告知框（useDialog 单例状态挂模块作用域，此处直接取用；不再用原生 alert）。
const { notice } = useDialog()
// 成功类回执走 toast，**绝不复用 notice()**：useDialog 是模块级单槽（_supersede），一个后台请求
// 返回就能把用户正盯着的破坏性确认框顶掉并让它 resolve 成 false（用户以为自己点了取消）。
const { toast, clearToasts } = useToast()

export interface DocItem {
  doc_id: string; title: string; original_filename: string; owner_dept: string
  permission_level: string; current_version_no: number; status: string
  status_badge: string; updated_at: string
  can_manage?: boolean   // 可操作（写作用域）；my-docs 恒 true，browse 全部门时外部门为 false
  cited_count?: number | null   // 利用度：被引用问答数（null/undefined=数据不可用，0=真·从未被引用）
  last_cited_at?: string        // 最近被引用时间（cited_count>0 时有值）
  reject_reason?: string        // 被驳回原因（仅 已驳回 态有值）：台账副行直出，闭环「为什么被驳回」
}
export interface PendingItem {
  doc_id: string; version_no: number; title: string; original_filename: string
  owner_dept: string; permission_level: string; owner_name: string; created_at: string
}
// 授权申请（Phase C）：其他部门申请检索本部门文档；审批人 = 文档所属部门管理员（锁定决策 2026-06-26）。
// 后端 /api/kb/access-requests 尚未上线 → loadAccessRequests 静默兜底空；DEV ?preview 注入 mock 以可视化。
export interface AccessRequestItem {
  id: string; doc_id: string; doc_title: string; owner_dept: string
  requester_dept: string; requester_name: string; permission_level: string; reason: string; created_at: string
}
// 审批方侧：已放行（approved）的跨部门授权存量（后端 /api/kb/access-grants）——供「已授权清单」展示 + 撤销。
export interface AccessGrantItem {
  id: string; doc_id: string; doc_title: string; owner_dept: string
  requester_dept: string; requester_name: string; permission_level: string; reason: string; decided_at: string
}
// 审批历史（只读聚合 /api/kb/approval-history）：四条审批流的【历史决策】合并时间线。
// dept_admin 见 access+contribution（本部门）；kb_admin 见全部四类（全库，含 upload/admin_grant）。
export interface ApprovalHistoryItem {
  kind: string            // 'access' | 'contribution' | 'upload' | 'admin_grant'
  action: string          // approved | rejected | revoked | accepted | granted
  title: string; owner_dept: string; subject: string
  detail: string; extra: string
  decided_by: string; decided_by_name: string; decided_at: string
}
// Phase F 成员/角色管理（kb_admin 专属）：现行管理员 + 各自可管理 owner_dept（后端 /api/kb/admin-grants）。
export interface AdminNodeRoot { dept_id: number; name: string; source: string; active: boolean }
export interface NodeCandidate {
  id: number; user_id: string; user_name: string; dept_id: number; dept_name: string
  risk_reason: string; derived_snapshot_rev: number; created_at: string
}
export interface AdminItem {
  user_id: string; user_name: string; role: string; managed_owner_depts: string[]
  managed_node_roots?: AdminNodeRoot[]
}
// 申请人侧：我的申请 + 派生同步态（已批准·待同步 vs 已放行；后端 /api/kb/my-access-requests）。
export interface MyAccessRequestItem {
  id: string; doc_id: string; doc_title: string; owner_dept: string; requester_dept: string
  status: string                 // pending / approved / rejected
  sync_state: string             // n/a | pending_sync | projected
  reason: string; created_at: string; decided_at: string
  decision_note?: string         // 审批人驳回原因（rejected 时有值）：申请人侧闭环
}
export type AccessState = 'none' | 'pending' | 'approved_pending_sync' | 'projected' | 'rejected'
// pct 必须可空：null = 该行此刻没有可测进度（排队 / 申请上传地址 / 登记 / 已提交 / 失败），
// 与 uploadPct 同一语义。写成非空的 number 并播种 0，会让 UploadCard 的守卫静态恒真——
// 评审实测：3 个文件卡在「申请上传地址」时 3 行全渲染 aria-valuenow=0 的条，批量失败终态后
// 条还冻在 0 不消失。那正是 redline③ 禁的假条，只是从单文件路径漏到了队列路径。
export interface QueueRow { name: string; status: string; pct: number | null; msg: string; dupMsg?: string }
export interface VerCtx { doc_id: string; title: string; owner_dept: string; permission_level: string; current_version_no: number }

interface MyDocsResp { items: DocItem[]; has_more: boolean; badge_counts?: Record<string, number> | null }
export interface KbStats { total: number; active: number; retired: number; chunks: number; new_this_month: number; by_badge: Record<string, number>; owner_depts?: string[] }
// Phase E 概览看板真实数据（镜像 api.py KbInsightsResponse / KbGovernanceResponse，字段一一对应）
export interface KbTopDoc { title: string; owner_dept: string; hits: number }
export interface KbGapQuery { query: string; count: number; avg_top: number }
export interface KbInsights {
  scope: string; window_days: number
  questions: number; askers: number; success: number; refusal: number; cited: number; helped_users: number; effective_rate: number
  top_docs: KbTopDoc[]; gap_queries: KbGapQuery[]
}
export interface KbEmbedRun { bizdate: string; embedded: number; failed: number; fail_rate: number }
export interface KbDeptCoverage { owner_dept: string; owner_label?: string; docs: number; new_month: number; qa_hits: number; no_answer_rate: number; pii_docs: number; wow_net?: number | null; wow_total?: number | null; qa_wow_net?: number | null; qa_wow?: number | null; qa_hits_7d?: number | null }
export interface KbFeedbackDay { day: string; up: number; down: number }
export interface KbDownvoteReason { reason: string; count: number }
export interface KbFileType { ftype: string; count: number }
export interface KbGovernance {
  window_days: number
  // P2-14：监控链路心跳（null=未知；stale=ops_monitor >26h 未跑，看板亮红）
  monitor_heartbeat_age_h?: number | null
  monitor_stale?: boolean
  file_types: KbFileType[]
  docs_active: number; docs_in_index: number; dual_version_docs: number
  avg_latency_ms: number; p50_latency_ms: number; p95_latency_ms: number
  avg_retrieval_ms: number; avg_llm_ms: number; embed_runs: KbEmbedRun[]
  qa_api_success_rate: number; retrieval_api_success_rate: number; errors_24h: number; qa_total_30d: number
  pii_redacted_docs: number; pii_quarantined_docs: number
  answer_total: number; answer_success: number; answer_refusal: number; answer_no_result: number; answer_error: number
  effective_rate: number
  feedback_up: number; feedback_down: number; feedback_total: number; helpful_rate: number
  feedback_last7: number; feedback_daily: KbFeedbackDay[]; downvote_reasons: KbDownvoteReason[]
  dept_coverage: KbDeptCoverage[]
}
export interface KbConfig { max_upload_bytes: number; accepted_exts: string[]; node_acl_grant?: boolean }

// ── 运营数据面（批次γ D1-D3，GET /api/kb/ops-metrics，kb_admin 专属）──
// 三块各自 available：false=该块查询失败（诚实「未知」）；true+空列表=表可读但确实没数据。
export interface KbOpsLlmModelRow { model: string; calls: number; error_calls: number; tokens_prompt: number; tokens_completion: number; avg_latency_ms: number }
export interface KbOpsBucketRow { key: string; calls: number; tokens_total: number }
export interface KbOpsDailyLlmRow { d: string; calls: number; tokens_total: number }
export interface KbOpsSloDayRow {
  d: string; total: number
  answer_rate: number | null; no_result_rate: number | null; error_rate: number | null
  p95_latency_ms: number | null; distinct_users: number
  slo_ok: boolean | null; breaches: string[]; rejected_count: number | null
}
export interface KbOpsAdmissionDayRow { d: string; admitted: number; rejected: number }
export interface KbOpsAdmissionReasonRow { reason: string; count: number }
export interface KbOpsMetrics {
  window_days: number
  llm_available: boolean
  llm_total_calls: number; llm_error_calls: number
  llm_tokens_prompt: number; llm_tokens_completion: number
  llm_cost_estimate: number | null
  llm_p50_latency_ms: number; llm_p95_latency_ms: number
  llm_by_model: KbOpsLlmModelRow[]; llm_by_category: KbOpsBucketRow[]; llm_by_dept: KbOpsBucketRow[]
  llm_daily: KbOpsDailyLlmRow[]
  slo_available: boolean; slo_daily: KbOpsSloDayRow[]; slo_breach_days: number
  admission_available: boolean; admission_daily: KbOpsAdmissionDayRow[]; admission_reasons: KbOpsAdmissionReasonRow[]
}
export interface VersionItem {
  version_no: number; content_process_status: string; chunk_status: string
  index_status: string; publish_status: string; status_badge: string; error_message: string; created_at: string
  has_raw?: boolean; quarantined?: boolean   // 历史版本下载：无原件/已封存 → 按钮置灰
}
interface UploadUrlResp { upload_token: string; put_url: string; raw_key: string; doc_id: string; expires_in: number; requires_kb_admin_approval: boolean; content_type?: string }
interface RegisterResp { doc_id: string; version_no: number; content_process_status: string; requires_kb_admin_approval: boolean; status_badge: string; idempotent: boolean; title: string; content_dups: DupDoc[]; content_dups_other: number }
interface DocStatusResp { status_badge: string; chunk_active: number; error_message: string }
interface RetireResp { status: string; retired: boolean; already: boolean; status_badge: string; note: string }

export type SortKey = 'updated_at' | 'current_version_no' | 'title' | 'owner_dept' | 'status_badge'

// ── 状态 ──
const docs = ref<DocItem[]>([])
const kbStats = ref<KbStats | null>(null)   // 全作用域聚合（真实总数/状态分布，不受 my-docs 50 上限影响）
const kbInsights = ref<KbInsights | null>(null)     // Phase E：使用成效 + 知识缺口（owner 作用域）
const kbGovernance = ref<KbGovernance | null>(null) // Phase E：全库运行健康/治理风险/部门覆盖（kb_admin）
const kbConfig = ref<KbConfig | null>(null) // 后端能力配置（上传上限/类型）；缺省用常量兜底
const kbOpsMetrics = ref<KbOpsMetrics | null>(null) // 批次γ：运营数据面（kb_admin；null=未加载/无权）
const maxUploadBytes = computed(() => kbConfig.value?.max_upload_bytes || MAX_UPLOAD_MB * 1048576)
const maxUploadMb = computed(() => Math.round(maxUploadBytes.value / 1048576))
const verHistory = ref<{ doc: DocItem | null; versions: VersionItem[]; loading: boolean; error: string } | null>(null)
const approvals = ref<PendingItem[]>([])
const accessRequests = ref<AccessRequestItem[]>([])   // 授权申请队列（审批人侧 · pending）
const queuesSettled = ref(false)                      // 队列完成过至少一次拉取——空态确认文案靠它区分「真无待办」与「还在加载」
const accessGrants = ref<AccessGrantItem[]>([])       // 已授权清单（审批人侧 · approved 存量，供撤销）
const approvalHistory = ref<ApprovalHistoryItem[]>([]) // 审批历史（只读聚合，四流合并时间线）
const adminGrants = ref<AdminItem[]>([])              // Phase F 现行管理员名单（kb_admin 专属）
const nodeCandidates = ref<NodeCandidate[]>([])       // 阶段 B：管辖根候选确认队列（kb_admin）
const grantableDepts = ref<string[]>([])             // 授予表单可选 owner_dept（写白名单）
const loadingDocs = ref(false)
const loadingMoreDocs = ref(false)                   // 「加载更多」翻页中（与首屏 loadingDocs 区分）
const hasMoreDocs = ref(false)                        // 服务端还有下一页（消费 my-docs/browse 的 has_more）
const docScope = ref<'managed' | 'all'>('managed')   // 本部门(my-docs) / 全部门只读浏览(browse)
// 授权申请（申请人侧，Phase C）：本会话内已申请的 doc_id（无后端持久化，仅即时反映「审批中」）。
const accessReqDoc = ref<DocItem | null>(null)
const accessReqBusy = ref(false)
const requestedDocIds = ref<Set<string>>(new Set())   // 乐观：本会话刚提交、服务端态尚未回灌前临时显「审批中」
// 申请人侧权威态：doc_id → {status, sync_state}（拉自 /api/kb/my-access-requests；后端未上线则空）
const myAccessReqs = ref<Map<string, { status: string; sync_state: string; note: string }>>(new Map())
const q = ref('')
const filter = ref('')                 // status_badge 精确过滤；'' = 全部
const permFilter = ref('')             // permission_level 精确过滤；'' = 全部
const ownerFilter = ref('')            // owner_dept（含生产子线）精确过滤；'' = 全部
const citedFilter = ref('')            // 利用度筛选：'' 全部 / 'never' 从未被引用（退役候选）/ 'used' 有引用
const sortKey = ref<SortKey>('updated_at')
const sortDir = ref<1 | -1>(-1)
// 多选（批量操作）：选中的 doc_id 集合。仅对可管理(can_manage!==false)行有意义；筛选/换源时自动收敛。
const selectedIds = ref<Set<string>>(new Set())
const bulkBusy = ref(false)
const bulkMsg = ref('')
// 批量真实进度。串行 for 循环里的 i+1/total 本来就是确定值（不是估算），此前只拼进 bulkMsg
// 文本。拆成结构化字段是为了让进度条读到数字——**注意**：方案说这是「替代从字符串解析」，
// 审计核实该前提不成立（全仓没有任何代码解析 bulkMsg，DocTable 只原样显示）。所以这是新增
// 地基，不是替换解析器。bulkTotal=0 表示不在批量中 ⇒ 进度条整个不渲染。
const bulkDone = ref(0)
const bulkTotal = ref(0)

// 上传表单 / 状态
const newTitle = ref('')
const newOwner = ref('')
const newPerm = ref('dept_internal')          // dept_internal / shared（=dept_internal+主动授权）/ public / restricted
const newShareDepts = ref<string[]>([])       // newPerm='shared' 时的共享目标组码（legacy 口径）
// 上传时按组织架构选的节点（node 口径）。⚠️ 与 newShareDepts **二选一**，绝不双写：
// acl_policy 的模式互斥不变量规定 node ⇒ 只投 d:/dx:、绝不含组码（codex BLOCKER-1）。
const newShareNodes = ref<{ dept_id: number; subtree: boolean }[]>([])
// 阶段 B：node 模式的归属节点（单选；数组形状与 OrgTreePicker v-model 一致，恒 ≤1 项）
const newOwnerNode = ref<{ dept_id: number; subtree: boolean }[]>([])
// 节点授权是否已对**读侧**生效（后端 /api/kb/org-tree 回的 RAG_NODE_ACL_GRANT）。
// ⚠️ 关时上传必须继续走 legacy 组码：GRANT 关时 can_read_doc 对 node 文档无条件 DENY，
// 此刻写 node 授权 = 新文档对所有人不可见（连归属部门自己都看不到）。
const nodeAclGrant = ref(false)
// 归属自动预填的「每身份一次」标记（记 userId）：切 tab 重挂不重播（尊重用户清空），
// 身份切换 / __resetKb 后对新身份重新生效。⚠️ 不用裸模块 boolean——那不随 reset 复位
// （codex v3 blocker）。
const autoSeedDoneFor = ref('')
// 主动共享弹窗（owner 侧）：把自己部门的 dept_internal 文档直接放行给指定部门（POST /api/kb/access-grants）。
const shareCtx = ref<DocItem | null>(null)
const docMetaCtx = ref<DocItem | null>(null)   // 阶段 B「编辑信息」弹窗（doc-meta 端点 UI 面）
const shareBusy = ref(false)
// 「谁能看到这篇文档」解释器弹窗（只读）：GET /api/kb/visibility-explain。
const visCtx = ref<DocItem | null>(null)
const visExplain = ref<VisExplain | null>(null)
const visLoading = ref(false)
const visErr = ref('')
// 差评联动复核（看板卡片）：引用了我作用域文档的回答收到 👎。null=尚未加载（显占位不显 0）。
const feedbackReview = ref<FeedbackReviewItem[] | null>(null)
const feedbackReviewTruncated = ref(false)   // B8：后端两层截断任一命中即 true（如实告知，不做分页）
const feedbackReviewViewTruncated = ref(false)   // B8 视图侧（2026-08-04 独立核验：两名核验员各自撞出
                                                 // 同一缺陷——筛选视图丢标志 ⇒ 静默截断在该路径复活 +
                                                 // 收件箱标志误挂到筛选数据上）
// P3-3（2026-08-04）：硬 LIMIT 队列的截断标记。后端 `LIMIT N+1` 取探针行，多出来即置位。
// 键 = 队列名；无键 = 该队列本轮没截断。与 B8 同族：**先让截断不再静默**，不做真分页
// —— 真分页需要稳定排序键（`DATETIME` 只到秒，OFFSET 翻页会漏/重）+ 前端翻页，属设计变更。
const truncatedQueues = reactive<Record<string, boolean>>({})
// ── 反馈区按部门筛选（2026-08-03，codex 两轮共识）─────────────────────────────
// 全库收件箱（chip 专用，永不带 owner_key）与区内筛选视图**分离**——否则筛选会把
// 「差评未处理」置顶 chip 也变成部门计数（行动区语义必须全库）。
const feedbackReviewView = ref<FeedbackReviewItem[] | null>(null)
export interface KbFeedbackStats {
  owner_key: string; window_days: number; scope_degraded: boolean
  answer_total: number; up: number; down: number; total: number; helpful_rate: number
  last7: number; daily: KbFeedbackDay[]; reasons: KbDownvoteReason[]
}
const fbStats = ref<KbFeedbackStats | null>(null)
let fbViewSeq = 0
let fbStatsSeq = 0
const showResolvedFeedback = ref(false)   // 「显示已处理」切换：默认只看未处置（收件箱语义）
const feedbackResolveBusy = ref<Set<string>>(new Set())   // 处置在途（按 message_id）
// 入库复审任务队列（盲区审计 P2-33：review_task 补消费端，kb_admin 专属）。
const reviewTasks = ref<ReviewTaskItem[] | null>(null)
const reviewTasksHasMore = ref(false)   // P2-11：后端 limit=20 此前静默截断，前端无从知道还有
const reviewTasksLoadBusy = ref(false)   // 「加载更多」追加在途（独立核验：双击重复追加）
const showClosedReviewTasks = ref(false)
const reviewTaskResolveBusy = ref<Set<string>>(new Set())
// 共享目标可选项 = 10 个用户面 ACL 组码（与后端 sanitize 白名单同源；生产子线是 owner 粒度、非读者组）。
const SHARE_TARGETS = Object.keys(GROUP_LABEL)
const verCtx = ref<VerCtx | null>(null)
const uploadBusy = ref(false)
const uploadMsg = ref('')
// 上传真实百分比。四个阶段里**只有 OSS PUT 这一段**有真值（XHR upload.onprogress）；
// 「申请上传地址」「登记」两段没有任何可测进度。null = 无真实进度 ⇒ UI 侧整个进度条不渲染。
// 绝不在这两段渲染成 valuenow=0 或三段式假条——那正是 redline③「不做假阶段状态条」禁的东西。
const uploadPct = ref<number | null>(null)
const uploadErr = ref('')
const uploadOk = ref(false)
const dupWarn = ref('')                // 文件名级预查重
const contentDupMsg = ref('')          // ETag 内容级查重
const uploadQueue = ref<QueueRow[]>([])
const selectedNames = ref<string[]>([])
// 在途互斥（按行/按用户 key）：一行审批/撤销不再禁用其他不相关按钮（B5）。
const inflight = ref<Set<string>>(new Set())
const retireBusy = ref(false)

// File 真身【绝不进响应式】（Vue3 Proxy 会破坏 xhr.send(file)）——留模块闭包。
let selectedFiles: File[] = []
const DOCS_PAGE = 50                  // 台账翻页页大小（= 后端 limit 上限）
// 服务端 offset 上界镜像（api.py::_KB_MAX_OFFSET=10000，防深分页扫表）：超界的深页服务端会
// 钳到上界（同一页数据反复返回），前端在此之前就禁掉——loadDocsPage 兜底不发请求，UI 层提示。
const KB_MAX_OFFSET = 10000
const DOCS_MAX_PAGE = Math.floor(KB_MAX_OFFSET / DOCS_PAGE) + 1   // 可达最深页（offset=10000 → 第 201 页）
const docsPage = ref(1)               // 当前页号（1 起；页码翻页器口径，loadDocsPage 落地时更新）
let docsOffset = 0                    // 当前已加载到的 offset（首屏 0；每翻一页 +DOCS_PAGE）
let docsSeq = 0
let trackSeq = 0
let qTimer: ReturnType<typeof setTimeout> | null = null
let trackTimer: ReturnType<typeof setTimeout> | null = null   // 当前轮询定时器句柄（可取消）

// ── 身份切换防线（codex v4 共识）───────────────────────────────────────────
// 上传表单是模块级 refs：A 免登失效→容器重登换成 B（reauth 的 syncHistoryForUser 同款
// 场景）后，A 的归属/可见集/已选文件绝不能被 B 继承误交。服务端 register 有 upload_token
// uid 门兜底（kb_console 403「与当前用户不符」）；这里管的是**前端草稿与 UI 不被污染**。

/** 清空上传草稿（生产可调；__resetKb 复用）。含 File 真身、file input 之外的全部草稿态；
 *  DOM input 的清理在 UploadCard 里 watch principalEpoch 走既有 clearFiles 路径。 */
function resetUploadDraft() {
  newTitle.value = ''; newOwner.value = ''; newOwnerNode.value = []; newPerm.value = 'dept_internal'
  newShareDepts.value = []; newShareNodes.value = []; verCtx.value = null
  selectedFiles = []; selectedNames.value = []; uploadQueue.value = []
  dupWarn.value = ''; contentDupMsg.value = ''; uploadMsg.value = ''; uploadErr.value = ''
  uploadOk.value = false
  autoSeedDoneFor.value = ''
  trackSeq++                                        // 作废进行中的状态轮询
  if (trackTimer) { clearTimeout(trackTimer); trackTimer = null }
}

/** principal 变化时（当前 epoch ≠ 捕获值）为真——上传链各异步边界据此静默终止，
 *  不把旧身份的进度/结果回写新身份 UI。 */
function principalChangedSince(epoch0: number): boolean {
  return useSession().principalEpoch !== epoch0
}

// 每个 session store 实例只注册一次身份 watcher。⚠️ 不能用裸模块 boolean：Vitest 逐 case
// 重建 Pinia，boolean 置位后新 store 不再被监听（codex v4 major）。detached effectScope：
// watcher 不随首个调用组件的卸载被回收。
const _watchedSessions = new WeakSet<object>()
function ensurePrincipalWatch(session: ReturnType<typeof useSession>) {
  if (_watchedSessions.has(session as unknown as object)) return
  _watchedSessions.add(session as unknown as object)
  let lastUid = session.identity?.userId ?? ''
  effectScope(true).run(() => {
    watch(() => session.identity?.userId ?? '', (uid) => {
      // A→B 直换、或 A→登出→B：清 A 的草稿。同用户 401 续签（uid 不变/短暂为空后复原）保留草稿。
      if (uid && lastUid && uid !== lastUid) resetUploadDraft()
      if (uid) lastUid = uid
    })
  })
}

/** 归属自动预填（Sam 拍板：dept_admin 单管辖根 ⇒ 预填并锁定展示、可点「更改」）。
 *  node 口径仅在 snap.status==='fresh' 且恰一根且根在快照中且表单为空且非升版时播种；
 *  legacy 口径 = 唯一上传目标预选。每身份一次（autoSeedDoneFor），不覆盖用户已选/已清。 */
function maybeAutoSeedOwner(snap?: OrgSnapshot) {
  const session = useSession()
  const uid = session.identity?.userId ?? ''
  if (!uid || autoSeedDoneFor.value === uid || verCtx.value) return
  if (session.role !== 'dept_admin') return
  if (nodeAclGrant.value) {
    if (!snap || snap.status !== 'fresh') return          // stale/unavailable：不播种不锁定（B2）
    const roots = snap.my_managed_node_roots
    if (!roots || roots.length !== 1) return              // null=旧后端 unknown；多根不自动（B3）
    const root = roots[0]
    const rootNode = snap.nodes.find((n) => n.dept_id === root)
    if (!rootNode || rootNode.depth > 2) return   // 颗粒度止于二级：深层唯一根不自动锁定（codex blocker）
    if (newOwnerNode.value.length) return
    newOwnerNode.value = [{ dept_id: root, subtree: true }]
    autoSeedDoneFor.value = uid
  } else {
    const t = uploadTargetDeptsOf(session)
    if (t.length !== 1 || newOwner.value) return
    newOwner.value = t[0]
    autoSeedDoneFor.value = uid
  }
}

/** 上传目标选项（模块级取值：与 useKb().uploadTargetDepts computed 同口径）。 */
function uploadTargetDeptsOf(session: ReturnType<typeof useSession>): string[] {
  const u = session.identity?.uploadTargetDepts
  return u && u.length ? u : (session.identity?.managedOwnerDepts ?? [])
}

// 各分区加载错误（key→提示文案）。诚实区分：404（端点未上线，Phase C/D 可选）→ 静默兜底空；
// 5xx/网络/其他 → 置错误条，组件显「加载失败 + 重试」，不再把服务端故障伪装成「无数据」。
// 返回值：true=真错误已置条；false=404 静默（调用方可据此决定是否兜底空数据）。
const loadErrors = ref<Record<string, string>>({})
function noteLoadError(key: string, e: unknown): boolean {
  if (e instanceof ApiError && e.status === 404) { delete loadErrors.value[key]; return false }
  loadErrors.value[key] = '加载失败，请重试'
  return true
}
function clearLoadError(key: string) { delete loadErrors.value[key] }

// ── GET loader 的在途/已定态（2026-08-04）────────────────────────────────────────
// 解决的是一个实测出来的具体故障：`/api/kb/stats` 返回 404 时，noteLoadError 走静默分支
// （delete loadErrors[key] 后直接返回），于是 kbStats 恒为 null、loadErrors 恒为空，
// 任何写成 `!data && !error` 的加载判据**永远为真** —— 实测 4 张 hero 卡的骨架转 5 秒仍在转，
// 0 个 alert、0 个重试。加载态必须由「请求是否在途」独立表达，不能从数据是否存在反推。
//
// ⚠️ 为什么不复用 withInflight：那是**行级互斥**（防重复提交），第二个调用方被直接丢弃、
// 立即拿到 undefined。而且那个 undefined 是**承重的**——grantDeptAdmin(:1730) 写着
// `?? false // 在途（重复点击）→ 视为未提交`，改成合流会让重复点击返回首调的 true，语义翻转；
// setVisibility(:1624) 的返回类型也依赖它。8 个写路径调用点，不动。
//
// 这里要的是**请求合流**：第二个调用方不重复发请求，但**必须等到同一份真结果**。理由不是
// 「否则 tab 级指示提前收掉」（那是后续轮次的事，今天还不成立），而是**丢弃语义根本修不好
// 这个双拉**：ensureTabLoaded 从 dash 与 docs 各调一次 loadStats（实测 2 个并发请求），
// 第二个调用方拿到 undefined 后 _loadedTabs 已把 docs 标记为已加载——此时若首个（dash）请求
// 随后失败，docs tab **永远不会重拉**。合流让第二个调用方拿到真实成败。
const loaderInflight = ref<Set<string>>(new Set())
/** 至少完整尝试过一次（成败不论）。用来把「还没开始/在途」与「跑完了但没数据」分开。 */
const loaderSettled = ref<Set<string>>(new Set())
const loaderPromises = new Map<string, Promise<void>>()   // 模块闭包，不进响应式
const loaderCount = new Map<string, number>()             // 同上；join:false 时同 key 可真并发
const loaderGen = new Map<string, number>()               // 同上；见 withLoader 的世代号

function isLoading(key: string): boolean { return loaderInflight.value.has(key) }
function hasSettled(key: string): boolean { return loaderSettled.value.has(key) }
/** 在途按 key 计数，不是布尔——否则 join:false 下先返回的那次会把仍在途的骨架提前撤掉。 */
function bumpLoading(key: string, d: 1 | -1) {
  const n = (loaderCount.get(key) || 0) + d
  if (n > 0) loaderCount.set(key, n); else loaderCount.delete(key)
  const s = new Set(loaderInflight.value)
  if (n > 0) s.add(key); else s.delete(key)
  loaderInflight.value = s
}

/**
 * 四态记账（在途/已定态）+ **可选**请求合流。
 *
 * ⚠️ `join` 默认 true，但**写后权威重拉必须传 `join:false`**。合流会让重拉 join 到一个
 * 快照早于本次写的在途 GET 上：评审实测连撤两个 dept_admin，第二次撤销后服务端已生效、
 * 界面仍显示该行，且 `revokeAdminGrant` 的 toast 就挂在重拉之后播报「已撤销全部管理权限」
 * （见 :1761 那条注释——它承诺的正是「播报时列表已是最终状态」）。`_loadedTabs` 短路让
 * 切走再切回也不重拉，脏行留到整页刷新。属「改了没生效」家族（console-web-fixes-2026-07-15）。
 * 反之 stats 必须保留合流：dash 与 docs 各调一次，去掉会退回并发双拉（loading-states G5）。
 */
async function withLoader(key: string, fn: () => Promise<void>, opts?: { join?: boolean }): Promise<void> {
  if (opts?.join !== false) {
    const joined = loaderPromises.get(key)
    if (joined) return joined          // 合流：不重复发请求，但调用方等到同一个真结果
  }
  // 世代号：join:false 覆盖过本 key 时，先落地的旧请求不得删掉新请求的登记
  const gen = (loaderGen.get(key) || 0) + 1
  loaderGen.set(key, gen)
  const p = (async () => {
    bumpLoading(key, 1)
    try { await fn() } finally {
      if (loaderGen.get(key) === gen) loaderPromises.delete(key)
      bumpLoading(key, -1)
      loaderSettled.value = new Set(loaderSettled.value).add(key)
    }
  })()
  loaderPromises.set(key, p)           // 后来者若合流，join 到更新的那次（快照更近）
  return p
}

// staleness 门（#82）：App ready 已为侧栏红点预载 approvals/accessRequests，30s 内再进 /manage 不重拉。
// 起跑即记时间戳 → 预载尚在途时挂载视图也不会并发双拉；失败清零重开门（LoadError 重试按钮走非 force
// 也能真重拉）。数据确实变化的写路径（上传产生待审批单等）传 force=true 逃生。
const STALE_MS = 30_000
const lastLoadedAt: Record<string, number> = {}
function freshEnough(key: string): boolean { return Date.now() - (lastLoadedAt[key] || 0) < STALE_MS }

// 在途互斥：按 key（行/用户）加锁，避免一键禁用无关按钮 + 防同行重复提交。组件用 isBusy(key) 绑 :disabled。
function isBusy(key: string): boolean { return inflight.value.has(key) }
async function withInflight<T>(key: string, fn: () => Promise<T>): Promise<T | undefined> {
  if (inflight.value.has(key)) return undefined
  inflight.value = new Set(inflight.value).add(key)
  try { return await fn() } finally { const n = new Set(inflight.value); n.delete(key); inflight.value = n }
}

function sortDocs(list: DocItem[], key: SortKey, dir: 1 | -1): DocItem[] {
  return [...list].sort((a, b) => {
    let r: number
    if (key === 'current_version_no') r = (Number(a[key]) || 0) - (Number(b[key]) || 0)
    else r = String(a[key] ?? '').localeCompare(String(b[key] ?? ''))
    return r * dir
  })
}

const filtered = computed(() =>
  sortDocs(docs.value.filter((d) =>
    (!filter.value || (filter.value === ANOMALY_FILTER ? BAD_BADGES.includes(d.status_badge) : d.status_badge === filter.value))
    && (!permFilter.value || d.permission_level === permFilter.value)
    && (!ownerFilter.value || d.owner_dept === ownerFilter.value)
    // 利用度：never=真·从未被引用（cited_count===0，退役候选）；used=有引用。
    // 数据不可用（null/undefined，RAG_QA_FACT_JOIN 未开）两个档都不入——0 与「不知道」必须可区分。
    && (!citedFilter.value || (citedFilter.value === 'never' ? d.cited_count === 0 : (d.cited_count ?? 0) > 0))
  ), sortKey.value, sortDir.value))

// 归属筛选选项 = 已加载文档里出现过的 owner_dept（含生产子线，如 production_mold）→ 覆盖"按子部门管理"。
const ownerOptions = computed(() => Array.from(new Set(docs.value.map((d) => d.owner_dept).filter(Boolean))).sort())

function countOf(badge: string): number {
  return badge ? docs.value.filter((d) => d.status_badge === badge).length : docs.value.length
}

// 异常文档徽章（退役候选/需人工处理）：待办摘要条 + 台账速览共用口径。
const BAD_BADGES = ['未入索引', '处理失败', '已隔离', '已驳回']
// 「异常」伪徽章筛选：一次圈出全部 BAD_BADGES(服务端 my-docs 有同名 IN 分支)。
// 待办条「异常文档」chip 点击即设——原先只滚动到台账,还得自己再挑一个坏徽章点。
const ANOMALY_FILTER = '异常'

// ── #7 全库口径：状态 chip 计数 / 归属下拉 / 异常文档数取自 /api/kb/stats（不受 50 页上限影响）。
// 仅在「本部门」台账（docScope='managed'，其 owner 作用域与 stats 同源）启用全库口径；
// 「全部门」浏览（browse，跨部门）无对应全库聚合 → 回退已加载页派生（诚实，不伪造跨部门总数）。
const fullScopeCounts = computed(() => docScope.value === 'managed' && !!kbStats.value)
// faceted 计数（2026-07-16 Sam 反馈）：my-docs/browse 响应带与当前筛选（归属/范围/利用度/搜索，
// 除状态自身）同口径的按徽章计数——chips 与标题总数跟随下拉筛选。三级口径：faceted > stats 全库 > 页派生。
const serverBadgeCounts = ref<Record<string, number> | null>(null)
const _sumCounts = (keys: string[]) => keys.reduce((s, k) => s + ((serverBadgeCounts.value || {})[k] || 0), 0)
// 状态 chip 列表（含「全部」）：faceted/全库口径取计数键；否则已加载页派生。
const ledgerBadgeChips = computed<string[]>(() => {
  const base = serverBadgeCounts.value
    ? Object.keys(serverBadgeCounts.value).filter((k) => k && serverBadgeCounts.value![k] > 0)
    : fullScopeCounts.value
      ? Object.keys(kbStats.value!.by_badge || {}).filter((k) => (kbStats.value!.by_badge || {})[k] > 0)
      : Array.from(new Set(docs.value.map((d) => d.status_badge).filter(Boolean)))
  // 坏徽章 ≥2 种才值得一枚聚合 chip（只有一种时与单徽章 chip 重复）
  return ['', ...base, ...(anomalyCount.value > 0 && base.filter((b) => BAD_BADGES.includes(b)).length > 1 ? [ANOMALY_FILTER] : [])]
})
// 状态 chip 计数：faceted 口径优先（跟随筛选）；退 stats 全库；再退已加载页计数（countOf）。
function ledgerBadgeCount(badge: string): number {
  if (badge === ANOMALY_FILTER) return anomalyCount.value
  if (serverBadgeCounts.value) {
    return badge ? (serverBadgeCounts.value[badge] || 0) : _sumCounts(Object.keys(serverBadgeCounts.value))
  }
  if (fullScopeCounts.value) {
    const bb = kbStats.value!.by_badge || {}
    return badge ? (bb[badge] || 0) : (kbStats.value!.total || 0)
  }
  return countOf(badge)
}
// 台账标题总数（当前全部筛选含状态 chip 生效后的真实总数）：faceted 口径才可得；
// 无 faceted（旧后端/计数失败）→ null，模板回退 docs.length（老行为：已加载行数）。
const ledgerTotal = computed<number | null>(() => {
  const c = serverBadgeCounts.value
  if (!c) return null
  if (filter.value === ANOMALY_FILTER) return _sumCounts(BAD_BADGES)
  if (filter.value) return c[filter.value] || 0
  return _sumCounts(Object.keys(c))
})
// 归属下拉选项：全库口径下取 stats.owner_depts；否则已加载页派生。
const ledgerOwnerOptions = computed<string[]>(() =>
  (fullScopeCounts.value && kbStats.value!.owner_depts?.length) ? kbStats.value!.owner_depts! : ownerOptions.value)
// 异常文档数（待办摘要条）：faceted 口径优先（跟随筛选）；退 stats 全库；再退已加载页计数。
const anomalyCount = computed<number>(() => {
  if (serverBadgeCounts.value) return _sumCounts(BAD_BADGES)
  if (fullScopeCounts.value) {
    const bb = kbStats.value!.by_badge || {}
    return BAD_BADGES.reduce((s, b) => s + (bb[b] || 0), 0)
  }
  return docs.value.filter((d) => BAD_BADGES.includes(d.status_badge)).length
})

// ── 多选 / 批量 ──────────────────────────────────────────────────
// 当前可见【且可管理】的行（批量只作用于这些；外部门只读行不可批量操作）。
const selectableVisible = computed(() => filtered.value.filter((d) => d.can_manage !== false))
// 选中集与可见集的交集（筛选后自动收敛：切筛选/换源，隐藏行虽仍在 set 里但不参与操作/计数）。
const selectedDocs = computed(() => selectableVisible.value.filter((d) => selectedIds.value.has(d.doc_id)))
const selectedCount = computed(() => selectedDocs.value.length)
const allVisibleSelected = computed(() =>
  selectableVisible.value.length > 0 && selectedDocs.value.length === selectableVisible.value.length)
function isSelected(id: string): boolean { return selectedIds.value.has(id) }
function toggleSelect(id: string) {
  const n = new Set(selectedIds.value)
  if (n.has(id)) n.delete(id); else n.add(id)
  selectedIds.value = n
}
function toggleSelectAllVisible() {
  // 已全选可见 → 清空；否则把当前可见可管理行全选（不动隐藏行的既有选中）。
  const n = new Set(selectedIds.value)
  const vis = selectableVisible.value.map((d) => d.doc_id)
  if (allVisibleSelected.value) vis.forEach((id) => n.delete(id))
  else vis.forEach((id) => n.add(id))
  selectedIds.value = n
}
function clearSelection() { selectedIds.value = new Set(); bulkMsg.value = '' }

// 批量执行器：顺序跑（aux 限流 30/分，串行最稳）+ 进度回填 bulkMsg；失败隔离，末尾一次 loadDocs。
async function _bulkRun(docsToRun: DocItem[], label: string,
                        one: (d: DocItem) => Promise<string | null>): Promise<void> {
  if (bulkBusy.value || !docsToRun.length) return
  bulkBusy.value = true
  bulkDone.value = 0; bulkTotal.value = docsToRun.length
  let ok = 0; const fails: string[] = []
  for (let i = 0; i < docsToRun.length; i++) {
    bulkMsg.value = `${label}中… ${i + 1}/${docsToRun.length}`
    const err = await one(docsToRun[i])
    if (err) fails.push(`${docsToRun[i].title || docsToRun[i].doc_id}：${err}`); else ok++
    bulkDone.value = i + 1                       // 该篇已结（成败不论）——进度是「跑完几篇」不是「成功几篇」
  }
  bulkBusy.value = false
  bulkTotal.value = 0                            // 收条：结果留在 bulkMsg 文本里，进度条撤掉
  bulkMsg.value = `${label}完成：成功 ${ok}${fails.length ? `，失败 ${fails.length}` : ''}`
  // 明细最多列 8 条防弹窗爆屏；超出部分显式声明数量——绝不让「列出的」被误读成「全部失败原因」。
  if (fails.length) {
    const more = fails.length > 8 ? `\n……另有 ${fails.length - 8} 篇失败未列出（共 ${fails.length} 篇，逐篇原因见台账状态）` : ''
    void notice({ title: `${label}部分失败`, message: fails.slice(0, 8).join('\n') + more, danger: true })
  }
  if (ok) { clearSelectionKeepMsg(); void loadDocs() }   // 成功过 → 清选中 + 权威重拉
}
function clearSelectionKeepMsg() { selectedIds.value = new Set() }

/** 批量退役（复用单篇 retire 端点，逐篇顺序）。调用方已二次确认。 */
async function bulkRetire(): Promise<void> {
  // P2-12：与 DocTable.onBulkRetire 的计数谓词**逐字一致**——公开文档需 kb_admin（后端 403），
  // 非 kb_admin 一律跳过，绝不发注定失败的请求（「说几篇=做几篇」）。
  const _kbAdmin = useSession().role === 'kb_admin'   // 模块级函数：isKbAdmin 在 useKb() 作用域内
  const targets = selectedDocs.value.filter(
    (d) => d.status_badge !== '已退役' && !(!_kbAdmin && d.permission_level === 'public'))
  await _bulkRun(targets, '退役', async (d) => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { d.status_badge = '已退役'; return null }
      await apiJson('/api/kb/retire', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id }) })
      return null
    } catch (e: any) { return e && e.status === 403 ? (e.detail || '无权退役') : uploadErrText(e) }
  })
}

/** 批量改可见范围（复用 set-visibility；public 涉全公司需 kb_admin，调用方按角色限制选项）。 */
async function bulkSetVisibility(level: string): Promise<void> {
  const targets = selectedDocs.value.filter((d) => d.permission_level !== level)
  // reload:false——逐篇重拉会 N+1 次全量列表；_bulkRun 批末统一一次权威 loadDocs
  await _bulkRun(targets, '改可见范围', (d) => setVisibility(d, level, '批量调整', { reload: false }))
}

function sortBy(key: SortKey) {
  if (sortKey.value === key) sortDir.value = (sortDir.value === 1 ? -1 : 1)
  else { sortKey.value = key; sortDir.value = key === 'updated_at' ? -1 : 1 }
}

function patchRow(docId: string, badge: string) {
  const d = docs.value.find((x) => x.doc_id === docId)
  if (d) d.status_badge = badge
}

// 台账列表 URL：scope 分流（全部门 browse / 本部门 my-docs）+ 分页（limit/offset）+ 文档名搜索
// + 结构化筛选（归属/可见范围/徽章/利用度，服务端执行 → 覆盖全库而非只筛已加载页，#7）。
function docsUrl(offset: number): string {
  const params = new URLSearchParams()
  if (docScope.value === 'all') params.set('scope', 'all')
  params.set('limit', String(DOCS_PAGE))
  params.set('offset', String(offset))
  if (q.value) params.set('q', q.value)
  if (ownerFilter.value) params.set('owner_dept', ownerFilter.value)
  if (permFilter.value) params.set('perm', permFilter.value)
  if (filter.value) params.set('badge', filter.value)
  if (citedFilter.value) params.set('cited', citedFilter.value)
  const path = docScope.value === 'all' ? '/api/kb/browse' : '/api/kb/my-docs'
  return `${path}?${params.toString()}`
}

// 结构化筛选变更 → 服务端重载（reset offset）。防抖复用 qTimer 语义但独立 timer，避免与搜索互相取消。
let filterTimer: ReturnType<typeof setTimeout> | null = null
function applyLedgerFilter() {
  if (filterTimer) clearTimeout(filterTimer)
  filterTimer = setTimeout(() => void loadDocs(), 150)
}
// 台账筛选 setter（供 DocTable 绑定）：设值 + 触发服务端重载。单测直接赋 .value（不走 setter）→
// 保持纯客户端 filtered/countOf 语义不变（spec 锁定），实际 UI 走 setter → 全库服务端筛选。
function setBadgeFilter(v: string) { filter.value = v; applyLedgerFilter() }
function setPermFilter(v: string) { permFilter.value = v; applyLedgerFilter() }
function setOwnerFilter(v: string) { ownerFilter.value = v; applyLedgerFilter() }
function setCitedFilter(v: string) { citedFilter.value = v; applyLedgerFilter() }
function clearLedgerFilters() {
  filter.value = ''; permFilter.value = ''; ownerFilter.value = ''; citedFilter.value = ''
  applyLedgerFilter()
}

// 按页号拉取并【替换】docs（设计稿页码翻页器语义；loadDocs = loadDocsPage(1)，筛选/搜索/scope
// 变化沿用既有 loadDocs 重置路径 → 自动回第 1 页）。复用 docsUrl 构造 + docsSeq 竞态守卫。
async function loadDocsPage(page: number) {
  const target = Math.max(1, Math.floor(page) || 1)
  const offset = (target - 1) * DOCS_PAGE
  if (offset > KB_MAX_OFFSET) return       // 深页兜底：超出服务端 offset 上界不发请求（UI 层已提示）
  const seq = ++docsSeq
  loadingDocs.value = true
  clearLoadError('docs')
  try {
    // DEV ?preview：注入 mock（含外部门 can_manage=false 行）以可视化全部门只读浏览；prod 死代码消除。
    // mock 不足一页 → 恒第 1 页（翻页器单页自隐，误调 loadDocsPage(n>1) 也不崩）。
    if (import.meta.env.DEV && useSession().token === 'dev-preview') {
      const mine: DocItem[] = [
        { doc_id: 'm1', title: '营销物料使用规范 v3', original_filename: 'guideline.pdf', owner_dept: 'marketing', permission_level: 'dept_internal', current_version_no: 3, status: 'active', status_badge: '已上线', updated_at: '2026-06-26 10:00', can_manage: true, cited_count: 12, last_cited_at: '2026-07-02 09:12' },
        { doc_id: 'm2', title: '品牌 VI 手册', original_filename: 'vi.pdf', owner_dept: 'marketing', permission_level: 'public', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '2026-06-20 09:00', can_manage: true, cited_count: 0 },
        { doc_id: 'm3', title: '618 活动复盘', original_filename: '618.docx', owner_dept: 'marketing', permission_level: 'dept_internal', current_version_no: 2, status: 'active', status_badge: '处理中', updated_at: '2026-06-25 14:00', can_manage: true },
      ]
      const foreign: DocItem[] = [
        { doc_id: 'h1', title: '员工考勤管理制度', original_filename: 'attendance.pdf', owner_dept: 'hr', permission_level: 'dept_internal', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '2026-06-24 11:00', can_manage: false },
        { doc_id: 'f1', title: '差旅报销标准', original_filename: 'travel.xlsx', owner_dept: 'finance', permission_level: 'public', current_version_no: 2, status: 'active', status_badge: '已上线', updated_at: '2026-06-22 16:00', can_manage: false },
        { doc_id: 'p1', title: '注塑车间作业指导书', original_filename: 'sop.docx', owner_dept: 'production', permission_level: 'dept_internal', current_version_no: 5, status: 'active', status_badge: '已上线', updated_at: '2026-06-19 08:00', can_manage: false },
      ]
      docs.value = docScope.value === 'all' ? [...mine, ...foreign] : mine
      hasMoreDocs.value = false
      serverBadgeCounts.value = null   // 预览 mock 无 faceted 计数 → 页派生口径
      docsOffset = 0; docsPage.value = 1
      return
    }
    // 作用域分流：全部门走只读 browse（排除 restricted、带 can_manage），本部门走 my-docs。
    const r = await apiJson<MyDocsResp>(docsUrl(offset), { auth: true })
    if (seq !== docsSeq) return            // 竞态守卫：仅最新结果落地
    docs.value = r.items || []             // 替换（不是追加）：页码翻页语义
    hasMoreDocs.value = !!r.has_more       // 服务端探测到下一页
    serverBadgeCounts.value = r.badge_counts || null   // faceted 计数（旧后端无此键 → 回退口径）
    docsOffset = offset
    docsPage.value = target                // 成功落地才更新页号（失败保留旧页旧表）
  } catch (e) { if (seq === docsSeq) noteLoadError('docs', e) /* 保留旧表 */ } finally { if (seq === docsSeq) loadingDocs.value = false }
}

async function loadDocs() { await loadDocsPage(1) }

// 台账总条数（翻页器口径）：faceted ledgerTotal（跟随当前筛选的真实总数）优先；无 faceted
// （旧后端/预览 mock）→ 已知下界 = 当前页起点 + 本页行数（还有下一页则至少再一页——「下一页」
// 可点、页码随翻页增长的诚实降级，不伪造总数）。
const docsTotal = computed<number>(() =>
  ledgerTotal.value ?? ((docsPage.value - 1) * DOCS_PAGE + docs.value.length + (hasMoreDocs.value ? DOCS_PAGE : 0)))

// 加载下一页并【追加】到当前列表（不自增 docsSeq：追加属于当前列表；期间若 loadDocs/换 scope/搜索
// 触发，docsSeq 变化 → 本页结果作废丢弃，避免错插到新列表）。
async function loadMoreDocs() {
  if (loadingMoreDocs.value || !hasMoreDocs.value) return
  const seq = docsSeq
  loadingMoreDocs.value = true
  try {
    const nextOffset = docsOffset + DOCS_PAGE
    const r = await apiJson<MyDocsResp>(docsUrl(nextOffset), { auth: true })
    if (seq !== docsSeq) return            // 期间列表已被重置 → 丢弃本页
    docs.value = [...docs.value, ...(r.items || [])]
    docsOffset = nextOffset
    hasMoreDocs.value = !!r.has_more
  } catch { /* 保留现有列表，hasMore 不变可重试 */ } finally { loadingMoreDocs.value = false }
}

// 切换台账作用域（本部门 ↔ 全部门只读）。切换即清状态筛选（两个集合徽章分布不同）并重载。
function setScope(s: 'managed' | 'all') {
  if (docScope.value === s) return
  docScope.value = s
  filter.value = ''; permFilter.value = ''; ownerFilter.value = ''; citedFilter.value = ''
  selectedIds.value = new Set(); bulkMsg.value = ''   // 换源清空选中（旧 doc_id 不再可见）
  void loadDocs()
  if (s === 'all') void loadMyAccessRequests()   // 全部门浏览：回灌我的申请态以渲染 申请授权/审批中/同步中/已放行
}

// ── 授权申请（申请人侧）：对其他部门文档发起检索授权申请 ──
function openAccessRequest(d: DocItem) { accessReqDoc.value = d }
function closeAccessRequest() { accessReqDoc.value = null }
function accessStateOf(docId: string): AccessState {
  const r = myAccessReqs.value.get(docId)
  if (r?.status === 'approved') return r.sync_state === 'projected' ? 'projected' : 'approved_pending_sync'
  if (r?.status === 'pending') return 'pending'
  // 已驳回 → 显式反馈态（原先折叠回 none，申请人对"被驳回"这件事和原因都无感）；
  // 刚重新申请（乐观标记在）→ 审批中。
  if (r?.status === 'rejected') return requestedDocIds.value.has(docId) ? 'pending' : 'rejected'
  // 服务端无 row → 看本会话乐观标记（刚提交、态未回灌前）；否则未申请
  return requestedDocIds.value.has(docId) ? 'pending' : 'none'
}
// 被驳回原因（rejected 行的 decision_note）：pill 悬停提示用
function accessNoteOf(docId: string): string { return myAccessReqs.value.get(docId)?.note || '' }
// 申请人侧权威态：拉我的申请 + 派生同步态。后端未上线 / 无申请 → 静默空（不报错、不打扰）。
async function loadMyAccessRequests() {
  try {
    const r = await apiJson<{ items: MyAccessRequestItem[]; truncated?: boolean }>('/api/kb/my-access-requests', { auth: true })
    truncatedQueues.myAccessRequests = !!r.truncated
    const m = new Map<string, { status: string; sync_state: string; note: string }>()
    // 后端按 created_at DESC（最新在前）返回；每 doc 保留【最新】一行——拒后重申/撤销后重申会留多行，
    // 若 last-write-wins（直接 m.set）会让最旧行覆盖最新 → 误显「申请授权」。首见即最新 → 不覆盖。
    for (const it of (r.items || [])) if (!m.has(it.doc_id)) m.set(it.doc_id, { status: it.status, sync_state: it.sync_state, note: it.decision_note || '' })
    myAccessReqs.value = m
    // 权威已有该 doc 的行 → 清掉本会话乐观标记（否则被驳回/撤销的文档因乐观回退仍显「审批中」、抑制「申请授权」按钮）。
    if (requestedDocIds.value.size) {
      const next = new Set(requestedDocIds.value)
      let changed = false
      for (const id of next) if (m.has(id)) { next.delete(id); changed = true }
      if (changed) requestedDocIds.value = next
    }
  } catch { /* 兜底空 */ }
}
async function submitAccessRequest(reason: string) {
  const d = accessReqDoc.value
  if (!d || accessReqBusy.value) return
  accessReqBusy.value = true
  try {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') {   // 预览演示：本地标记审批中
      requestedDocIds.value = new Set(requestedDocIds.value).add(d.doc_id)
      accessReqDoc.value = null
      return
    }
    await apiJson('/api/kb/access-requests', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id, owner_dept: d.owner_dept, reason }) })
    requestedDocIds.value = new Set(requestedDocIds.value).add(d.doc_id)
    accessReqDoc.value = null
    void loadMyAccessRequests()   // 提交后回灌权威态（pending）；乐观标记保证即时反馈
  } catch (e: any) {
    // 后端（Phase C）未上线 → 404：诚实告知，不伪造「已提交」。
    if (e && e.status === 404) void notice({ message: '授权申请功能即将上线，敬请期待。' })
    else void notice({ title: '提交失败', message: uploadErrText(e), danger: true })
  } finally { accessReqBusy.value = false }
}

async function loadStats() { return withLoader('stats', _loadStats) }
async function _loadStats() {
  // 概览真实口径（总数/状态分布/已索引分块）；失败则前端兜底用已加载文档计数（docs.length / countOf）。
  const s = useSession()
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    kbStats.value = s.role === 'kb_admin'
      ? { total: 1618, active: 1475, retired: 143, chunks: 27659, new_this_month: 1249, by_badge: { 已上线: 1475, 处理中: 8, 排队中: 4, 已退役: 143 } }
      : { total: 42, active: 40, retired: 2, chunks: 612, new_this_month: 6, by_badge: { 已上线: 38, 待审核: 3, 处理中: 1 } }
    return
  }
  clearLoadError('stats')
  try { kbStats.value = await apiJson<KbStats>('/api/kb/stats', { auth: true }) } catch (e) { noteLoadError('stats', e) /* 兜底 */ }
}

async function loadConfig() {
  // 上传上限/类型走后端权威，避免硬编码漂移（失败则用 MAX_UPLOAD_MB 常量兜底）。
  try { kbConfig.value = await apiJson<KbConfig>('/api/kb/config', { auth: true }) } catch { /* 兜底 */ }
  // 节点授权读侧姿态随 kbConfig 一起回（同一个请求，零额外往返）。
  // ⚠️ 别为这个布尔值单独打 /api/kb/org-tree：那个端点要过 _require_kb_console（一次 RDS
  // 往返，实测 ~1s），而 kb/config 是公开能力端点、实测 2ms。
  nodeAclGrant.value = !!kbConfig.value?.node_acl_grant
}

// ── Phase E：概览看板真实数据（缺数据/端点未上线 → 静默兜底 null，由组件如实显空/加载中）──
// DEV ?preview 注入 mock（取自真实口径量级，便于设计走查）；prod build 死代码消除。
async function loadInsights() { return withLoader('insights', _loadInsights) }
async function _loadInsights() {
  const s = useSession()
  if (!s.identity?.canManage) { kbInsights.value = null; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    kbInsights.value = {
      scope: s.role === 'kb_admin' ? 'global' : 'dept', window_days: 30,
      questions: 186, askers: 40, success: 143, refusal: 43, cited: 130, helped_users: 37, effective_rate: 0.769,
      top_docs: [
        { title: 'FL-GJMY-WI-008《下达销售订单》作业指导书.docx', owner_dept: 'marketing', hits: 64 },
        { title: '亚马逊运营SOP（标准化流程）.docx', owner_dept: 'marketing', hits: 51 },
        { title: '客户投诉处理 SOP.pdf', owner_dept: 'marketing', hits: 33 },
      ],
      gap_queries: [
        { query: '2ozpp杯在龙盛机上的速度', count: 2, avg_top: 0.729 },
        { query: '由此写一封英文信', count: 1, avg_top: 0.617 },
      ],
    }
    return
  }
  clearLoadError('insights')
  try { kbInsights.value = await apiJson<KbInsights>('/api/kb/insights', { auth: true }) } catch (e) { noteLoadError('insights', e) /* 兜底 */ }
}

async function loadGovernance() { return withLoader('governance', _loadGovernance) }
async function _loadGovernance() {
  const s = useSession()
  if (s.role !== 'kb_admin') { kbGovernance.value = null; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    kbGovernance.value = {
      window_days: 30, docs_active: 1618, docs_in_index: 1475, dual_version_docs: 0,
      file_types: [
        { ftype: 'PDF', count: 628 }, { ftype: 'DOCX', count: 607 }, { ftype: 'XLSX', count: 313 },
        { ftype: 'PPTX', count: 5 }, { ftype: '图片', count: 6 },
      ],
      qa_api_success_rate: 0.974, retrieval_api_success_rate: 0.974, errors_24h: 0, qa_total_30d: 951,
      avg_latency_ms: 14035, p50_latency_ms: 8106, p95_latency_ms: 54994, avg_retrieval_ms: 1538, avg_llm_ms: 12428,
      embed_runs: [
        { bizdate: '2026-06-23', embedded: 117, failed: 0, fail_rate: 0 },
        { bizdate: '2026-06-22', embedded: 96, failed: 0, fail_rate: 0 },
        { bizdate: '2026-06-21', embedded: 228, failed: 0, fail_rate: 0 },
      ],
      pii_redacted_docs: 475, pii_quarantined_docs: 3,
      answer_total: 902, answer_success: 790, answer_refusal: 112, answer_no_result: 15, answer_error: 25,
      effective_rate: 0.876,
      feedback_up: 64, feedback_down: 44, feedback_total: 108, helpful_rate: 0.593,
      feedback_last7: 5,
      feedback_daily: [
        { day: '2026-06-15', up: 4, down: 4 }, { day: '2026-06-16', up: 9, down: 0 },
        { day: '2026-06-17', up: 1, down: 7 }, { day: '2026-06-18', up: 3, down: 21 },
        { day: '2026-06-20', up: 1, down: 0 }, { day: '2026-06-22', up: 0, down: 2 },
        { day: '2026-06-24', up: 0, down: 1 }, { day: '2026-06-26', up: 1, down: 1 },
      ],
      downvote_reasons: [
        { reason: '其他', count: 14 }, { reason: '不准确', count: 12 }, { reason: '不相关', count: 8 },
        { reason: '不完整', count: 8 }, { reason: '已过时', count: 2 }, { reason: '未注明', count: 2 },
      ],
      dept_coverage: [
        { owner_dept: 'production', docs: 800, new_month: 711, qa_hits: 303, no_answer_rate: 0.221, pii_docs: 247, wow_net: 30, wow_total: 0.04, qa_wow_net: -10, qa_wow: -0.032, qa_hits_7d: 61 },
        { owner_dept: 'hr', docs: 192, new_month: 0, qa_hits: 372, no_answer_rate: 0.124, pii_docs: 71, wow_net: 0, wow_total: 0.0, qa_wow_net: 15, qa_wow: 0.042, qa_hits_7d: 93 },
        { owner_dept: 'it', docs: 36, new_month: 0, qa_hits: 384, no_answer_rate: 0.102, pii_docs: 8, wow_net: -2, wow_total: -0.053, qa_wow_net: 22, qa_wow: 0.061, qa_hits_7d: 97 },
        { owner_dept: 'marketing', docs: 178, new_month: 178, qa_hits: 186, no_answer_rate: 0.231, pii_docs: 29, wow_net: 19, wow_total: 0.12, qa_wow_net: 8, qa_wow: 0.045, qa_hits_7d: 45 },
        { owner_dept: 'rd', docs: 175, new_month: 175, qa_hits: 24, no_answer_rate: 0.0, pii_docs: 64, wow_net: 9, wow_total: 0.054, qa_wow_net: 3, qa_wow: 0.143, qa_hits_7d: 7 },
      ],
    }
    return
  }
  clearLoadError('governance')
  try { kbGovernance.value = await apiJson<KbGovernance>('/api/kb/governance', { auth: true }) } catch (e) { noteLoadError('governance', e) /* 兜底 */ }
}

// 批次γ：运营数据面（governance 同款单门模式——kb_admin 短路 + loadErrors，无 supported 探测：
// 端点无 flag 语义，「三块各自 available」是业务降级不是功能开关）。
// （分支侧另有 P0-D 身份指纹判废——随大合并收敛；main 无运行期身份切换，指纹恒等。）
async function loadOpsMetrics() { return withLoader('opsMetrics', _loadOpsMetrics) }
async function _loadOpsMetrics() {
  const s = useSession()
  if (s.role !== 'kb_admin') { kbOpsMetrics.value = null; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    // 日期相对 Date.now() 生成（批次β 拆雷同款教训）：硬编码日历日会腐坏——过几天后
    // 预览里的「rollup 停摆哨兵」会因数据"变旧"而误亮，演示失真。
    const _day = (off: number) => new Date(Date.now() + off * 86400000).toISOString().slice(0, 10)
    kbOpsMetrics.value = {
      window_days: 30,
      llm_available: true,
      llm_total_calls: 2841, llm_error_calls: 31,
      llm_tokens_prompt: 3_412_000, llm_tokens_completion: 861_000,
      llm_cost_estimate: null,   // 价表未配的诚实空态也要在预览里出现（不能全是开心路径）
      llm_p50_latency_ms: 1180, llm_p95_latency_ms: 6400,
      llm_by_model: [
        { model: 'qwen3.7-plus', calls: 2210, error_calls: 24, tokens_prompt: 2_900_000, tokens_completion: 720_000, avg_latency_ms: 1350 },
        { model: 'qwen3-vl-plus', calls: 431, error_calls: 7, tokens_prompt: 396_000, tokens_completion: 98_000, avg_latency_ms: 2100 },
        { model: 'qwen3-rerank', calls: 200, error_calls: 0, tokens_prompt: 116_000, tokens_completion: 43_000, avg_latency_ms: 310 },
      ],
      llm_by_category: [
        { key: 'default', calls: 2300, tokens_total: 3_400_000 },
        { key: 'deep', calls: 341, tokens_total: 700_000 },
        { key: '未标注', calls: 200, tokens_total: 173_000 },
      ],
      llm_by_dept: [
        { key: 'marketing', calls: 1030, tokens_total: 1_500_000 },
        { key: 'production', calls: 890, tokens_total: 1_320_000 },
        { key: 'hr', calls: 520, tokens_total: 780_000 },
        { key: '未归集', calls: 401, tokens_total: 673_000 },
      ],
      llm_daily: [
        { d: _day(-6), calls: 350, tokens_total: 520_000 }, { d: _day(-5), calls: 410, tokens_total: 610_000 },
        { d: _day(-4), calls: 380, tokens_total: 560_000 }, { d: _day(-3), calls: 460, tokens_total: 690_000 },
        { d: _day(-2), calls: 520, tokens_total: 760_000 }, { d: _day(-1), calls: 310, tokens_total: 450_000 },
        { d: _day(0), calls: 411, tokens_total: 683_000 },
      ],
      slo_available: true,
      slo_daily: [
        { d: _day(-6), total: 98, answer_rate: 0.91, no_result_rate: 0.05, error_rate: 0.01, p95_latency_ms: 51_000, distinct_users: 37, slo_ok: true, breaches: [], rejected_count: null },
        { d: _day(-5), total: 112, answer_rate: 0.89, no_result_rate: 0.07, error_rate: 0.02, p95_latency_ms: 55_000, distinct_users: 41, slo_ok: true, breaches: [], rejected_count: 0 },
        { d: _day(-4), total: 121, answer_rate: 0.78, no_result_rate: 0.14, error_rate: 0.06, p95_latency_ms: 68_000, distinct_users: 44, slo_ok: false, breaches: ['answer_rate_min', 'error_rate_max'], rejected_count: 12 },
        { d: _day(-3), total: 105, answer_rate: 0.9, no_result_rate: 0.06, error_rate: 0.01, p95_latency_ms: 52_000, distinct_users: 38, slo_ok: true, breaches: [], rejected_count: 0 },
        { d: _day(-2), total: 118, answer_rate: 0.92, no_result_rate: 0.05, error_rate: 0.01, p95_latency_ms: 49_000, distinct_users: 45, slo_ok: true, breaches: [], rejected_count: 3 },
        { d: _day(0), total: 96, answer_rate: 0.9, no_result_rate: 0.07, error_rate: 0.02, p95_latency_ms: 53_000, distinct_users: 35, slo_ok: true, breaches: [], rejected_count: 0 },
      ],   // 缺 _day(-1)：演示比率图「缺日断线不臆造 0」的诚实语义
      slo_breach_days: 1,
      admission_available: true,
      admission_daily: [
        { d: _day(-2), admitted: 118, rejected: 3 }, { d: _day(-1), admitted: 84, rejected: 0 },
        { d: _day(0), admitted: 96, rejected: 0 },
      ],
      admission_reasons: [{ reason: 'per_min', count: 2 }, { reason: 'thinking_quota', count: 1 }],
    }
    return
  }
  clearLoadError('opsMetrics')
  try {
    kbOpsMetrics.value = await apiJson<KbOpsMetrics>('/api/kb/ops-metrics', { auth: true })
  } catch (e) { noteLoadError('opsMetrics', e) /* 兜底 */ }
}

// 版本历史（点击文档行「历史」）：拉 /api/kb/version-history（后端现成）。
async function openHistory(d: DocItem) {
  verHistory.value = { doc: d, versions: [], loading: true, error: '' }
  try {
    const r = await apiJson<{ versions: VersionItem[] }>(`/api/kb/version-history?doc_id=${encodeURIComponent(d.doc_id)}`, { auth: true })
    verHistory.value = { doc: d, versions: r.versions || [], loading: false, error: '' }
  } catch { verHistory.value = { doc: d, versions: [], loading: false, error: '版本历史加载失败' } }
}
function closeHistory() { verHistory.value = null }

// 预览原件（审批盲批的解药 + 台账速览）：拉短时签名 GET URL 并在新标签打开原始上传文件。
// 同步先开占位标签保住用户手势（防弹窗拦截），拿到 URL 再定向；不可用则关闭占位并提示。
async function openDocPreview(docId: string, version = 0): Promise<void> {
  const s = useSession()
  const w = typeof window !== 'undefined' ? window.open('', '_blank') : null
  // 批次9 S4：占位标签立即断 opener——签名 URL 指向的文档域（OSS，内容可由上传者影响）
  // 否则持有本页 window.opener，可反向 tabnabbing 重定向控制台页。兜底分支已是 noopener。
  if (w) w.opener = null
  if (import.meta.env.DEV && s.token === 'dev-preview') { w?.close(); void notice({ title: '预览原件', message: '演示环境无真实文件。' }); return }
  try {
    const qs = version ? `&version=${version}` : ''
    const r = await apiJson<{ url: string; available: boolean; filename: string; blocked?: string }>(
      `/api/kb/doc-preview?doc_id=${encodeURIComponent(docId)}${qs}`, { auth: true })
    if (r.available && r.url) { if (w) w.location.href = r.url; else window.open(r.url, '_blank', 'noopener') }
    else if (r.blocked === 'quarantined') {
      // 与「文件缺失」区分：封存是政策拒绝（PII/敏感原件不外发），不是故障。
      w?.close(); void notice({ title: '原件已安全封存', message: '该版本含敏感内容（PII），原件不外发；如需处理请走脱敏重灌流程。', danger: true })
    }
    else { w?.close(); void notice({ title: '原件暂不可预览', message: '文件缺失或对象存储未配置。', danger: true }) }
  } catch (e: any) {
    w?.close()
    void notice({ title: '预览失败', message: e && e.status === 403 ? '无权预览该文档' : uploadErrText(e), danger: true })
  }
}

function setQuery(v: string) {
  q.value = v
  if (qTimer) clearTimeout(qTimer)
  qTimer = setTimeout(() => void loadDocs(), 300)   // 防抖；搜索走服务端（可命中未加载文档）
}

async function loadApprovals(force = false) {
  const s = useSession()
  if (s.role !== 'kb_admin') { approvals.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    approvals.value = [
      { doc_id: 'P1', version_no: 2, title: '2026 客户验厂应答模板', original_filename: '验厂应答.docx', owner_dept: 'quality', permission_level: 'public', owner_name: '李娜', created_at: '2026-06-27' },
      { doc_id: 'P2', version_no: 1, title: '外销报价单（公开版）', original_filename: '报价单.xlsx', owner_dept: 'marketing', permission_level: 'public', owner_name: '王伟', created_at: '2026-06-26' },
    ]
    queuesSettled.value = true
    return
  }
  if (!force && freshEnough('approvals')) { queuesSettled.value = true; return }   // App ready 刚预载过 → 跳过（#82）
  lastLoadedAt['approvals'] = Date.now()
  clearLoadError('approvals')
  try {
    const r = await apiJson<{ items: PendingItem[]; truncated?: boolean }>('/api/kb/pending-approvals', { auth: true })
    approvals.value = r.items || []
    truncatedQueues.approvals = !!r.truncated
  } catch (e) { lastLoadedAt['approvals'] = 0; approvals.value = []; noteLoadError('approvals', e) }
  // kb_admin 的待办队列源=上传审批（授权申请已划归 dept_admin），拉取过即视为队列就绪
  finally { queuesSettled.value = true }
}

// ── 升版态 ──
// 台账「升版」按钮在页面下方,而 UploadCard 在「上传入库」区(台账上方)——不滚动过去,
// 模式切换发生在屏幕外,点击处零反馈=「点了没反应」(与 77e5f97 筛选钳底同族视口问题)。
function scrollToUploadCard() {
  if (typeof document === 'undefined') return
  void nextTick(() => scrollIntoViewSafely(document.getElementById('kb-sec-upload'), 'start'))
}

function enterVersionMode(d: DocItem) {
  verCtx.value = { doc_id: d.doc_id, title: d.title, owner_dept: d.owner_dept, permission_level: d.permission_level, current_version_no: d.current_version_no }
  newTitle.value = ''; dupWarn.value = ''; contentDupMsg.value = ''; uploadErr.value = ''; uploadMsg.value = ''
  selectedFiles = []; selectedNames.value = []
  scrollToUploadCard()
}
function exitVersionMode() { verCtx.value = null }

/**
 * 升版深链落地（小程序「上传新版本」→ ?doc_id=&owner=&title=）：命中已加载文档则正常进升版态；
 * 列表外（>50 / 旧文档）则用 doc_id+owner+title 合成 verCtx，permission_level 留空交后端强制继承
 * （action=version 时后端忽略客户端 perm）。补回 parity-1/3 丢失的能力。
 */
function applyPendingVersion(p: { docId: string; owner: string; title: string }) {
  const doc = docs.value.find((d) => d.doc_id === p.docId)
  if (doc) { enterVersionMode(doc); return }
  verCtx.value = { doc_id: p.docId, title: p.title || p.docId, owner_dept: p.owner, permission_level: '', current_version_no: 0 }
  newTitle.value = ''; dupWarn.value = ''; contentDupMsg.value = ''; uploadErr.value = ''; uploadMsg.value = ''
  selectedFiles = []; selectedNames.value = []
  scrollToUploadCard()
}

// ── 选文件：预检 + 文件名级查重 ──
async function onFileSelected(list: FileList | null) {
  uploadErr.value = ''; uploadMsg.value = ''; contentDupMsg.value = ''; uploadQueue.value = []
  selectedFiles = list ? Array.from(list) : []
  if (verCtx.value) selectedFiles = selectedFiles.slice(0, 1)   // 升版仅 1 文件
  // 客户端扩展名预检（拖拽绕过 input accept）：剔除不支持的文件并提示，省一次「传完才被后端拒」的往返。
  const bad = unsupportedNames(selectedFiles)
  if (bad.length) {
    selectedFiles = selectedFiles.filter((f) => !bad.includes(f.name))
    uploadErr.value = `已忽略 ${bad.length} 个不支持的文件（${bad.join('、')}）。仅支持 PDF / DOCX / XLSX / PPTX / JPG / PNG。`
  }
  selectedNames.value = selectedFiles.map((f) => f.name)
  dupWarn.value = ''
  if (!verCtx.value && selectedFiles.length === 1) {
    const core = fileCore(selectedFiles[0].name)
    if (core.length >= 2) {
      try {
        const epoch0 = useSession().principalEpoch
        const r = await apiJson<MyDocsResp>(`/api/kb/my-docs?limit=10&q=${encodeURIComponent(core)}`, { auth: true })
        if (principalChangedSince(epoch0)) return   // 旧身份的查重结果不写新身份 UI
        const hit = (r.items || []).find((d) => d.status_badge !== '已退役')
        if (hit) dupWarn.value = `已有相似文档《${hit.title || hit.original_filename || hit.doc_id}》v${hit.current_version_no}（${hit.status_badge}）。如是同一文档，建议改为「升版」。`
      } catch { /* 软提示，失败忽略 */ }
    }
  }
}

function trackStatus(docId: string, versionNo: number) {
  const mySeq = ++trackSeq
  const epoch0 = useSession().principalEpoch       // 身份切换即作废（不往新身份 UI 回写轮询结果）
  if (trackTimer) clearTimeout(trackTimer)
  let tries = 0
  let fails = 0                                     // 连续轮询失败计：区分「处理慢」与「状态检查接口出错」
  const MAX = 22
  const poll = async () => {
    if (mySeq !== trackSeq || principalChangedSince(epoch0)) return   // 被新上传/操作/换身份作废
    tries++
    try {
      const s = await apiJson<DocStatusResp>(`/api/kb/doc-status?doc_id=${encodeURIComponent(docId)}&version=${versionNo}`, { auth: true })
      if (mySeq !== trackSeq || principalChangedSince(epoch0)) return   // await 期间被作废
      fails = 0                                     // 成功 → 清失败计
      patchRow(docId, s.status_badge)
      if (TERMINAL_BADGES.includes(s.status_badge)) {
        if (s.status_badge === '处理失败') { uploadOk.value = false; uploadErr.value = `入库失败：${s.error_message || ''}（${docId} v${versionNo}）`; uploadMsg.value = '' }
        else if (s.status_badge === '已上线') uploadMsg.value = `已上线（${s.chunk_active} 段）`
        void loadDocs()
        return
      }
    } catch {
      // 轮询本身失败：累计，连续多次才提示（偶发抖动不打扰），仍重试到上限。
      if (++fails >= 3) uploadMsg.value = '状态检查暂时失败，仍在重试…若持续请手动刷新「我的文档」'
    }
    if (tries >= MAX) { uploadMsg.value = '仍在处理…耗时较长，稍后刷新「我的文档」查看'; return }
    trackTimer = setTimeout(poll, 8000)
  }
  trackTimer = setTimeout(poll, 4000)              // 首查延 4s，给 scanner 认领时间
}

async function uploadSingle(file: File) {
  uploadErr.value = ''; uploadMsg.value = ''; uploadOk.value = false; contentDupMsg.value = ''
  if (file.size <= 0) { uploadErr.value = '所选文件为空。'; return }
  if (file.size > maxUploadBytes.value) { uploadErr.value = `文件 ${(file.size / 1048576).toFixed(1)}MB，超过上限 ${maxUploadMb.value}MB，请压缩或拆分。`; return }
  trackSeq++                                        // 作废上一轮轮询
  // 身份代际：链上任一 await 期间换人 ⇒ 静默终止，不回写 UI（服务端 register uid 门兜底真拦截）
  const epoch0 = useSession().principalEpoch
  uploadBusy.value = true
  try {
    const isVer = !!verCtx.value
    // 「指定部门」模式：登记仍是 dept_internal（可见度基线），随后经主动共享端点放行所选部门。
    const shared = !isVer && newPerm.value === 'shared'
    // 阶段 B node 模式：归属=组织节点，可见集随 upload-url 走、register 原子落授权
    // （不再是「先登记、再补授权」两请求两事务——失败会留半截 node 文档）。
    const nodeMode = !isVer && nodeAclGrant.value && newOwnerNode.value.length > 0
    const body = isVer
      ? { action: 'version', doc_id: verCtx.value!.doc_id, owner_dept: verCtx.value!.owner_dept, permission_level: verCtx.value!.permission_level, filename: file.name, title: newTitle.value || undefined }
      : nodeMode
        ? { action: 'new', filename: file.name, owner_dept: '', permission_level: shared ? 'dept_internal' : newPerm.value, title: newTitle.value || undefined,
            owner_dept_id: newOwnerNode.value[0].dept_id, visible_nodes: [...newShareNodes.value] }
        : { action: 'new', filename: file.name, owner_dept: newOwner.value, permission_level: shared ? 'dept_internal' : newPerm.value, title: newTitle.value || undefined }
    uploadMsg.value = '申请上传地址…'
    const u = await apiJson<UploadUrlResp>('/api/kb/upload-url', { method: 'POST', auth: true, body: JSON.stringify(body) })
    if (principalChangedSince(epoch0)) return
    uploadMsg.value = '上传文件到 OSS… 0%'
    uploadPct.value = 0                             // ← 进入唯一有真实进度的阶段
    await putWithProgress(u.put_url, file, (pct) => {
      if (!principalChangedSince(epoch0)) { uploadMsg.value = `上传文件到 OSS… ${pct}%`; uploadPct.value = pct }
    }, u.content_type)
    uploadPct.value = null                          // ← 离开该阶段，「登记…」没有真实进度可报
    if (principalChangedSince(epoch0)) return       // 换人后不再 register（token uid 也必被后端 403）
    uploadMsg.value = '登记…'
    const r = await apiJson<RegisterResp>('/api/kb/register', { method: 'POST', auth: true, body: JSON.stringify({ upload_token: u.upload_token }) })
    if (principalChangedSince(epoch0)) return
    uploadOk.value = true
    let shareNote = ''
    if (nodeMode && newShareNodes.value.length) {
      // node 模式授权已随 register 原子落库，这里只是提示语
      shareNote = `；可见范围已按组织架构设定（${newShareNodes.value.length} 个节点）`
    } else if (shared && !nodeMode) {
      // legacy 共享失败不判上传失败：文档已在库，提示可去台账「共享」补做。
      try {
        if (newShareDepts.value.length) {
          shareNote = shareResultNote(await createGrants(r.doc_id, [...newShareDepts.value]))
        }
      } catch { shareNote = '；共享设置失败，可稍后在台账点「共享」重试' }
      if (principalChangedSince(epoch0)) return
    }
    uploadMsg.value = `已提交：${r.title || file.name} v${r.version_no}（${r.status_badge}${r.requires_kb_admin_approval ? '，待审批' : ''}）${shareNote}`
    contentDupMsg.value = buildDupMsg(r.content_dups, r.content_dups_other)
    newTitle.value = ''; dupWarn.value = ''; selectedFiles = []; selectedNames.value = []
    if (isVer) exitVersionMode()
    void loadDocs(); void loadApprovals(true)   // force：刚上传可能新增待审批单，穿透 staleness 门
    if (!r.requires_kb_admin_approval) trackStatus(r.doc_id, r.version_no)   // 待审批不轮询
  } catch (e: any) {
    if (principalChangedSince(epoch0)) return       // 旧身份的失败不写新身份 UI
    uploadErr.value = uploadErrText(e); uploadMsg.value = ''
  } finally { uploadBusy.value = false; uploadPct.value = null }   // 任何退出路径都撤条，含 PUT 中途失败
}

// 批量上传并发度（E#35）。跨文件小并发池：每文件内部 upload-url → OSS PUT → register 三步顺序不变，
// 文件之间并发。register 侧已按 raw_key 幂等 + 行锁防撞号（批量恒为 action=new、各文件独立 doc_id/
// raw_key），无跨文件顺序依赖。取 2 而非 3：upload-url/register 都计入后端 aux 限流（默认 30/分，
// 每文件吃 2 次），并发越高小文件批越易撞 429——撞上也只影响该行（失败隔离），但保守起见压低突发。
const BATCH_CONCURRENCY = 2

async function uploadBatch(files: File[]) {
  uploadErr.value = ''; uploadOk.value = false; contentDupMsg.value = ''; uploadMsg.value = ''
  trackSeq++
  const epoch0 = useSession().principalEpoch        // 身份代际（与 uploadSingle 同款防线）
  const rows: QueueRow[] = files.map((f) => ({ name: f.name, status: '排队', pct: null, msg: '' }))
  uploadQueue.value = rows
  // 捕获 ref 里的 proxy 数组一次。**不能**在 worker 里每次经 uploadQueue.value 取：
  // onFileSelected()（「清空已选文件」/file input/dropzone 三个入口在批量期间都可点、
  // 都没有 :disabled="uploadBusy"）会把 uploadQueue.value 重新指向 []，于是 [i] 取到
  // undefined ⇒ row.status 抛 TypeError ⇒ 被 catch 接住 ⇒ catch 里的 row.pct = null
  // **二次抛出且无人接** ⇒ Promise.all reject ⇒ uploadBusy 永不复位 = 上传卡永久锁死，
  // 且本批已 register 成功的文档因 loadDocs() 不执行而不进台账，界面还不报错。
  // 捕获后写的仍是 proxy 元素（反应式不退化），只是免疫重指向：队列被清空后那些写入
  // 不再被模板观察，变成无害的空转。
  const qrows = uploadQueue.value
  uploadBusy.value = true
  let okN = 0, badN = 0
  // 单文件全流程（三步顺序不变）；异常只落到本行——与原串行版一致的失败隔离，其余文件照常。
  const shared = newPerm.value === 'shared'   // 「指定部门」模式对批量同样生效（每文件登记后放行同一组目标）
  const uploadOne = async (i: number) => {
    // ⚠️ 取 qrows[i]（上面捕获的 proxy 数组），**不是**建行时的裸数组 `rows[i]`，也**不是**
    // 每次经 `uploadQueue.value[i]`（会被清空重指向打成 undefined，见上方注释）。
    // 用裸数组的后果：ref 里存的是 Vue 包出来的 proxy，模板读的也是它；改裸对象不过 set trap
    // ⇒ 不发通知、不重渲染。评审实测 onprogress 确实在跑、row.pct 确实写进去了，但队列进度条
    // 与既有的「N%」文本在整个上传期间都不出现；强制一次无关的响应式变更后才一起冒出来。
    // `row.status = '上传中'` 看起来正常只是搭了 `uploadQueue.value = rows` 那次首屏渲染的
    // 便车——PUT 期间的更新没有这个便车。
    const f = files[i], row = qrows[i]
    if (f.size <= 0 || f.size > maxUploadBytes.value) { row.status = '跳过'; row.msg = f.size <= 0 ? '空文件' : `超过 ${maxUploadMb.value}MB`; badN++; return }
    try {
      row.status = '上传中'
      const bNodeMode = nodeAclGrant.value && newOwnerNode.value.length > 0
      const u = await apiJson<UploadUrlResp>('/api/kb/upload-url', { method: 'POST', auth: true, body: JSON.stringify(bNodeMode
        ? { action: 'new', filename: f.name, owner_dept: '', permission_level: shared ? 'dept_internal' : newPerm.value, owner_dept_id: newOwnerNode.value[0].dept_id, visible_nodes: [...newShareNodes.value] }
        : { action: 'new', filename: f.name, owner_dept: newOwner.value, permission_level: shared ? 'dept_internal' : newPerm.value }) })
      if (principalChangedSince(epoch0)) return
      await putWithProgress(u.put_url, f, (pct) => {
        if (!principalChangedSince(epoch0)) { row.pct = pct; row.msg = `${pct}%` }
      }, u.content_type)
      row.pct = null                                // 离开唯一有真实进度的阶段（同单文件路径纪律）
      if (principalChangedSince(epoch0)) return     // 换人后不再 register（token uid 后端必 403）
      row.status = '登记中'; row.msg = ''
      const r = await apiJson<RegisterResp>('/api/kb/register', { method: 'POST', auth: true, body: JSON.stringify({ upload_token: u.upload_token }) })
      if (principalChangedSince(epoch0)) return
      row.status = '已提交'; row.msg = `v${r.version_no}（${r.status_badge}）`
      if (shared && newShareDepts.value.length) {
        // refresh:false：并发 worker 逐文件刷清单会互相覆盖+烧 aux 限流，批末统一拉一次
        try { row.msg += shareResultNote(await createGrants(r.doc_id, [...newShareDepts.value], '', { refresh: false })).replace(/^，/, ' · ') }
        catch { if (!principalChangedSince(epoch0)) row.msg += ' · 共享失败可稍后重试' }
      }
      const dm = buildDupMsg(r.content_dups, r.content_dups_other); if (dm) row.dupMsg = dm
      okN++
    } catch (e: any) {
      row.pct = null                                // PUT 中途失败也要撤条，否则冻在原值不消失
      if (principalChangedSince(epoch0)) return     // 旧身份的失败不写新身份 UI
      row.status = '失败'; row.msg = uploadErrText(e); badN++
    }
  }
  // 小并发池：worker 共享游标领任务，按队列顺序开工（uploadOne 自吞异常，Promise.all 不会中途 reject）。
  let cursor = 0
  const worker = async () => {
    for (;;) {
      if (principalChangedSince(epoch0)) return     // 换身份：整批停止领新任务
      const i = cursor++; if (i >= files.length) return; await uploadOne(i)
    }
  }
  await Promise.all(Array.from({ length: Math.min(BATCH_CONCURRENCY, files.length) }, () => worker()))
  uploadBusy.value = false
  if (principalChangedSince(epoch0)) return         // 批末汇总不写新身份 UI
  uploadMsg.value = `${okN} 成功${badN ? `，${badN} 失败/跳过` : ''}`
  void loadDocs(); void loadApprovals(true)   // force：批量里可能有待审批单
  if (shared && okN) void loadAccessGrants({ afterWrite: true })  // 批末一次权威刷新（逐文件已抑制）
}

function doUpload() {
  if (uploadBusy.value) return
  if (!selectedFiles.length) { uploadErr.value = '请先选择文件。'; return }
  if (verCtx.value || selectedFiles.length === 1) void uploadSingle(selectedFiles[0])
  else void uploadBatch(selectedFiles)
}

// 审批后定向更新（#82）：成功即本地移除该单（reviewCount 红点由 approvals.length 派生，随之同步），
// 免一次全量审批队列重拉；文档列表真受影响（放行/驳回改变该文档徽章）→ 保留一次权威 loadDocs。
// 失败路径保持现状：notice 告知框提示、该单留在队列可重试。
function removeApproval(d: PendingItem) {
  approvals.value = approvals.value.filter((x) => !(x.doc_id === d.doc_id && x.version_no === d.version_no))
}

async function approve(d: PendingItem) {
  await withInflight(`appr:${d.doc_id}/${d.version_no}`, async () => {
    try {
      await apiJson('/api/kb/approve', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id, version_no: d.version_no }) })
      removeApproval(d)
      await loadDocs()
    } catch (e: any) { void notice({ title: '通过失败', message: uploadErrText(e), danger: true }) }
  })
}

async function reject(d: PendingItem, reason: string) {
  await withInflight(`appr:${d.doc_id}/${d.version_no}`, async () => {
    try {
      await apiJson('/api/kb/reject', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id, version_no: d.version_no, reason }) })
      removeApproval(d)
      await loadDocs()
    } catch (e: any) { void notice({ title: '驳回失败', message: uploadErrText(e), danger: true }) }
  })
}

// ── 授权申请（Phase C，审批人侧）──
// 数据源 /api/kb/access-requests 尚未上线 → 静默兜底空（不报错、不打扰）。DEV ?preview 注入 mock 可视化。
async function loadAccessRequests(force = false) {
  const s = useSession()
  if (!s.identity?.canManage) { accessRequests.value = []; return }
  // 拍板：授权申请审批 = 部门管理员之间的事（kb_admin 只管入库）→ kb_admin 不拉不显不计数。
  // 后端仍向 kb_admin 放行（某部门暂无管理员时的救急兜底通道），只是 console 不再呈现为其待办。
  if (s.role === 'kb_admin') { accessRequests.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    accessRequests.value = [
      { id: 'ar1', doc_id: 'D1', doc_title: '营销物料使用规范 v3', owner_dept: 'marketing', requester_dept: 'production', requester_name: '王伟', permission_level: 'dept_internal', reason: '生产部包装设计需引用营销规范，确保对外物料一致。', created_at: '2026-06-26' },
      { id: 'ar2', doc_id: 'D2', doc_title: '客户投诉处理 SOP', owner_dept: 'marketing', requester_dept: 'quality', requester_name: '李娜', permission_level: 'dept_internal', reason: '品质部需对照投诉闭环流程。', created_at: '2026-06-25' },
    ]
    queuesSettled.value = true
    return
  }
  if (!force && freshEnough('accessRequests')) { queuesSettled.value = true; return }   // App ready 刚预载过 → 跳过（#82）
  lastLoadedAt['accessRequests'] = Date.now()
  clearLoadError('accessRequests')
  try {
    const r = await apiJson<{ items: AccessRequestItem[]; truncated?: boolean }>('/api/kb/access-requests', { auth: true })
    accessRequests.value = r.items || []
    truncatedQueues.accessRequests = !!r.truncated
  } catch (e) { lastLoadedAt['accessRequests'] = 0; accessRequests.value = []; noteLoadError('accessRequests', e) }   // 404（未上线）静默；5xx 显错
  finally { queuesSettled.value = true }
}

async function approveAccess(d: AccessRequestItem) {
  await withInflight(`acc:${d.id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { accessRequests.value = accessRequests.value.filter((x) => x.id !== d.id); return }
      await apiJson('/api/kb/access-requests/approve', { method: 'POST', auth: true, body: JSON.stringify({ id: d.id }) })
      // 定向更新（#82）：本地移除该单（与 preview 分支同构）；新授权落到「已授权清单」→ 只刷真正受影响的列表。
      accessRequests.value = accessRequests.value.filter((x) => x.id !== d.id)
      void loadAccessGrants({ afterWrite: true })
    } catch (e: any) { void notice({ title: '授权失败', message: uploadErrText(e), danger: true }) }
  })
}

async function rejectAccess(d: AccessRequestItem, reason: string) {
  await withInflight(`acc:${d.id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { accessRequests.value = accessRequests.value.filter((x) => x.id !== d.id); return }
      await apiJson('/api/kb/access-requests/reject', { method: 'POST', auth: true, body: JSON.stringify({ id: d.id, reason }) })
      accessRequests.value = accessRequests.value.filter((x) => x.id !== d.id)   // 定向更新（#82）：驳回只影响本队列
    } catch (e: any) { void notice({ title: '驳回失败', message: uploadErrText(e), danger: true }) }
  })
}

// 已授权清单（审批人侧 · approved 存量）：后端 /api/kb/access-grants 未上线 → 静默兜底空；DEV ?preview 注入 mock。
// grantsSeq 竞态守卫（同 docsSeq）：批量上传的并发 worker 各自触发刷新时，仅最新一次落地，防旧响应覆盖新清单。
let grantsSeq = 0
/** `afterWrite`：写后权威重拉，禁止合流（理由见 withLoader 注释）。读路径不传，照常合流。 */
async function loadAccessGrants(o?: { afterWrite?: boolean }) { return withLoader('accessGrants', _loadAccessGrants, { join: !o?.afterWrite }) }
async function _loadAccessGrants() {
  const seq = ++grantsSeq
  const s = useSession()
  if (!s.identity?.canManage) { accessGrants.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    accessGrants.value = [
      { id: 'ag1', doc_id: 'D1', doc_title: '营销物料使用规范 v3', owner_dept: 'marketing', requester_dept: 'production', requester_name: '王伟', permission_level: 'dept_internal', reason: '生产部包装设计需引用营销规范。', decided_at: '2026-06-26' },
      { id: 'ag2', doc_id: 'D2', doc_title: '客户投诉处理 SOP', owner_dept: 'marketing', requester_dept: 'quality', requester_name: '李娜', permission_level: 'dept_internal', reason: '品质部对照投诉闭环流程。', decided_at: '2026-06-25' },
    ]
    return
  }
  clearLoadError('accessGrants')
  try {
    const r = await apiJson<{ items: AccessGrantItem[]; truncated?: boolean }>('/api/kb/access-grants', { auth: true })
    if (seq !== grantsSeq) return          // 竞态守卫：仅最新结果落地
    accessGrants.value = r.items || []
    truncatedQueues.accessGrants = !!r.truncated
  } catch (e) { if (seq === grantsSeq) { accessGrants.value = []; noteLoadError('accessGrants', e) } }   // 404（未上线）静默；5xx 显错
}

// 撤销【已批准】的跨部门授权（approved→revoked）：后端同事务收窄 allowed_depts 投影 + 标脏，stage-3 收回放行。
async function revokeAccess(g: AccessGrantItem, reason: string) {
  await withInflight(`grant:${g.id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { accessGrants.value = accessGrants.value.filter((x) => x.id !== g.id); return }
      await apiJson('/api/kb/access-requests/revoke', { method: 'POST', auth: true, body: JSON.stringify({ id: g.id, reason }) })
      await loadAccessGrants({ afterWrite: true })
    } catch (e: any) { void notice({ title: '撤销失败', message: uploadErrText(e), danger: true }) }
  })
}

// ── 主动共享（owner 侧，多部门可见度）───────────────────────────────────────
// 被动申请流（submit→approve）的主动式对偶：文档所属部门管理员直接放行指定部门，
// 后端直插 approved 行并复用 Phase D 投影；撤销/清单/审批历史与被动流同一套。
interface GrantCreateResp { doc_id: string; granted: string[]; skipped: string[]; ok: boolean }

// 差评联动复核（与后端 KbFeedbackReviewItem 对齐；question/comment 已服务端 PII 脱敏）。
export interface FeedbackDocRef { doc_id: string; title: string; owner_dept: string }
export interface FeedbackReviewItem {
  message_id: string; question: string; created_at: string
  reasons: string[]; comment: string          // 点踩原因（中文标签列表）+ 用户补充说明（脱敏）
  handled: boolean; handled_status: string     // 是否已处置 + 原始态（RESOLVED/DISMISSED/PENDING/…）
  docs: FeedbackDocRef[]
}
export type FeedbackResolveAction = 'resolve' | 'dismiss' | 'reopen'

/** 拉差评复核队列（看板卡片）：404（端点未上线）→ 如实兜底空；真错误（5xx/网络）→ 保留
 *  null/旧值 + loadErrors 显错可重试——绝不把服务端故障伪装成「无差评」（staging 2026-07-11：
 *  接口 500 时快乐空态 +「待你处理」chip 双双消失，管理员无从知晓差评存在或功能已坏）。
 *  include_resolved 由「显示已处理」切换驱动；默认只收未处置（收件箱语义）。 */
async function loadFeedbackReview() {
  const s = useSession()
  if (!s.identity?.canManage) { feedbackReview.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    const all: FeedbackReviewItem[] = [
      { message_id: 'fb1', question: '2oz 纸杯的收缩率标准是多少？', created_at: '2026-07-03 14:20',
        reasons: ['不准确', '已过时'], comment: '参数表里写的是旧机型的数据，龙盛机对不上。', handled: false, handled_status: 'PENDING',
        docs: [{ doc_id: 'D1', title: '纸杯工艺参数表', owner_dept: 'production' }] },
      { message_id: 'fb2', question: '出口美国的 FDA 检测周期？', created_at: '2026-07-02 09:11',
        reasons: ['不完整'], comment: '', handled: false, handled_status: '',
        docs: [{ doc_id: 'D2', title: '出口检测流程 SOP', owner_dept: 'marketing' }, { doc_id: 'D3', title: '认证台账', owner_dept: 'marketing' }] },
      { message_id: 'fb3', question: '请假流程走哪个系统？', created_at: '2026-06-30 10:05',
        reasons: ['不相关'], comment: '答的是报销流程。', handled: true, handled_status: 'RESOLVED',
        docs: [{ doc_id: 'D4', title: '考勤请假制度', owner_dept: 'hr' }] },
    ]
    feedbackReview.value = showResolvedFeedback.value ? all : all.filter((x) => !x.handled)
    feedbackReviewTruncated.value = false
    return
  }
  clearLoadError('feedbackReview')
  try {
    const qs = showResolvedFeedback.value ? '?include_resolved=true' : ''
    const r = await apiJson<{ items: FeedbackReviewItem[]; truncated_messages?: boolean; truncated_scan?: boolean }>(
      `/api/kb/feedback-review${qs}`, { auth: true })
    feedbackReview.value = r.items || []
    // B8（Sam 2026-08-04 选 c）：两层截断任一命中即如实告知。**刻意不给「加载更多」** ——
    // 后端 SQL 的 offset 作用在原始 join 行上、与去重聚合后的条目不对齐，加了会漏/重消息。
    feedbackReviewTruncated.value = !!(r.truncated_messages || r.truncated_scan)
  } catch (e) {
    // 仅 404 兜底空；真错误保留 null（首载）/旧值（重载），组件与 chip 据此显式降级而非伪装成空。
    if (!noteLoadError('feedbackReview', e)) feedbackReview.value = feedbackReview.value ?? []
  }
}

/** 区内筛选视图 loader（kb_admin 看板专用；DeptDashboard 恒不调）。无 key ⇒ 镜像全库
 *  收件箱（零额外请求）；有 key ⇒ 带 owner_key 拉取，seq 复核防乱序覆盖。 */
async function loadFeedbackReviewView(ownerKey: string) {
  const mySeq = ++fbViewSeq
  if (!ownerKey) {
    await loadFeedbackReview()
    if (mySeq === fbViewSeq) {
      feedbackReviewView.value = feedbackReview.value
      feedbackReviewViewTruncated.value = feedbackReviewTruncated.value   // 镜像行也镜像标志
    }
    return
  }
  clearLoadError('feedbackReviewView')
  feedbackReviewView.value = null            // 进加载态——绝不把上一 scope 的行挂在新筛选标签下（ux-review D1/D3）
  try {
    const qs = `?owner_key=${encodeURIComponent(ownerKey)}${showResolvedFeedback.value ? '&include_resolved=true' : ''}`
    const r = await apiJson<{ items: FeedbackReviewItem[]; truncated_messages?: boolean; truncated_scan?: boolean }>(
      `/api/kb/feedback-review${qs}`, { auth: true })
    if (mySeq !== fbViewSeq) return
    feedbackReviewView.value = r.items || []
    // 标志随本次筛选数据走（不读收件箱那次 fetch 的标志）——静默截断/误报横幅双向修
    feedbackReviewViewTruncated.value = !!(r.truncated_messages || r.truncated_scan)
  } catch (e) {
    if (mySeq !== fbViewSeq) return
    feedbackReviewViewTruncated.value = false
    // 404=端点未上线兜底空；真错误保留 null ⇒ 组件显式错误占位——不回退全库/旧 scope 数据
    if (!noteLoadError('feedbackReviewView', e)) feedbackReviewView.value = []
  }
}

/** 反馈聚合筛选视图（/api/kb/feedback-stats）。409=org_snapshot_stale ⇒ 显式错误、
 *  绝不显示成「该部门零反馈」；清筛选（''）⇒ 回全库口径（governance 数据，零请求）。 */
async function loadFeedbackStats(ownerKey: string) {
  const mySeq = ++fbStatsSeq
  if (!ownerKey) { fbStats.value = null; clearLoadError('feedbackStats'); return }
  clearLoadError('feedbackStats')
  try {
    const r = await apiJson<KbFeedbackStats>(
      `/api/kb/feedback-stats?owner_key=${encodeURIComponent(ownerKey)}`, { auth: true })
    if (mySeq !== fbStatsSeq) return
    fbStats.value = r
  } catch (e: any) {
    if (mySeq !== fbStatsSeq) return
    fbStats.value = null
    loadErrors.value['feedbackStats'] = e?.status === 409
      ? '组织快照过期，暂不可按部门筛选——可清除筛选查看全库口径'
      : (e?.detail || '部门反馈聚合加载失败')
  }
}

/** 切换「显示已处理」并重载。 */
function toggleShowResolvedFeedback() {
  showResolvedFeedback.value = !showResolvedFeedback.value
  void loadFeedbackReview()
}

/** 差评处置：resolve（已修复/跟进）/ dismiss（忽略）/ reopen（重开）。
 *  成功后据当前「显示已处理」视图定向更新：收件箱视图里 resolve/dismiss 即移除该条；
 *  否则本地翻转 handled 态。按 message_id 在途互斥防连点。 */
async function resolveFeedback(messageId: string, action: FeedbackResolveAction): Promise<boolean> {
  if (feedbackResolveBusy.value.has(messageId)) return false
  feedbackResolveBusy.value = new Set(feedbackResolveBusy.value).add(messageId)
  try {
    const s = useSession()
    const done = action !== 'reopen'
    if (import.meta.env.DEV && s.token === 'dev-preview') { /* 预览：直接本地更新 */ }
    else {
      await apiJson('/api/kb/feedback-review/resolve', { method: 'POST', auth: true, body: JSON.stringify({ message_id: messageId, action }) })
    }
    // 处置成功同步**双状态**（全库收件箱 + 部门筛选视图,2026-08-03）——两者可能各持一份列表
    const applyTo = (target: typeof feedbackReview) => {
      const list = target.value || []
      if (done && !showResolvedFeedback.value) {
        target.value = list.filter((x) => x.message_id !== messageId)   // 收件箱：处置即移出
      } else {
        target.value = list.map((x) => x.message_id === messageId
          ? { ...x, handled: done, handled_status: action === 'resolve' ? 'RESOLVED' : action === 'dismiss' ? 'DISMISSED' : 'PENDING' } : x)
      }
    }
    applyTo(feedbackReview)
    if (feedbackReviewView.value) applyTo(feedbackReviewView)
    // 收件箱语义下该行处置即移出，用户唯一的反馈就是「那一行没了」——与「我点错了」「网络没生效」
    // 「本来就没有」三种情况肉眼不可区分；处理完最后一条时空态还与「从来没有过」共用同一句文案。
    // 故成功侧必须留一条可读屏播报的回执，并顺带告知恢复路径（把消失变成可逆）。
    toast(action === 'reopen' ? '已重开这条差评'
      : action === 'dismiss' ? '已忽略（可在「显示已处理」中重开）'
      : '已标记为已处理（可在「显示已处理」中重开）')
    return true
  } catch (e: any) { void notice({ title: '处置失败', message: uploadErrText(e), danger: true }); return false }
  finally { const n = new Set(feedbackResolveBusy.value); n.delete(messageId); feedbackResolveBusy.value = n }
}

// 处置动作类型（历史上与转人工工单共用；转人工已下线 2026-07，现仅入库复审使用）。
export type EscalationResolveAction = 'resolve' | 'dismiss' | 'reopen'

// 入库复审任务（与后端 KbReviewTaskItem 对齐；P2-33 spot_checker 权限泄露安全网等的消费端）。
export interface ReviewTaskItem {
  task_id: string; doc_id: string; title: string; version_no: number
  review_type: string; review_reason: string; owner_dept: string
  suggested_permission_level: string
  created_at: string; age_days: number
  status: string; closed: boolean; reviewer_name: string
}

/** 拉入库复审任务队列（kb_admin 专属）：默认只列 PENDING（不设时间窗、老单先出）。 */
async function loadReviewTasks(offset = 0) {
  // 追加在途闸（2026-08-04 独立核验）：双击「加载更多」= 同 offset 双请求 = 同批 20 条
  // 重复追加（duplicate key）。闸只作用于追加对追加（offset>0 在途时再追加 no-op）；
  // 替换路径（offset=0，含 toggle 的 void 重载）完全不置忙也不受闸——替换本就 last-wins，
  // 且置忙会把「toggle 后立刻追加」误伤掉（既有契约测试钉着）。
  if (offset) {
    if (reviewTasksLoadBusy.value) return
    reviewTasksLoadBusy.value = true
    try { await _loadReviewTasksInner(offset) } finally { reviewTasksLoadBusy.value = false }
    return
  }
  await _loadReviewTasksInner(0)
}

async function _loadReviewTasksInner(offset = 0) {
  const s = useSession()
  if (s.role !== 'kb_admin') { reviewTasks.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    const all: ReviewTaskItem[] = [
      { task_id: 'rt1', doc_id: 'D1', title: '员工薪酬发放办法', version_no: 2,
        review_type: 'spot_check_mismatch', review_reason: '实时权限 public 比 LLM 建议 restricted 更宽松',
        owner_dept: 'hr', suggested_permission_level: 'restricted',
        created_at: '2026-06-25 08:10', age_days: 9, status: 'PENDING', closed: false, reviewer_name: '' },
    ]
    reviewTasks.value = showClosedReviewTasks.value ? all : all.filter((x) => !x.closed)
    reviewTasksHasMore.value = false
    return
  }
  clearLoadError('reviewTasks')
  try {
    // P2-11：后端 limit 默认 20 且此前不回 has_more —— 复审任务是「安全网承诺」，
    // 静默截断意味着第 21 条以后的隐患在界面上根本不存在。offset>0=追加，0=回首页替换。
    const qs = showClosedReviewTasks.value ? '&include_closed=true' : ''
    const r = await apiJson<{ items: ReviewTaskItem[]; has_more?: boolean }>(
      `/api/kb/review-tasks?offset=${offset}${qs}`, { auth: true })
    reviewTasks.value = offset ? [...(reviewTasks.value || []), ...(r.items || [])] : (r.items || [])
    reviewTasksHasMore.value = !!r.has_more
  } catch (e) { reviewTasks.value = reviewTasks.value ?? []; noteLoadError('reviewTasks', e) }
}

function toggleShowClosedReviewTasks() {
  showClosedReviewTasks.value = !showClosedReviewTasks.value
  void loadReviewTasks()
}

/** 复审任务处置：resolve（已核实/已修正）/ dismiss（误报）/ reopen；comment 可选。 */
async function resolveReviewTask(taskId: string, action: EscalationResolveAction,
                                 comment = ''): Promise<boolean> {
  if (reviewTaskResolveBusy.value.has(taskId)) return false
  reviewTaskResolveBusy.value = new Set(reviewTaskResolveBusy.value).add(taskId)
  try {
    const s = useSession()
    const done = action !== 'reopen'
    if (import.meta.env.DEV && s.token === 'dev-preview') { /* 预览：本地更新 */ }
    else {
      await apiJson('/api/kb/review-tasks/resolve', {
        method: 'POST', auth: true,
        body: JSON.stringify({ task_id: taskId, action, comment }),
      })
    }
    const list = reviewTasks.value || []
    if (done && !showClosedReviewTasks.value) {
      reviewTasks.value = list.filter((x) => x.task_id !== taskId)
    } else {
      reviewTasks.value = list.map((x) => x.task_id === taskId
        ? { ...x, closed: done, status: action === 'resolve' ? 'RESOLVED' : action === 'dismiss' ? 'DISMISSED' : 'PENDING' } : x)
    }
    // 同 resolveFeedback：处置成功即移出列表，成功侧此前零回执。
    // ⚠️ 括号里的控件名必须与 ReviewTaskQueue.vue:49 的按钮标签**逐字一致**（「显示已处理」）。
    // 初版这里写的是「显示已处置」——全仓没有任何控件叫这个名字，等于把「无信息」变成「错误指路」，
    // 而恰恰这条当时没被硬门断言覆盖（G1 有、G2 无）才漏出去。硬门 G2 已补上对称断言。
    toast(action === 'reopen' ? '已重开该复审任务'
      : action === 'dismiss' ? '已按误报忽略（可在「显示已处理」中重开）'
      : '已标记为已处理（可在「显示已处理」中重开）')
    return true
  } catch (e: any) { void notice({ title: '处置失败', message: uploadErrText(e), danger: true }); return false }
  finally { const n = new Set(reviewTaskResolveBusy.value); n.delete(taskId); reviewTaskResolveBusy.value = n }
}

// 「谁能看到这篇文档」解释器响应（与后端 KbVisibilityExplainResponse 对齐；判定与检索同源）。
export interface VisReader { dept: string; via: 'owner' | 'umbrella' | 'shared_policy' | 'grant' }
export interface VisExplain {
  doc_id: string; owner_dept: string; permission_level: string
  everyone: boolean; nobody: boolean; quarantined: boolean; active: boolean
  readers: VisReader[]
}

/** 打开「谁能看到」弹窗并拉取解释（只读；失败在弹窗内显示错误）。 */
async function openVisibility(d: DocItem): Promise<void> {
  visCtx.value = d
  visExplain.value = null
  visErr.value = ''
  visLoading.value = true
  try {
    const s = useSession()
    if (import.meta.env.DEV && s.token === 'dev-preview') {
      visExplain.value = {
        doc_id: d.doc_id, owner_dept: d.owner_dept, permission_level: d.permission_level,
        everyone: d.permission_level === 'public', nobody: d.permission_level === 'restricted',
        quarantined: false, active: true,
        readers: d.permission_level === 'dept_internal'
          ? [{ dept: d.owner_dept, via: 'owner' }, { dept: 'hr', via: 'grant' }] : [],
      }
      return
    }
    visExplain.value = await apiJson<VisExplain>(
      `/api/kb/visibility-explain?doc_id=${encodeURIComponent(d.doc_id)}`, { auth: true })
  } catch (e: any) {
    visErr.value = e && e.status === 403 ? (e.detail || '无权查看该文档的可见范围明细') : uploadErrText(e)
  } finally { visLoading.value = false }
}
function closeVisibility() { visCtx.value = null; visExplain.value = null; visErr.value = '' }

/** 该文档现行已放行的组码集合（含被动流 CSV 行拆分）——驱动弹窗「已共享」与台账副行计数。 */
// O(1) 记忆化（perf）：accessGrants 变更才重算一次。此前 grantedDeptsOf 每次调用全量扫
// grants + CSV 拆分，DocTable 模板每行调 4 次 → 每次重渲染 O(rows×4×grants)，
// 搜索框逐键/勾选切换都全量重算——grants 一多就卡。
const grantedDeptsByDoc = computed(() => {
  const m = new Map<string, string[]>()
  const sets = new Map<string, Set<string>>()
  for (const g of accessGrants.value) {
    let s = sets.get(g.doc_id)
    if (!s) { s = new Set(); sets.set(g.doc_id, s) }
    for (const p of String(g.requester_dept || '').split(',')) { const c = p.trim(); if (c) s.add(c) }
  }
  for (const [id, s] of sets) m.set(id, [...s].sort())
  return m
})
// 组码 → 中文标签的行级缓存（DocTable 副行直接 .get() O(1)）。
const grantedLabelsByDoc = computed(() => {
  const m = new Map<string, string[]>()
  for (const [id, codes] of grantedDeptsByDoc.value) m.set(id, codes.map(deptLabel))
  return m
})

function grantedDeptsOf(docId: string): string[] {
  return grantedDeptsByDoc.value.get(docId) || []
}

/** 该文档的授权行（供弹窗逐行撤销）。 */
function docGrantRows(docId: string): AccessGrantItem[] {
  return accessGrants.value.filter((g) => g.doc_id === docId)
}

function openShare(d: DocItem) { shareCtx.value = d }
function openDocMeta(d: DocItem) { docMetaCtx.value = d }
function closeDocMeta() { docMetaCtx.value = null }
function closeShare() { if (!shareBusy.value) shareCtx.value = null }

/** 直接放行：POST /api/kb/access-grants；成功后刷新已授权清单（弹窗/台账随之更新）。
 * opts.refresh=false 供批量路径抑制逐次刷新（批末统一拉一次，防 N 并发 GET 互相覆盖+烧限流）。 */
async function createGrants(docId: string, depts: string[], reason = '',
                            opts: { refresh?: boolean } = {}): Promise<GrantCreateResp | null> {
  if (!depts.length) return null
  const r = await apiJson<GrantCreateResp>('/api/kb/access-grants', {
    method: 'POST', auth: true,
    body: JSON.stringify({ doc_id: docId, target_depts: depts, ...(reason ? { reason } : {}) }),
  })
  if (opts.refresh !== false) void loadAccessGrants({ afterWrite: true })
  return r
}

/** 共享结果如实文案：以后端 granted/skipped 为准——skipped（归属自身/伞下冗余/已覆盖）
 * 没有写任何授权行，不能再按请求数报「已共享 N 部门」。 */
function shareResultNote(gr: GrantCreateResp | null): string {
  if (!gr) return ''
  const g = gr.granted?.length || 0, s = gr.skipped?.length || 0
  if (g && s) return `，已共享 ${g} 部门（${s} 个已覆盖跳过）`
  if (g) return `，已共享 ${g} 部门`
  if (s) return `，${s} 个目标已覆盖无需新授权`
  return ''
}

/** 弹窗提交：共享 shareCtx 文档给所选部门。返回 null=成功关闭；string=错误/提示文案（弹窗内联显示）。
 * 全部目标被后端跳过（伞组/共享面本就可读）→ 不关弹窗、如实提示——不能让用户以为写了授权。 */
async function submitShare(depts: string[], reason: string): Promise<string | null> {
  const d = shareCtx.value
  if (!d || !depts.length) return '请选择要共享的部门'
  shareBusy.value = true
  try {
    const r = await createGrants(d.doc_id, depts, reason)
    if (r && !r.granted?.length && r.skipped?.length) {
      return `所选部门本就可读该文档（生产伞组/营销共享面覆盖），无需新增授权：${r.skipped.map(deptLabel).join('、')}`
    }
    shareCtx.value = null
    return null
  } catch (e: any) {
    return e && e.status === 403 ? (e.detail || '无权共享该文档') : uploadErrText(e)
  } finally { shareBusy.value = false }
}

/** node-ACL：保存文档的组织树节点可见范围 —— **整体替换**（POST /api/kb/doc-node-grants）。
 * 未勾选的节点会被撤销；acl_revision 做 CAS，并发保存时后到者收 409（绝不静默丢勾选）。
 * 返回 null=成功；string=错误文案。 */
async function saveNodeGrants(
  docId: string, nodes: { dept_id: number; subtree: boolean }[],
  aclRevision: number | null, reason = ''): Promise<string | null> {
  if (!docId) return '缺少文档'
  shareBusy.value = true
  try {
    await apiJson('/api/kb/doc-node-grants', {
      method: 'POST', auth: true,
      body: JSON.stringify({ doc_id: docId, nodes, acl_revision: aclRevision, reason }),
    })
    shareCtx.value = null
    await loadDocs()
    return null
  } catch (e: any) {
    if (e?.status === 409) return e.detail || '可见范围已被他人修改，请刷新后重试'
    if (e?.status === 422) return e.detail || '所选节点过多，请改选更上层节点'
    return e?.status === 403 ? (e.detail || '无权管理该文档') : uploadErrText(e)
  } finally { shareBusy.value = false }
}

/** 重设基础可见范围（dept_internal / public / restricted）：POST /api/kb/set-visibility。
 * 成功后即时反映行的 permission_level + 权威 loadDocs 纠正徽章（restricted 会离开检索）。
 * opts.reload=false 供批量路径抑制逐篇重拉（_bulkRun 批末已有一次权威 loadDocs——否则
 * N 篇 = N+1 次全量列表拉取，烧 aux 限流且批中列表反复重置分页/闪动）。
 * 返回 null=成功；string=错误文案。 */
async function setVisibility(d: DocItem, level: string, reason = '',
                             opts: { reload?: boolean; expectedAclRevision?: number | null } = {},
                             ): Promise<string | null> {
  return withInflight(`vis:${d.doc_id}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') { d.permission_level = level; return null }
      await apiJson('/api/kb/set-visibility', {
        method: 'POST', auth: true,
        // R1（Sam 2026-08-04）：带上 expected_acl_revision ⇒ 后端强制 CAS，并发改动返 409。
        // ⚠️ 只有**手上确实有**该版本号的调用方才传（ShareDocModal 开弹窗即拉 doc-meta）；
        // 批量路径暂不传 —— 后端对"没带"按现状放行，但**无论如何都 bump**，
        // 所以 doc-meta 侧的 CAS 立刻能感知到可见范围变更（原本完全感知不到）。
        body: JSON.stringify({
          doc_id: d.doc_id, permission_level: level, ...(reason ? { reason } : {}),
          ...(opts.expectedAclRevision != null ? { expected_acl_revision: opts.expectedAclRevision } : {}),
        }),
      })
      d.permission_level = level                     // 乐观即时反映
      if (opts.reload !== false) void loadDocs()     // 权威纠正（restricted→可能掉出列表/改徽章）
      return null
    } catch (e: any) {
      // 409 现在有两种成因：非在线状态（旧）/ acl_revision CAS 落空（R1，新）。
      // 后端 detail 已分别写明，原样透出即可，别再套死"该文档非在线状态"。
      return e && e.status === 403 ? (e.detail || '无权修改该文档可见范围')
        : e && e.status === 409 ? (e.detail || '该文档状态已变化，请刷新后重试') : uploadErrText(e)
    }
  }) as Promise<string | null>
}

// 审批历史（只读聚合）：后端按角色作用域 —— dept_admin 见本部门 access+contribution、kb_admin 见全库四类。
// 404（未上线）静默兜底空；5xx 显错。DEV ?preview 注入 mock（kb_admin 见四类混合、dept_admin 仅两类）。
async function loadApprovalHistory() { return withLoader('approvalHistory', _loadApprovalHistory) }
async function _loadApprovalHistory() {
  const s = useSession()
  if (!s.identity?.canManage) { approvalHistory.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    const base: ApprovalHistoryItem[] = [
      { kind: 'access', action: 'approved', title: '客户投诉处理 SOP', owner_dept: 'marketing', subject: '王伟', detail: '生产部包装设计需引用营销规范。', extra: '', decided_by: 'kb001', decided_by_name: '系统管理员', decided_at: '2026-06-28 14:32:10' },
      { kind: 'access', action: 'rejected', title: '海外客户名录 v2', owner_dept: 'marketing', subject: '赵强', detail: '涉客户隐私，暂不外放。', extra: '', decided_by: 'mgr001', decided_by_name: '王伟', decided_at: '2026-06-27 10:05:00' },
      { kind: 'contribution', action: 'accepted', title: '2ozpp杯在龙盛机上的速度是多少？', owner_dept: 'production', subject: '孙工', detail: '', extra: 'searchable', decided_by: 'mgr002', decided_by_name: '李娜', decided_at: '2026-06-27 09:15:22' },
      { kind: 'contribution', action: 'rejected', title: '请假流程能不能加急', owner_dept: 'hr', subject: '周敏', detail: '与现行制度冲突，未采纳。', extra: '', decided_by: 'mgr003', decided_by_name: '陈立', decided_at: '2026-06-26 16:40:00' },
    ]
    const adminOnly: ApprovalHistoryItem[] = [
      { kind: 'upload', action: 'approved', title: '注塑车间安全操作规程 v4.docx', owner_dept: 'production', subject: '', detail: '', extra: '', decided_by: 'kb001', decided_by_name: '系统管理员', decided_at: '2026-06-28 11:20:00' },
      { kind: 'upload', action: 'rejected', title: '旧版包装规范.pdf', owner_dept: 'marketing', subject: '', detail: '内容过期，已被 v3 取代。', extra: '', decided_by: 'kb001', decided_by_name: '系统管理员', decided_at: '2026-06-26 17:40:00' },
      { kind: 'admin_grant', action: 'granted', title: 'mgr002', owner_dept: '', subject: 'mgr002', detail: 'grant dept_admin mgr002 → quality,production', extra: '', decided_by: 'kb001', decided_by_name: '系统管理员', decided_at: '2026-06-25 09:00:00' },
    ]
    approvalHistory.value = s.role === 'kb_admin'
      ? [...base, ...adminOnly].sort((a, b) => (a.decided_at < b.decided_at ? 1 : -1))
      : base
    return
  }
  clearLoadError('approvalHistory')
  try {
    const r = await apiJson<{ items: ApprovalHistoryItem[] }>('/api/kb/approval-history', { auth: true })
    approvalHistory.value = r.items || []
  } catch (e) { approvalHistory.value = []; noteLoadError('approvalHistory', e) }
}

// ── Phase F：成员/角色管理（仅 kb_admin）──
// adminSeq 竞态守卫（与 accessGrants 的 grantsSeq 对称）：合流在时同 key 不可能并发，缺口被掩盖；
// afterWrite 打开真并发后暴露——withInflight 的 key 是 `member:${userId}`（按人），撤销两个不同
// dept_admin 可以重叠，若旧快照后到就会覆盖新清单，界面出现「已撤销的行复活」。
let adminSeq = 0
/** `afterWrite`：写后权威重拉，禁止合流（理由见 withLoader 注释）。读路径不传，照常合流。 */
async function loadAdminGrants(o?: { afterWrite?: boolean }) { return withLoader('adminGrants', _loadAdminGrants, { join: !o?.afterWrite }) }
async function _loadAdminGrants() {
  const seq = ++adminSeq
  const s = useSession()
  if (s.role !== 'kb_admin') { adminGrants.value = []; grantableDepts.value = []; return }
  if (import.meta.env.DEV && s.token === 'dev-preview') {
    adminGrants.value = [
      { user_id: 'mgr001', user_name: '王伟', role: 'dept_admin', managed_owner_depts: ['marketing'] },
      { user_id: 'mgr002', user_name: '李娜', role: 'dept_admin', managed_owner_depts: ['quality', 'production'] },
      { user_id: 'kb001', user_name: '系统管理员', role: 'kb_admin', managed_owner_depts: [] },
    ]
    grantableDepts.value = ['marketing', 'production', 'quality', 'finance', 'hr', 'supply', 'pmc', 'rd', 'admin', 'it']
    return
  }
  clearLoadError('adminGrants')
  try {
    const r = await apiJson<{ items: AdminItem[]; grantable_owner_depts: string[] }>('/api/kb/admin-grants', { auth: true })
    if (seq !== adminSeq) return           // 竞态守卫：仅最新结果落地
    adminGrants.value = r.items || []
    grantableDepts.value = r.grantable_owner_depts || []
  } catch (e) { if (seq === adminSeq) { adminGrants.value = []; grantableDepts.value = []; noteLoadError('adminGrants', e) } }   // 404 静默；5xx 显错
}

// 阶段 B：管辖根候选队列（org_sync 派生的中心级/超规模/换根待 kb_admin 确认）
async function loadNodeCandidates() {
  const s = useSession()
  if (s.role !== 'kb_admin') { nodeCandidates.value = []; return }
  try {
    const r = await apiJson<{ items: NodeCandidate[]; truncated?: boolean }>('/api/kb/admin-node-candidates', { auth: true })
    nodeCandidates.value = r.items || []
    truncatedQueues.nodeCandidates = !!r.truncated
  } catch { nodeCandidates.value = []; truncatedQueues.nodeCandidates = false }
}

/** 确认/驳回候选。409 = 快照已翻或候选失效——刷新队列并把后端文案回给调用方。 */
// 审计发现：这是全组件里唯一**零防护**的写操作——按钮无 :disabled、这里也无 busy 追踪，
// 连点可并发发出多个决策请求，且在途期间界面无任何反应（同文件其余 5 个按钮都有 isBusy）。
// 补上与它们同一套 withInflight 互斥；key 按候选 id，两个候选可各自独立操作。
// 在途重复调用返回 undefined ⇒ 这里归一成 null（等同「没有错误可报」），不让调用方把
// 「被互斥拦下」误显示成失败。
async function decideNodeCandidate(id: number, action: 'confirm' | 'reject'): Promise<string | null> {
  const r = await withInflight(`cand:${id}`, async (): Promise<string | null> => {
    try {
      await apiJson('/api/kb/admin-node-candidates/decide', {
        method: 'POST', auth: true, body: JSON.stringify({ candidate_id: id, action }) })
      // 第七个写后重拉点（我最初的清单只列了六个，把它误记成读路径）：确认候选会改管辖根，
      // 名单必须重取，同样禁止合流。
      await Promise.all([loadNodeCandidates(), loadAdminGrants({ afterWrite: true })])
      return null
    } catch (e: any) {
      await loadNodeCandidates()
      return e?.detail?.message || e?.detail || '操作失败，请刷新后重试'
    }
  })
  return r ?? null
}

// 授予/更新一名部门管理员（owner_depts = 权威全集,提交即覆盖）。成功返回 true。
// 阶段 B：nodeRoots —— undefined=不动节点轴；[]=清空；[ids]=该轴权威全集（覆盖按轴隔离）。
async function grantDeptAdmin(userId: string, userName: string, ownerDepts: string[], note: string,
                              nodeRoots?: number[]): Promise<boolean> {
  return (await withInflight(`member:${userId}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') {
        const i = adminGrants.value.findIndex((a) => a.user_id === userId)
        const row: AdminItem = { user_id: userId, user_name: userName, role: 'dept_admin', managed_owner_depts: [...ownerDepts] }
        adminGrants.value = i >= 0 ? adminGrants.value.map((a, k) => (k === i ? row : a)) : [...adminGrants.value, row]
        return true
      }
      await apiJson('/api/kb/admin-grants', { method: 'POST', auth: true, body: JSON.stringify({ user_id: userId, user_name: userName, owner_depts: ownerDepts, note, ...(nodeRoots !== undefined ? { node_roots: nodeRoots } : {}) }) })
      await loadAdminGrants({ afterWrite: true })
      return true
    } catch (e: any) { void notice({ title: '授予失败', message: uploadErrText(e), danger: true }); return false }
  })) ?? false   // 在途（重复点击）→ 视为未提交
}

// 撤销：ownerDept 指定→撤该一项；nodeRoot 指定→撤该管辖根；两者皆空→撤两轴全部
// （降级判定后端看两表——node-only 管理员不误降）。
async function revokeAdminGrant(userId: string, ownerDept = '', nodeRoot = 0): Promise<void> {
  await withInflight(`member:${userId}`, async () => {
    try {
      const s = useSession()
      if (import.meta.env.DEV && s.token === 'dev-preview') {
        adminGrants.value = adminGrants.value
          .map((a) => (a.user_id === userId ? { ...a, managed_owner_depts: ownerDept ? a.managed_owner_depts.filter((d) => d !== ownerDept) : [] } : a))
          .filter((a) => a.role === 'kb_admin' || a.managed_owner_depts.length > 0)   // 无授权剩余 → 视为降级移出
        return
      }
      await apiJson('/api/kb/admin-grants/revoke', { method: 'POST', auth: true, body: JSON.stringify({ user_id: userId, owner_dept: ownerDept, node_root: nodeRoot }) })
      // 注意这里是「权威重拉」而非本地 splice（正确性上是对的），所以 UI 落定要等两次串行往返
      // （实测 ~960ms）；toast 挂在重拉之后，保证播报时列表已是最终状态。
      // dev-preview 分支走上面的本地 splice 且提前 return——刻意不合并两条路径：
      // __tests__/member-role-manager.spec.ts:84-96 断言的正是那条本地 splice 的即时性。
      await loadAdminGrants({ afterWrite: true })
      toast(ownerDept || nodeRoot ? '已撤销该部门的管理权限' : '已撤销全部管理权限')
    } catch (e: any) { void notice({ title: '撤销失败', message: uploadErrText(e), danger: true }) }
  })
}

async function retire(d: DocItem): Promise<{ ok: boolean; msg?: string }> {
  if (retireBusy.value) return { ok: false }
  retireBusy.value = true
  try {
    const r = await apiJson<RetireResp>('/api/kb/retire', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id }) })
    d.status_badge = '已退役'                       // 即时反映行
    await loadDocs()                                // await 重拉，让服务端权威徽章纠正乐观值（对齐 approve/reject；G3）
    return { ok: true, msg: r.note }
  } catch (e: any) {
    const msg = e && e.status === 403 ? (e.detail || '无权退役该文档') : uploadErrText(e)
    return { ok: false, msg }
  } finally { retireBusy.value = false }
}

// 恢复上线（退役逆操作）：重新激活 + 标脏待重索引；HA3 未删则即时可检索，否则下次维护重索引后恢复。
async function restore(d: DocItem): Promise<{ ok: boolean; msg?: string }> {
  if (retireBusy.value) return { ok: false }
  retireBusy.value = true
  try {
    if (import.meta.env.DEV && useSession().token === 'dev-preview') { d.status_badge = '排队中'; return { ok: true } }
    const r = await apiJson<{ note?: string }>('/api/kb/restore', { method: 'POST', auth: true, body: JSON.stringify({ doc_id: d.doc_id }) })
    d.status_badge = '排队中'                       // 即时反映（NOT_INDEXED → 待重索引）
    await loadDocs()                                // await 重拉，把乐观「排队中」纠正为服务端权威徽章（HA3 未删则即时「已上线」；G3）
    return { ok: true, msg: r.note }
  } catch (e: any) {
    const msg = e && e.status === 403 ? (e.detail || '无权恢复该文档') : uploadErrText(e)
    return { ok: false, msg }
  } finally { retireBusy.value = false }
}

export function useKb() {
  const session = useSession()
  ensurePrincipalWatch(session)   // 身份切换 ⇒ 清上传草稿（每 store 实例注册一次）
  const ownerDepts = computed(() => session.identity?.managedOwnerDepts ?? [])
  // 上传目标选项(含生产子线);toIdentity 已做旧后端回退,此处仅兜底空值。
  const uploadTargetDepts = computed(() => {
    const u = session.identity?.uploadTargetDepts
    return u && u.length ? u : (session.identity?.managedOwnerDepts ?? [])
  })
  const isKbAdmin = computed(() => session.role === 'kb_admin')
  const isDeptAdmin = computed(() => session.role === 'dept_admin')
  // 待你审核的数量（红点/角标单一来源）：kb_admin = 待审批上传 + 授权申请；dept_admin = 授权申请（其本部门文档的）。
  // 上传审批仅 kb_admin（/pending-approvals kb-only），故 dept_admin 的 approvals 恒空、不计入。
  // 待你审核数（侧栏红点/tab 角标）：kb_admin=上传审批（只管入库）；dept_admin=授权申请
  // （部门管理员之间的事）。两角色职权不重叠——拍板见 2026-07-04。
  const reviewCount = computed(() => (session.role === 'kb_admin' ? approvals.value.length : accessRequests.value.length))

  return {
    // 状态
    docs, filtered, approvals, accessRequests, queuesSettled, accessGrants, approvalHistory, adminGrants, grantableDepts, loadingDocs, loadingMoreDocs, hasMoreDocs, docScope, q, filter, permFilter, ownerFilter, citedFilter, ownerOptions, sortKey, sortDir,
    // #7 全库口径 + 服务端筛选 setter
    ledgerBadgeChips, ledgerBadgeCount, ledgerOwnerOptions, anomalyCount, ledgerTotal,
    setBadgeFilter, setPermFilter, setOwnerFilter, setCitedFilter, clearLedgerFilters,
    // 页码翻页器（DocTable 尾部 QueuePager）
    docsPage, docsTotal, docsPerPage: DOCS_PAGE, docsMaxPage: DOCS_MAX_PAGE, loadDocsPage,
    // 多选 / 批量
    selectableVisible, selectedDocs, selectedCount, allVisibleSelected, isSelected, toggleSelect, toggleSelectAllVisible, clearSelection, bulkBusy, bulkMsg, bulkDone, bulkTotal, bulkRetire, bulkSetVisibility,
    newTitle, newOwner, newOwnerNode, newPerm, newShareDepts, newShareNodes, nodeAclGrant, verCtx, uploadBusy, uploadMsg, uploadPct, uploadErr, uploadOk,
    dupWarn, contentDupMsg, uploadQueue, selectedNames, isBusy, retireBusy,
    accessReqDoc, accessReqBusy, requestedDocIds, myAccessReqs,
    shareCtx, shareBusy, shareTargets: SHARE_TARGETS,
    ownerDepts, uploadTargetDepts, isKbAdmin, isDeptAdmin, reviewCount, kbStats, kbConfig, kbInsights, kbGovernance, kbOpsMetrics, maxUploadMb, verHistory, loadErrors, isLoading, hasSettled,
    // 方法
    loadDocs, loadMoreDocs, loadStats, loadConfig, loadInsights, loadGovernance, loadOpsMetrics, openHistory, closeHistory, openDocPreview, setQuery, loadApprovals, sortBy, countOf,
    loadAccessRequests, approveAccess, rejectAccess, loadAccessGrants, revokeAccess, loadApprovalHistory, setScope,
    openShare, closeShare, submitShare, saveNodeGrants, grantedDeptsOf, grantedLabelsByDoc, docGrantRows, setVisibility,
    docMetaCtx, openDocMeta, closeDocMeta,
    nodeCandidates, loadNodeCandidates, decideNodeCandidate,
    visCtx, visExplain, visLoading, visErr, openVisibility, closeVisibility,
    feedbackReview, feedbackReviewTruncated, truncatedQueues, loadFeedbackReview, showResolvedFeedback, toggleShowResolvedFeedback, resolveFeedback, feedbackResolveBusy,
    feedbackReviewView, feedbackReviewViewTruncated, loadFeedbackReviewView, fbStats, loadFeedbackStats,
    reviewTasks, loadReviewTasks, reviewTasksHasMore, reviewTasksLoadBusy, showClosedReviewTasks, toggleShowClosedReviewTasks, resolveReviewTask, reviewTaskResolveBusy,
    loadAdminGrants, grantDeptAdmin, revokeAdminGrant,
    openAccessRequest, closeAccessRequest, submitAccessRequest, accessStateOf, accessNoteOf, loadMyAccessRequests,
    enterVersionMode, exitVersionMode, applyPendingVersion, onFileSelected, doUpload,
    maybeAutoSeedOwner, resetUploadDraft, autoSeedDoneFor,
    approve, reject, retire, restore,
  }
}

/** 仅供测试：重置 store。（分支侧 P0-D 已升级为运行期身份切换共用的 _resetKbState——随大合并收敛。） */
export function __resetKb() {
  docs.value = []; kbStats.value = null; kbInsights.value = null; kbGovernance.value = null; kbOpsMetrics.value = null; kbConfig.value = null; verHistory.value = null; approvals.value = []; accessRequests.value = []; accessGrants.value = []; approvalHistory.value = []; adminGrants.value = []; grantableDepts.value = []; loadingDocs.value = false; loadingMoreDocs.value = false; hasMoreDocs.value = false; loadErrors.value = {}; serverBadgeCounts.value = null
  docScope.value = 'managed'; accessReqDoc.value = null; accessReqBusy.value = false; requestedDocIds.value = new Set(); myAccessReqs.value = new Map()
  q.value = ''; filter.value = ''; permFilter.value = ''; ownerFilter.value = ''; citedFilter.value = ''; sortKey.value = 'updated_at'; sortDir.value = -1
  selectedIds.value = new Set(); bulkBusy.value = false; bulkMsg.value = ''
  bulkDone.value = 0; bulkTotal.value = 0; uploadPct.value = null
  resetUploadDraft()                                // 上传草稿域（含 selectedFiles/autoSeedDoneFor/轮询作废）
  shareCtx.value = null; shareBusy.value = false
  uploadBusy.value = false
  inflight.value = new Set(); retireBusy.value = false
  feedbackReview.value = null; showResolvedFeedback.value = false; feedbackResolveBusy.value = new Set()
  reviewTasks.value = null; showClosedReviewTasks.value = false; reviewTaskResolveBusy.value = new Set()
  docsOffset = 0; docsPage.value = 1; docsSeq = 0; trackSeq = 0
  for (const k of Object.keys(lastLoadedAt)) delete lastLoadedAt[k]   // 重开 staleness 门（#82）
  if (qTimer) { clearTimeout(qTimer); qTimer = null }
  if (filterTimer) { clearTimeout(filterTimer); filterTimer = null }
  loaderInflight.value = new Set(); loaderSettled.value = new Set()
  loaderPromises.clear()
  // ⚠️ 刻意**不清** loaderCount / loaderGen —— 我原先清了它们，注释里的理由还写反了：
  //   · loaderCount：不清才守恒（旧 1 → 新 2 → 旧落地 1 → 新落地 0）。清了才会让旧请求把
  //     新请求的计数减到 0，骨架提前撤——正是计数式记账要消灭的那个现象。
  //   · loaderGen：清了会让新请求的世代号从 1 重来、与重置前仍在途的旧请求撞号，旧请求落地
  //     时判等成立 ⇒ 删掉新请求的登记 ⇒ 后续读路径不再合流、多发一次请求。世代号本就是防这个的。
  // 两者都是模块闭包内的单调/守恒量，跨身份不携带任何用户数据，无需复位。
  clearToasts()   // 安全项：队列是模块级的，不清会让 A 的「已撤销管理权限」飘到 B 的界面上
}

/** 仅供测试：注入选中文件（绕过 input）。 */
export function __setSelectedFiles(files: File[]) { selectedFiles = files }
