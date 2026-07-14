import { test, expect } from '@playwright/test';
import {
  attachConsoleGuard,
  assertNoHorizontalScroll,
  assertKeyActionsVisible,
  assertHasWayForward,
  assertBackPreservesData,
  assertDestructiveConfirmed,
  assertAsyncFeedbackAndNoDoubleSubmit,
  assertErrorHasRecovery,
} from './ux-gate.helpers';

/**
 * UX 硬门。对应 dispatcher 第 4 步——pass/fail 只由这里判定。
 * 路由已按本项目预填(应用基址 /console/):管理页 = /console/manage,问答页 = /console/。
 * ‼️ 仍需把 getByTestId(...) 换成应用里真实存在的 data-testid,并按注释构造空/错误等状态。
 *    建议给关键元素加稳定的 data-testid,别依赖文案或样式类。
 */

const ROUTE = '/console/manage'; // 知识库管理页(文档列表 / 删除 / 表单 / 提交)

// ⚠️ 暂跳过:本组的 getByTestId('primary-action'/'doc-row'/'submit-btn' …) 仍是未接线的 TODO 占位,
//    且未用 ?token= 离线登录入口 → 会卡「正在登录」。待"管理页 testid 接线"那一轮做完再 .skip→.describe 打开。
//    现状下 `npm run e2e` 只跑下方已接线的「AI 助手」组(12/12 绿),避免占位用例produce 假红。
test.describe.skip('UX 硬门 — 目标页面（待管理页 testid 接线）', () => {
  test('页面打开且控制台/网络干净', async ({ page }) => {
    const guard = attachConsoleGuard(page, [
      // /\/api\/health/  // 例:放行已知探针
    ]);
    await page.goto(ROUTE);
    await page.waitForLoadState('networkidle');
    guard.assertClean();
  });

  test('无整页横向滚动(三个视口都跑)', async ({ page }) => {
    await page.goto(ROUTE);
    await page.waitForLoadState('networkidle');
    await assertNoHorizontalScroll(page);
  });

  test('关键操作首屏可见', async ({ page }) => {
    await page.goto(ROUTE);
    await assertKeyActionsVisible([
      page.getByTestId('primary-action'), // TODO: 本页最重要的 1~3 个操作
      // page.getByTestId('pending-items'),
    ]);
  });

  test('空状态不是死胡同', async ({ page }) => {
    // TODO: 导航或注入到空数据状态
    await page.goto(`${ROUTE}?state=empty`);
    await assertHasWayForward(page, [
      page.getByRole('button', { name: /新建|创建|去添加|add|create/i }),
      page.getByRole('link', { name: /返回|back/i }),
    ]);
  });

  test('请求错误态有恢复路径', async ({ page }) => {
    // TODO: 用 route mock 制造一次接口失败
    await page.route('**/api/**', (r) => r.fulfill({ status: 500, body: '{}' }));
    await page.goto(ROUTE);
    await assertErrorHasRecovery({
      errorRegion: page.getByTestId('error-state'),
      recoveryAction: page.getByRole('button', { name: /重试|retry/i }),
    });
  });

  test('表单返回不丢数据', async ({ page }) => {
    await page.goto(ROUTE);
    // TODO: 打开你的表单/抽屉
    await assertBackPreservesData({
      field: page.getByTestId('doc-name-input'),
      value: '季度合规审查-临时草稿',
      leaveAndReturn: async () => {
        await page.getByRole('button', { name: /取消|返回/i }).click();
        await page.getByTestId('open-form').click(); // 再次打开
      },
    });
  });

  test('删除有二次确认且可取消', async ({ page }) => {
    await page.goto(ROUTE);
    const firstRow = page.getByTestId('doc-row').first();
    await assertDestructiveConfirmed({
      page,
      trigger: firstRow.getByRole('button', { name: /删除|移除/i }),
      rowStillThere: firstRow,
    });
  });

  test('提交有反馈且防重复提交', async ({ page }) => {
    await page.goto(ROUTE);
    // TODO: 定位你的提交按钮与 loading 指示
    await assertAsyncFeedbackAndNoDoubleSubmit({
      submit: page.getByTestId('submit-btn'),
      loadingIndicator: page.locator('[aria-busy="true"], [data-loading="true"]'),
    });
  });
});

test.describe('UX 硬门 — AI 助手交互', () => {
  // 问答页(QaView)。用 ?token= 透传登录(useAuth 真实路径)+ mock /api/kb/whoami 取身份 → ready=true。
  // ⚠️ 不能用 ?preview:它在 apiFetch 里把 authed 请求合成 503 直接短路、根本不走网络,page.route 截不到,
  //    /api/ask/stream 等 mock 全部失效。?token 走真实 fetch,mock 才生效。两者都不需要真后端。
  const CHAT_ROUTE = '/console/?token=e2e-fake-token';

  // 把若干流帧拼成 SSE 文本(每帧 data: <json>\n\n,末尾 [DONE]),对齐 sseDecoder.ts 的线格式。
  const sse = (frames: object[]) =>
    frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n';

  // 进入页面时的接口固定为离线 mock,使硬门不依赖真后端(DashScope/RDS)。
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/kb/whoami', (r) =>
      r.fulfill({ contentType: 'application/json', body: JSON.stringify({
        user_id: 'e2e', display_name: 'E2E 测试', role: 'employee',
        can_manage_kb: false, acl_groups: ['marketing'], managed_owner_depts: [],
      }) }));
    await page.route('**/api/hot-questions*', (r) =>
      r.fulfill({ contentType: 'application/json', body: JSON.stringify({ questions: ['示例问题一', '示例问题二'] }) }));
    await page.route('**/api/conversations*', (r) =>
      r.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [] }) }));
  });

  test('流式双节点拆分：已定稿段落 DOM 不随后续吐字重建（Perf-5 硬门）', async ({ page }) => {
    // route.fulfill 是一次性交付（done 即刻定稿，观察不到流式窗口）→ 在页内包 fetch，
    // 用 ReadableStream 按 120ms 间隔逐帧下发，制造真实的多秒流式窗口。
    await page.addInitScript(() => {
      const orig = window.fetch.bind(window)
      ;(window as any).fetch = (input: any, init?: any) => {
        const url = typeof input === 'string' ? input : (input && input.url) || ''
        if (!String(url).includes('/api/ask/stream')) return orig(input, init)
        const frames = [
          JSON.stringify({ type: 'chunk', content: '第一段内容确立事实基础，先渲染并定稿。\n\n' }),
          ...Array.from({ length: 24 }, (_, i) => JSON.stringify({ type: 'chunk', content: `第二段持续输出第${i}词，` })),
          JSON.stringify({ type: 'done' }),
        ]
        const enc = new TextEncoder()
        const stream = new ReadableStream({
          start(c) {
            let i = 0
            const t = setInterval(() => {
              if (i >= frames.length) { clearInterval(t); c.enqueue(enc.encode('data: [DONE]\n\n')); c.close(); return }
              c.enqueue(enc.encode(`data: ${frames[i++]}\n\n`))
            }, 120)
          },
        })
        return Promise.resolve(new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
      }
    })
    await page.goto(CHAT_ROUTE)
    await page.getByTestId('chat-input').fill('请分两段回答')
    await page.getByTestId('chat-send').click()
    // 节点身份断言在【页内】同步采样（Playwright expect 轮询往返有百毫秒级延迟，三视口并行
    // 时容易滑过流式窗口、采到定稿帧的合法重建）：第一段进 stable 段时给 <p> 打标记，
    // 等尾段推进 ≥3 个采样帧后检查标记仍在同一节点上。
    const verdict = await page.evaluate(async () => {
      const deadline = Date.now() + 15000
      let marked: any = null
      let tailGrowth = 0
      let lastTail = -1
      while (Date.now() < deadline) {
        const segs = Array.from(document.querySelectorAll('.md [data-seg]'))
        if (segs.length >= 2) {
          const p = segs[0].querySelector('p') as any
          if (p && /第一段/.test(p.textContent || '')) {
            if (!marked) { p.__mark = 1; marked = p }
            const tailLen = segs[1].innerHTML.length
            if (tailLen > lastTail) { if (lastTail >= 0) tailGrowth++; lastTail = tailLen }
            const cur = segs[0].querySelector('p') as any
            const alive = !!(cur && cur.__mark) && marked.isConnected
            if (!alive) return { ok: false, why: 'stable 节点在流式期被重建', tailGrowth }
            if (tailGrowth >= 3) return { ok: true, why: '', tailGrowth }
          }
        }
        await new Promise((r) => setTimeout(r, 60))
      }
      return { ok: false, why: '未观察到双节点流式窗口（split 未生效或流过快）', tailGrowth }
    })
    expect(verdict.ok, `已定稿段落节点在尾段推进期间必须原封未动：${verdict.why}`).toBe(true)
    // 收尾定稿：回单节点权威渲染，全文完整、光标退场
    await expect(page.locator('.md').last()).toContainText('第23词', { timeout: 20000 })
    await expect(page.locator('.md.is-streaming')).toHaveCount(0, { timeout: 10000 })
    await expect(page.locator('.md').last()).toContainText('第一段内容确立事实基础')
  })

  test('流式输出过程中可停止', async ({ page }) => {
    // 慢响应:保持 asking=true 的窗口,期间发送按钮应切到「停止」(aria-label=停止)。
    await page.route('**/api/ask/stream', async (route) => {
      await new Promise((r) => setTimeout(r, 3000));
      await route.fulfill({ contentType: 'text/event-stream', body: sse([{ type: 'chunk', content: '…' }, { type: 'done' }]) });
    });
    await page.goto(CHAT_ROUTE);
    await page.getByTestId('chat-input').fill('总结这篇文档');
    await page.getByTestId('chat-send').click();
    await expect(
      page.getByRole('button', { name: /停止|stop|中止/i }),
      '流式输出中应可停止'
    ).toBeVisible();
  });

  test('生成失败有重试', async ({ page }) => {
    await page.route('**/api/ask/stream', (r) => r.fulfill({ status: 500, body: '{}' }));
    await page.goto(CHAT_ROUTE);
    // 问句避免含「重试」二字:否则它会成为侧栏会话标题,污染 /重试/ 的 getByRole 匹配(strict 多命中)。
    await page.getByTestId('chat-input').fill('触发一次失败');
    await page.getByTestId('chat-send').click();
    await expect(
      page.getByRole('button', { name: /重试|retry/i }),
      '生成失败后应提供重试入口'
    ).toBeVisible();
  });

  test('引用可点开', async ({ page }) => {
    // 固定返回一条带来源的回答 → SourceList 渲染「来源」chip(data-testid=citation)。
    await page.route('**/api/ask/stream', (route) => route.fulfill({
      contentType: 'text/event-stream',
      body: sse([
        { type: 'session', message_id: 'm1', session_id: 's1' },
        { type: 'sources', sources: [{ title: 'U8+ 操作手册', section: '登录', level: 'high', score: 8.1, relevance: 0.9, preview: '示例片段' }] },
        { type: 'chunk', content: '这是一段示例答案。' },
        { type: 'done' },
      ]),
    }));
    await page.goto(CHAT_ROUTE);
    await page.getByTestId('chat-input').fill('U8+ 怎么登录');
    await page.getByTestId('chat-send').click();
    const citation = page.getByTestId('citation').first();
    await expect(citation, '回答应带可交互的引用').toBeVisible();
    await expect(citation).toBeEnabled();
  });

  test('检索为空不是死胡同', async ({ page }) => {
    // 固定返回 no_result + 改写建议 → 无结果卡(未找到/试试这样问/转人工)即「前进路径」。
    await page.route('**/api/ask/stream', (route) => route.fulfill({
      contentType: 'text/event-stream',
      body: sse([
        { type: 'sources', sources: [] },
        { type: 'done', no_result: true, rephrase: ['U8+ 登录入口在哪', '如何重置 U8+ 密码'] },
      ]),
    }));
    await page.goto(CHAT_ROUTE);
    await page.getByTestId('chat-input').fill('一个肯定检索不到的问题zzz');
    await page.getByTestId('chat-send').click();
    // 落到本应用真实的无结果出路(原模板的「换个说法/扩大范围」文案本项目没有)。
    await assertHasWayForward(page, [
      page.getByText('未找到相关内容'),
      page.getByText('试试这样问'),
      page.getByRole('button', { name: /转人工/ }),
    ]);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// 审批中心（本轮 IA 重构）：三条审批流（Agent 高风险 / 上传入库 / 跨部门授权）收进单一
// 「审批」tab，待办/历史同址切换；文档管理回归纯台账。旧 tab=agent / tab=history 走别名。
// 进入方式与「AI 助手」组一致：?token= 透传登录 + mock /api/kb/whoami（不用 ?preview，
// 否则 authed 请求被合成 503、page.route 截不到）。ManageView 挂载会并发拉一串管理接口，
// 先注册 catch-all 空响应、再注册专属 mock（Playwright 后注册者优先）。
// ─────────────────────────────────────────────────────────────────────────────
test.describe('UX 硬门 — 审批中心', () => {
  const MANAGE_ROUTE = '/console/manage?token=e2e-fake-token';

  // expires_at 用相对时间：固定日历日期会腐坏成「已过期」——过期单禁处置上线后，
  // 「批准有二次确认」用例点的就是这条数据的批准按钮，硬编码过期日期会让它无预警变红。
  const fmtTs = (d: Date) => {
    const p = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  };
  const AREQ = (over: Record<string, unknown> = {}) => ({
    request_id: 'ap1', run_id: 'r1', call_id: 'c1', tool_name: 'u8_writeback', tool_version: '1.0',
    proposed_args: { qty: 120, item: 'PP 刀叉 8寸' }, args_digest: 'd',
    render_summary: 'u8_writeback(item, qty)', requested_by: 'user_wang', requested_dept: 'production',
    approver_scope: 'production', status: 'pending', expires_at: fmtTs(new Date(Date.now() + 3 * 86_400_000)),
    created_at: '2026-07-09 20:00:00', decided_at: null, ...over,
  });
  const ACCESS_REQ = (over: Record<string, unknown> = {}) => ({
    id: 'ar1', doc_id: 'D1', doc_title: '营销物料使用规范 v3', owner_dept: 'production',
    requester_dept: 'marketing', requester_name: '王伟', permission_level: 'dept_internal',
    reason: '包装设计需引用营销规范。', created_at: '2026-07-09', ...over,
  });
  const UPLOAD_REQ = (over: Record<string, unknown> = {}) => ({
    doc_id: 'P1', version_no: 2, title: '2026 客户验厂应答模板', original_filename: '验厂应答.docx',
    owner_dept: 'quality', permission_level: 'public', owner_name: '李娜', created_at: '2026-06-27', ...over,
  });

  interface MockOpts {
    role?: 'dept_admin' | 'kb_admin';
    agent?: object | number;          // /api/agent/approvals 响应体或状态码（404/403）
    access?: object[];                // 授权申请（dept_admin 职责）
    uploads?: object[];               // 上传审批（kb_admin 职责）
    history?: object[];               // 审批历史
    contribs?: object[];              // 待审核知识贡献（跳转 chip）
    tools?: object | number;          // /api/agent/tools 响应体或状态码；缺省 404（治理 tab 自隐，老用例零扰动）
    invocations?: object[];           // /api/agent/invocations（uncertain 对账队列）
  }
  function mockManage(page: import('@playwright/test').Page, o: MockOpts = {}) {
    // catch-all：ManageView 的其余 loaders 全部回空（避免 4xx 触发 console guard）
    return Promise.all([
      page.route('**/api/**', (r) => r.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [], questions: [], docs: [], total: 0 }),
      })),
      page.route('**/api/kb/whoami', (r) => r.fulfill({
        contentType: 'application/json', body: JSON.stringify({
          user_id: 'admin1', display_name: '生产部管理员', role: o.role ?? 'dept_admin',
          can_manage_kb: true, acl_groups: ['production'], managed_owner_depts: ['production'],
        }),
      })),
      page.route('**/api/agent/approvals*', (r) => (
        typeof o.agent === 'number'
          ? r.fulfill({ status: o.agent, contentType: 'application/json', body: JSON.stringify({ detail: 'Not Found' }) })
          : r.fulfill({ contentType: 'application/json', body: JSON.stringify(o.agent ?? { items: [] }) })
      )),
      page.route('**/api/kb/access-requests*', (r) =>
        r.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: o.access ?? [] }) })),
      page.route('**/api/kb/pending-approvals*', (r) =>
        r.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: o.uploads ?? [] }) })),
      page.route('**/api/kb/approval-history*', (r) =>
        r.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: o.history ?? [] }) })),
      page.route('**/api/kb/contributions/pending*', (r) =>
        r.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: o.contribs ?? [], has_more: false }) })),
      // Agent 治理（批次β）：专属 mock 堵住 catch-all 的形状错配（{items,questions,docs,total} 里
      // 没有 disabled/drift 键）。缺省 404 = flag 未开 → tab 自隐，既有用例零视觉扰动。
      page.route('**/api/agent/tools*', (r) => (
        typeof (o.tools ?? 404) === 'number'
          ? r.fulfill({ status: (o.tools ?? 404) as number, contentType: 'application/json', body: JSON.stringify({ detail: 'Not Found' }) })
          : r.fulfill({ contentType: 'application/json', body: JSON.stringify(o.tools) })
      )),
      page.route('**/api/agent/invocations*', (r) =>
        r.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: o.invocations ?? [] }) })),
    ]);
  }
  const approvalsTab = (page: import('@playwright/test').Page) =>
    page.locator('[aria-label="管理台分区"]').getByRole('tab', { name: /审批/ });

  test('聚合角标 = 授权 + Agent；待办面按风险降序（Agent 区块在授权之上），无横向滚动', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    await mockManage(page, {
      agent: { items: [AREQ(), AREQ({ request_id: 'ap2', run_id: 'r2', proposed_args: { qty: 40 } })] },
      access: [ACCESS_REQ()],
    });
    await page.goto(MANAGE_ROUTE);
    const tab = approvalsTab(page);
    await expect(tab, '审批 tab 常驻（不随 Agent 端点探测消失）').toBeVisible();
    await expect(tab, '角标 = 角色职责队列(授权1) + Agent(2)').toContainText('3');
    await tab.click();
    await expect(page.getByText('Agent 高风险操作审批')).toBeVisible();
    await expect(page.getByText('授权申请', { exact: true })).toBeVisible();
    // 风险降序：批准即执行的 Agent 区块必须排在常规授权之上
    const agentBox = await page.getByText('Agent 高风险操作审批').boundingBox();
    const accessBox = await page.getByText('授权申请', { exact: true }).boundingBox();
    expect(agentBox && accessBox && agentBox.y < accessBox.y, 'Agent 区块应在授权申请之上').toBeTruthy();
    await expect(page.getByText('qty=120')).toBeVisible();      // 脱敏后参数可核对
    await assertKeyActionsVisible([
      page.getByRole('button', { name: '批准' }).first(),
      page.getByRole('button', { name: '驳回' }).first(),
      page.getByRole('button', { name: '终止' }).first(),
    ]);
    await assertNoHorizontalScroll(page);
    guard.assertClean();
  });

  test('kb_admin 角标 = 上传 + Agent；授权申请按拍板不呈现、不计数', async ({ page }) => {
    await mockManage(page, {
      role: 'kb_admin',
      agent: { items: [AREQ()] },
      uploads: [UPLOAD_REQ(), UPLOAD_REQ({ doc_id: 'P2', version_no: 1 })],
      access: [ACCESS_REQ()],   // 后端兜底通道有数据 → console 仍不呈现（拍板 2026-07-04）
    });
    await page.goto(MANAGE_ROUTE);
    const tab = approvalsTab(page);
    await expect(tab).toContainText('3');                        // 2 上传 + 1 Agent，授权不计
    await tab.click();
    await expect(page.getByText('待审批队列')).toBeVisible();     // 上传审批区块
    await expect(page.getByText('授权申请', { exact: true })).toHaveCount(0);
  });

  test('职责分离：自己发起的申请 → 批准/驳回禁用、动作变「撤回」', async ({ page }) => {
    await mockManage(page, { agent: { items: [AREQ({ requested_by: 'admin1' })] } });   // == whoami user_id
    await page.goto(MANAGE_ROUTE);
    await approvalsTab(page).click();
    await expect(page.getByText('我发起的')).toBeVisible();
    await expect(page.getByRole('button', { name: '批准' })).toBeDisabled();
    await expect(page.getByRole('button', { name: '驳回' })).toBeDisabled();
    await expect(page.getByRole('button', { name: '撤回' })).toBeEnabled();
  });

  test('批准有二次确认，确认后 POST /api/agent/approve 且行移除 → 聚合空态', async ({ page }) => {
    await mockManage(page, { agent: { items: [AREQ()] } });
    const posts: string[] = [];
    await page.route('**/api/agent/approve', (r) => {
      posts.push(r.request().postData() || '');
      return r.fulfill({ contentType: 'text/event-stream', body: 'data: [DONE]\n\n' });
    });
    await page.goto(MANAGE_ROUTE);
    await approvalsTab(page).click();
    await page.getByRole('button', { name: '批准' }).click();
    const dlg = page.locator('[role="alertdialog"], [role="dialog"]');
    await expect(dlg, '批准是不可撤回的执行放行，必须有二次确认').toBeVisible();
    await dlg.getByRole('button', { name: /批准执行/ }).click();
    // 最后一件清空 → 全队列空 → 聚合空态（三条流共用，不再各自留空卡）
    await expect(page.getByTestId('approval-empty')).toBeVisible();
    await expect(page.getByText('当前没有待你处理的审批')).toBeVisible();
    expect(posts.length).toBe(1);
    const body = JSON.parse(posts[0]);
    expect(body.run_id).toBe('r1');
    expect(body.outcome.kind).toBe('approved');
    expect(body.idempotency_key).toBe('ap1:approved');
  });

  test('批次α-③：上传审批「通过」有二次确认——确认前零请求，确认后 POST /api/kb/approve 且队列清空', async ({ page }) => {
    await mockManage(page, { role: 'kb_admin', uploads: [UPLOAD_REQ()], agent: 404 });
    const posts: string[] = [];
    await page.route('**/api/kb/approve', (r) => {
      posts.push(r.request().postData() || '');
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true }) });
    });
    await page.goto(MANAGE_ROUTE);
    await approvalsTab(page).click();
    await expect(page.getByText('待审批队列')).toBeVisible();
    await page.getByRole('button', { name: '通过', exact: true }).click();
    const dlg = page.locator('[role="alertdialog"], [role="dialog"]');
    await expect(dlg, '放行入库与退役/Agent 批准同级，必须有二次确认').toBeVisible();
    expect(posts.length, '确认前不得发请求（取消=零副作用）').toBe(0);
    await dlg.getByRole('button', { name: /通过并放行/ }).click();
    await expect(page.getByTestId('approval-empty')).toBeVisible();
    expect(posts.length).toBe(1);
  });

  test('批次α-⑦：文档管理筛选键不残留到审批深链；切回 docs 筛选（store 态）仍在', async ({ page }) => {
    await mockManage(page, {});
    await page.goto(MANAGE_ROUTE);
    const zone = page.locator('[aria-label="管理台分区"]');
    await zone.getByRole('tab', { name: /文档管理/ }).click();
    await page.getByPlaceholder('搜索文档名…').fill('营销规范');
    await expect(page, 'DocTable 防抖后把 q 写回 URL（既有机制）').toHaveURL(/[?&]q=/);
    await approvalsTab(page).click();
    await expect(page).toHaveURL(/tab=approvals/);
    await expect(page, '审批深链不携带文档筛选词（白名单摘除）').not.toHaveURL(/[?&]q=/);
    await zone.getByRole('tab', { name: /文档管理/ }).click();
    await expect(page.getByPlaceholder('搜索文档名…'), '切回 docs：筛选靠 useKb 模块态保持').toHaveValue('营销规范');
  });

  test('批次β：Agent 治理 tab（kb_admin+flag 双门）——停用走必填理由确认，POST toggle 后状态翻转', async ({ page }) => {
    const TOOLS = {
      items: [{ tool_name: 'u8_writeback', version: '1.0', risk_level: 'high_write', permission_scope: 'approval', owner_team: 'erp', status: 'active', registered_by: 'system', created_at: '2026-07-05 09:00' }],
      disabled: [], drift: ['spec 漂移（代码 ≠ DB）: legacy@0.9'],
    };
    await mockManage(page, { role: 'kb_admin', agent: 404, tools: TOOLS });
    const posts: string[] = [];
    await page.route('**/api/agent/tools/toggle', (r) => {
      posts.push(r.request().postData() || '');
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ tool_name: 'u8_writeback', status: 'disabled', rows: 1 }) });
    });
    await page.goto(MANAGE_ROUTE);
    const zone = page.locator('[aria-label="管理台分区"]');
    const tab = zone.getByRole('tab', { name: /Agent 治理/ });
    await expect(tab, 'kb_admin + tools 端点 200 → 治理 tab 出现').toBeVisible();
    await tab.click();
    await expect(page.getByTestId('agent-gov-drift'), '漂移告警条渲染').toContainText('legacy@0.9');
    await page.getByTestId('tool-toggle-u8_writeback').click();
    const dlg = page.locator('[role="alertdialog"], [role="dialog"]');
    await expect(dlg, '全局 kill switch 必须有理由确认').toBeVisible();
    expect(posts.length, '确认前不得发请求').toBe(0);
    await dlg.getByRole('textbox').fill('误触发风险，先全局停用');
    await dlg.getByRole('button', { name: '停用' }).click();
    await expect(page.getByTestId('tool-toggle-u8_writeback'), '成功后本地翻转为可恢复').toHaveText(/恢复/);
    expect(posts.length).toBe(1);
    expect(JSON.parse(posts[0])).toEqual({ tool_name: 'u8_writeback', disabled: true, reason: '误触发风险，先全局停用' });
  });

  test('批次β：flag 未开（tools 404）→ 治理 tab 自隐；kb_admin 深链 ?tab=agent_gov 落诚实提示页', async ({ page }) => {
    await mockManage(page, { role: 'kb_admin', agent: 404 });   // tools 缺省 404
    await page.goto(MANAGE_ROUTE);
    const zone = page.locator('[aria-label="管理台分区"]');
    await expect(zone.getByRole('tab', { name: /审批/ })).toBeVisible();
    await expect(zone.getByRole('tab', { name: /Agent 治理/ }), 'flag 未开不摆死 tab').toHaveCount(0);
    await page.goto('/console/manage?token=e2e-fake-token&tab=agent_gov');
    await expect(page.getByText('Agent 治理未开启或无权访问'), '深链兜底=诚实提示，不是空态假象').toBeVisible();
  });

  test('批次β：uncertain 对账——核实依据必填，回填成功后行移除并 POST resolve', async ({ page }) => {
    const INV = {
      invocation_id: 'inv1', run_id: 'run_abc123', step_no: 3, tool_name: 'u8_writeback', status: 'uncertain',
      policy_decision: 'require_approval', approval_request_id: null, idempotency_key: 'k1', args_digest: 'a1b2c3d4',
      error_text: 'stale executing（进程崩溃/超时僵尸，900s 无收尾）', started_at: '2026-07-13 16:02', ended_at: null,
    };
    await mockManage(page, { role: 'kb_admin', agent: 404, tools: { items: [], disabled: [], drift: [] }, invocations: [INV] });
    const posts: string[] = [];
    await page.route('**/api/agent/invocations/resolve', (r) => {
      posts.push(r.request().postData() || '');
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ invocation_id: 'inv1', status: 'succeeded' }) });
    });
    await page.goto(MANAGE_ROUTE);
    await page.locator('[aria-label="管理台分区"]').getByRole('tab', { name: /Agent 治理/ }).click();
    await expect(page.getByText('stale executing（进程崩溃/超时僵尸，900s 无收尾）')).toBeVisible();
    await page.getByRole('button', { name: '核实为成功' }).click();
    const dlg = page.locator('[role="alertdialog"], [role="dialog"]');
    await expect(dlg, '对账必须采集核实依据').toBeVisible();
    expect(posts.length, '确认前不得发请求').toBe(0);
    await dlg.getByRole('textbox').fill('已到 U8 核实单据 20260713-001 已落库');
    await dlg.getByRole('button', { name: /回填已成功/ }).click();
    await expect(page.getByText('当前没有待对账的调用', { exact: false }), '回填后行移除→显式空态').toBeVisible();
    expect(posts.length).toBe(1);
    const body = JSON.parse(posts[0]);
    expect(body).toEqual({ invocation_id: 'inv1', resolution: 'confirmed_succeeded', note: '已到 U8 核实单据 20260713-001 已落库' });
  });

  test('RAG_AGENT_ENABLE 未开（端点 404）→ Agent 区块不出现，审批 tab 常驻且角标只数 kb 队列', async ({ page }) => {
    await mockManage(page, { agent: 404, access: [ACCESS_REQ()] });
    await page.goto(MANAGE_ROUTE);
    const tab = approvalsTab(page);
    await expect(tab).toBeVisible();
    await expect(tab).toContainText('1');                        // 仅授权申请
    await tab.click();
    await expect(page.getByText('Agent 高风险操作审批')).toHaveCount(0);   // 功能未开不造噪声
    await expect(page.getByText('授权申请', { exact: true })).toBeVisible();
  });

  test('旧深链别名：?tab=agent → 审批待办面；?tab=history → 审批历史面', async ({ page }) => {
    await mockManage(page, {
      agent: { items: [AREQ()] },
      history: [
        { kind: 'access', action: 'approved', title: '营销物料使用规范 v3', subject: '王伟',
          owner_dept: 'production', decided_by_name: '生产部管理员', decided_at: '2026-07-08 10:00:00', detail: '', extra: '' },
        // agent 类（后端五类扩展）：终止决策 → 时间线要能渲染类型/动作/发起人
        { kind: 'agent', action: 'rejected_terminate', title: 'u8_writeback', subject: '王伟',
          owner_dept: 'production', decided_by_name: '生产部管理员', decided_at: '2026-07-07 09:00:00', detail: '', extra: '' },
      ],
    });
    await page.goto(`${MANAGE_ROUTE}&tab=agent`);
    await expect(approvalsTab(page)).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByText('Agent 高风险操作审批')).toBeVisible();

    await page.goto(`${MANAGE_ROUTE}&tab=history`);
    await expect(approvalsTab(page)).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByTestId('approval-history')).toBeVisible();
    await expect(page.getByText('营销物料使用规范 v3')).toBeVisible();   // 时间线有内容，不是空壳
    await expect(page.getByText('u8_writeback')).toBeVisible();          // agent 决策同列时间线
    await expect(page.getByText('发起人 王伟')).toBeVisible();
    await expect(page.getByRole('tab', { name: 'Agent 操作' })).toBeVisible();   // 类型筛选 chip
  });

  test('全空 → 聚合空态可见 + 知识贡献跳转 chip 带计数直达贡献页', async ({ page }) => {
    await mockManage(page, { contribs: [{ contribution_id: 'c1' }, { contribution_id: 'c2' }] });
    await page.goto(MANAGE_ROUTE);
    await approvalsTab(page).click();
    await expect(page.getByTestId('approval-empty')).toBeVisible();
    const chip = page.getByTestId('approval-contrib-link');
    await expect(chip).toContainText('2');
    await chip.getByRole('link', { name: '去处理' }).click();
    await expect(page).toHaveURL(/\/contribute/);
  });

  test('文档管理回归纯台账：无队列区块，台账表头首屏可见（无需滚动）', async ({ page }) => {
    // 队列非空（审计基线里正是该状态把台账推出首屏 110~283px）——现在队列在审批 tab，不影响台账
    await mockManage(page, { agent: { items: [AREQ()] }, access: [ACCESS_REQ()] });
    await page.goto(`${MANAGE_ROUTE}&tab=docs`);
    await expect(page.getByText('文档台账')).toBeVisible();
    await expect(page.getByText('待审批队列')).toHaveCount(0);
    await expect(page.getByText('Agent 高风险操作审批')).toHaveCount(0);
    const head = page.locator('.led-head');
    await expect(head).toBeVisible();
    const box = await head.boundingBox();
    const vp = page.viewportSize();
    expect(box && vp && box.y >= 0 && box.y + box.height <= vp.height,
      `台账表头须在首屏内（y=${box?.y}, 视口高=${vp?.height}）`).toBeTruthy();
  });

  test('桌面 ?token 刷新不再死胡同：token tab 级续存，reload 后自动重登且 URL 仍无 token', async ({ page }) => {
    await mockManage(page, { access: [ACCESS_REQ()] });
    await page.goto(MANAGE_ROUTE);
    await expect(page.getByRole('tab', { name: /概览看板/ })).toBeVisible();
    expect(page.url(), '#F-console-urltoken：token 抹除不回退').not.toContain('token=');
    await page.reload();
    await expect(page.getByRole('tab', { name: /概览看板/ }),
      '刷新后应用 sessionStorage 续存 token 自动重登，而非「未能完成免登」死胡同').toBeVisible();
    await expect(page.getByText('未能完成免登')).toHaveCount(0);
    expect(page.url()).not.toContain('token=');
  });

  test('切子 tab 不整树重挂载：/api/kb/my-docs 全程只请求一次', async ({ page }) => {
    await mockManage(page, { access: [ACCESS_REQ()] });
    let myDocsHits = 0;
    await page.route('**/api/kb/my-docs*', (r) => {
      myDocsHits += 1;
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) });
    });
    await page.goto(MANAGE_ROUTE);
    await expect(page.getByRole('tab', { name: /概览看板/ })).toBeVisible();
    await page.getByRole('tab', { name: /文档管理/ }).click();
    await expect(page.getByText('文档台账')).toBeVisible();
    await approvalsTab(page).click();
    await expect(page.getByText('授权申请', { exact: true })).toBeVisible();
    await page.getByRole('tab', { name: /文档管理/ }).click();
    await expect(page.getByText('文档台账')).toBeVisible();
    expect(myDocsHits, '子 tab 切换曾整树重挂载 ManageView、每次重发 9~12 个请求（审计问题2）').toBe(1);
  });
});
