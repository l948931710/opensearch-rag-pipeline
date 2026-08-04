import type { Page } from '@playwright/test';

/**
 * node-ACL（组织树授权）模式的 e2e mock 基座。
 *
 * 进入方式与其它硬门一致：?token= 透传免登 + page.route mock（绝不用 ?preview——
 * 带 auth 请求在 preview 哨兵下被 503 短路，kbConfig 拿不到 node_acl_grant）。
 * 关键三件：/api/kb/config 开 node_acl_grant、/api/kb/org-tree 回快照 +
 * my_managed_node_roots、/api/kb/whoami 回 dept_admin 身份。
 */

export const MANAGE_ROUTE = '/console/manage?token=e2e-fake-token';

/** 生产形状组织树（org_sync 契约：**中心=depth 1、无公司根节点**，根的直接子部门起记）：
 *  中心(d1)→事业部(d2)→车间(d3)，覆盖深层归属、「0 人节点」防呆与中心卷积场景。 */
export const ORG_NODES = [
  { dept_id: 2, parent_id: 1, name: '生产中心', depth: 1, staff_count: 800, direct_staff_count: 4 },
  { dept_id: 3, parent_id: 2, name: '注塑事业部', depth: 2, staff_count: 300, direct_staff_count: 8 },
  { dept_id: 4, parent_id: 3, name: '注塑一车间', depth: 3, staff_count: 120, direct_staff_count: 120 },
  { dept_id: 5, parent_id: 2, name: '吸管事业部', depth: 2, staff_count: 200, direct_staff_count: 10 },
  { dept_id: 6, parent_id: 1, name: '质量中心', depth: 1, staff_count: 60, direct_staff_count: 60 },
  { dept_id: 7, parent_id: 1, name: '营销中心', depth: 1, staff_count: 90, direct_staff_count: 90 },
];

/** 治理/成效 mock（node:<id> 桶 + legacy + unknown 混布——重设计前的基线会把 node 桶显示成裸键）。
 *  注塑(d2)+车间(d3) 应卷到生产中心；质量中心自身持桶（「本级」子行场景）。 */
export const GOVERNANCE_MOCK = {
  window_days: 30, monitor_heartbeat_age_h: 2, monitor_stale: false,
  file_types: [{ ftype: 'docx', count: 31 }, { ftype: 'pdf', count: 18 }, { ftype: 'xlsx', count: 9 }],
  docs_active: 58, docs_in_index: 57, dual_version_docs: 0,
  avg_latency_ms: 5200, p50_latency_ms: 4100, p95_latency_ms: 9800,
  avg_retrieval_ms: 600, avg_llm_ms: 4200,
  embed_runs: [
    { bizdate: '20260801', embedded: 240, failed: 0, fail_rate: 0 },
    { bizdate: '20260802', embedded: 180, failed: 2, fail_rate: 0.011 },
  ],
  qa_api_success_rate: 0.994, retrieval_api_success_rate: 0.998, errors_24h: 1, qa_total_30d: 1240,
  pii_redacted_docs: 4, pii_quarantined_docs: 0,
  answer_total: 1240, answer_success: 1080, answer_refusal: 96, answer_no_result: 40, answer_error: 24,
  effective_rate: 0.87,
  feedback_up: 96, feedback_down: 14, feedback_total: 110, helpful_rate: 0.873,
  feedback_last7: 31,
  feedback_daily: [{ day: '08-01', up: 12, down: 2 }, { day: '08-02', up: 15, down: 1 }],
  downvote_reasons: [{ reason: '答非所问', count: 6 }, { reason: '信息过时', count: 4 }],
  dept_coverage: [
    { owner_dept: 'node:3', owner_label: '注塑事业部', docs: 23, new_month: 3, qa_hits: 145, no_answer_rate: 0.08, pii_docs: 2, wow_net: 2, wow_total: 0.1, qa_wow_net: 12, qa_wow: 0.09, qa_hits_7d: 41 },
    { owner_dept: 'node:4', owner_label: '注塑一车间', docs: 9, new_month: 1, qa_hits: 52, no_answer_rate: 0.12, pii_docs: 0, wow_net: 1, wow_total: 0.13, qa_wow_net: -3, qa_wow: -0.05, qa_hits_7d: 12 },
    { owner_dept: 'node:6', owner_label: '质量中心', docs: 12, new_month: 0, qa_hits: 88, no_answer_rate: 0.05, pii_docs: 1, wow_net: 0, wow_total: 0, qa_wow_net: 5, qa_wow: 0.06, qa_hits_7d: 25 },
    { owner_dept: 'marketing', owner_label: 'marketing', docs: 12, new_month: 1, qa_hits: 30, no_answer_rate: 0.2, pii_docs: 0, wow_net: -1, wow_total: -0.08, qa_wow_net: 0, qa_wow: null, qa_hits_7d: 8 },
    { owner_dept: 'unknown', owner_label: 'unknown', docs: 2, new_month: 0, qa_hits: 3, no_answer_rate: 0.33, pii_docs: 0, wow_net: 0, wow_total: 0, qa_wow_net: 0, qa_wow: null, qa_hits_7d: 1 },
  ],
};

export const INSIGHTS_MOCK = {
  scope: 'all', window_days: 30,
  questions: 1240, askers: 210, success: 1080, refusal: 96, cited: 640, helped_users: 180, effective_rate: 0.87,
  top_docs: [
    { title: '注塑机保养作业指导书', owner_dept: 'node:3', hits: 88 },
    { title: '质检抽样规范', owner_dept: 'node:6', hits: 54 },
    { title: '营销费用报销流程', owner_dept: 'marketing', hits: 21 },
  ],
  gap_queries: [{ query: '吸管产线换模流程', count: 12, avg_top: 0.41 }],
};

export interface NodeModeOpts {
  /** whoami 角色（默认 dept_admin） */
  role?: 'dept_admin' | 'kb_admin';
  /** org-tree 的 my_managed_node_roots；undefined = 字段缺失（旧后端） */
  managedRoots?: number[];
  /** 组织快照 stale 标记 */
  stale?: boolean;
  /** org_tree 整体为 null（快照不可用） */
  orgTreeNull?: boolean;
  /** 台账文档行（默认一行 node 文档，供 DocMetaModal 入口） */
  docs?: Record<string, unknown>[];
}

export const NODE_DOC = {
  doc_id: 'nd1', title: '注塑机保养作业指导书', original_filename: 'sop_injection.docx',
  owner_dept: '', permission_level: 'dept_internal',
  current_version_no: 3, status: 'active', status_badge: '已上线',
  updated_at: '2026-08-01 10:00',
};

export const DOC_META_RESPONSE = {
  doc_id: 'nd1', title: '注塑机保养作业指导书', category_l1: '生产', category_l2: '设备保养',
  permission_level: 'dept_internal', status: 'active', acl_mode: 'node',
  owner_dept: '', owner_dept_id: 3, owner_key: 'node:3', owner_label: '注塑事业部',
  acl_revision: 5,
  node_grants: [
    { dept_id: 3, scope: 'subtree', name: '注塑事业部', active: true },
    { dept_id: 6, scope: 'subtree', name: '质量中心', active: true },
  ],
  legacy_grants: [],
};

export async function mockNodeMode(page: Page, opts: NodeModeOpts = {}) {
  const role = opts.role ?? 'dept_admin';
  await page.route('**/api/**', (r) => r.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], questions: [], docs: [], total: 0, has_more: false }),
  }));
  await page.route('**/api/kb/whoami', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      user_id: 'mgr_e2e', display_name: '注塑管理员', role,
      can_manage_kb: true, acl_groups: ['production'],
      managed_owner_depts: role === 'kb_admin' ? [] : ['production_injection'],
    }),
  }));
  await page.route('**/api/kb/config', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      max_upload_bytes: 209715200,
      accepted_exts: ['pdf', 'docx', 'xlsx', 'pptx', 'jpg', 'png'],
      node_acl_grant: true,
    }),
  }));
  await page.route('**/api/kb/org-tree', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      acl_groups: [], dept_name_to_groups: {}, my_role: role,
      my_managed_owner_depts: role === 'kb_admin' ? [] : ['production_injection'],
      my_grantable_owner_depts: [],
      node_acl_grant: true,
      org_tree: opts.orgTreeNull ? null : {
        nodes: ORG_NODES, snapshot_rev: 42, synced_at: '2026-08-03 00:15:00',
        stale: !!opts.stale, staff_total: 1200,
      },
      ...(opts.managedRoots !== undefined ? { my_managed_node_roots: opts.managedRoots } : {}),
    }),
  }));
  await page.route('**/api/kb/governance**', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify(GOVERNANCE_MOCK),
  }));
  await page.route('**/api/kb/insights**', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify(INSIGHTS_MOCK),
  }));
  await page.route('**/api/kb/stats**', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      total: 58, active: 56, retired: 2, chunks: 3120, new_this_month: 5,
      by_badge: { 已上线: 52, 处理中: 3, 排队中: 1, 待审核: 2 },
    }),
  }));
  await page.route('**/api/kb/my-docs**', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      items: opts.docs ?? [NODE_DOC], total: 1, has_more: false,
    }),
  }));
  await page.route('**/api/kb/doc-meta**', (r) => {
    if (r.request().method() === 'GET') {
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify(DOC_META_RESPONSE) });
    }
    return r.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ doc_id: 'nd1', acl_mode: 'node', acl_revision: 6, changed: [], ok: true }),
    });
  });
}

export async function gotoManageDocs(page: Page) {
  await page.goto(MANAGE_ROUTE);
  const zone = page.locator('[aria-label="管理台分区"]');
  await zone.getByRole('tab', { name: /文档管理/ }).click();
}
