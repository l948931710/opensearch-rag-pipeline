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
    // 固定返回 no_result + 改写建议 → 无结果卡(未找到/试试这样问)即「前进路径」（转人工已下线）。
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
    ]);
  });
});
// ─────────────────────────────────────────────────────────────────────────────
// 运营指标（批次γ，摘自分支「审批中心」组并适配 main 的 tab 结构——审批中心 IA 本身
// 随 ontology-p0 大合并）。进入方式与「AI 助手」组一致：?token= 透传登录 + mock
// /api/kb/whoami（不用 ?preview，否则 authed 请求被合成 503、page.route 截不到）。
// ManageView 挂载会并发拉一串管理接口，先注册 catch-all 空响应、再注册专属 mock
// （Playwright 后注册者优先）。
// ─────────────────────────────────────────────────────────────────────────────
test.describe('UX 硬门 — 运营指标（批次γ）', () => {
  const MANAGE_ROUTE = '/console/manage?token=e2e-fake-token';

  const ACCESS_REQ = (over: Record<string, unknown> = {}) => ({
    id: 'ar1', doc_id: 'D1', doc_title: '营销物料使用规范 v3', owner_dept: 'production',
    requester_dept: 'marketing', requester_name: '王伟', permission_level: 'dept_internal',
    reason: '包装设计需引用营销规范。', created_at: '2026-07-09', ...over,
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
    ops?: object;                     // /api/kb/ops-metrics（运营指标）；缺省三块 available=false（老用例零扰动）
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
      // 运营指标（批次γ）：专属 mock 堵 catch-all 形状错配（缺 *_available 键）。缺省三块不可用。
      page.route('**/api/kb/ops-metrics*', (r) =>
        r.fulfill({ contentType: 'application/json', body: JSON.stringify(o.ops ?? {
          window_days: 30, llm_available: false, llm_total_calls: 0, llm_error_calls: 0,
          llm_tokens_prompt: 0, llm_tokens_completion: 0, llm_cost_estimate: null,
          llm_p50_latency_ms: 0, llm_p95_latency_ms: 0,
          llm_by_model: [], llm_by_category: [], llm_by_dept: [], llm_daily: [],
          slo_available: false, slo_daily: [], slo_breach_days: 0,
          admission_available: false, admission_daily: [], admission_reasons: [],
        }) })),
    ]);
  }

  test('批次γ：运营指标 tab（kb_admin）——三分区渲染、口径脚注、SLO 停摆哨兵、准入健康语义', async ({ page }) => {
    const today = new Date().toISOString().slice(0, 10);
    const OPS = {
      window_days: 30, llm_available: true, llm_total_calls: 2841, llm_error_calls: 31,
      llm_tokens_prompt: 3412000, llm_tokens_completion: 861000, llm_cost_estimate: null,
      llm_p50_latency_ms: 1180, llm_p95_latency_ms: 6400,
      llm_by_model: [{ model: 'qwen3.7-plus', calls: 2210, error_calls: 24, tokens_prompt: 2900000, tokens_completion: 720000, avg_latency_ms: 1350 }],
      llm_by_category: [{ key: 'default', calls: 2300, tokens_total: 3400000 }],
      llm_by_dept: [{ key: 'marketing', calls: 1030, tokens_total: 1500000 }],
      llm_daily: [{ d: today, calls: 411, tokens_total: 683000 }],
      slo_available: true,
      slo_daily: [
        { d: '2026-07-01', total: 121, answer_rate: 0.78, no_result_rate: 0.14, error_rate: 0.06, p95_latency_ms: 68000, distinct_users: 44, slo_ok: false, breaches: ['answer_rate_min'], rejected_count: 12 },
      ],   // 最新一天远早于今天 → 停摆哨兵必亮
      slo_breach_days: 1,
      admission_available: true,
      admission_daily: [{ d: today, admitted: 96, rejected: 0 }],
      admission_reasons: [],
    };
    await mockManage(page, { role: 'kb_admin', agent: 404, ops: OPS });
    await page.goto(MANAGE_ROUTE);
    const zone = page.locator('[aria-label="管理台分区"]');
    const tab = zone.getByRole('tab', { name: /运营指标/ });
    await expect(tab, 'kb_admin 可见运营指标 tab').toBeVisible();
    await tab.click();
    await expect(page.getByTestId('ops-llm')).toContainText('价表未配');        // cost NULL 诚实空态
    await expect(page.getByTestId('ops-llm')).toContainText('qwen3.7-plus');
    await expect(page.getByTestId('ops-slo-stale'), 'rollup 停摆哨兵').toContainText('qa_rollup');
    await expect(page.getByTestId('ops-slo-breaches')).toContainText('answer_rate_min');
    await expect(page.getByTestId('ops-admission')).toContainText('限流未触发，健康');
    await expect(page.getByText('非同一口径'), 'governance↔SLO 口径脚注').toBeVisible();
  });

  test('批次γ：非 kb_admin 深链 kb_admin 专属 tab（ops/members）→ 落回概览看板（resolveTab 名单门）', async ({ page }) => {
    await mockManage(page, {});   // 默认 dept_admin
    for (const t of ['ops', 'members']) {
      await page.goto(`/console/manage?token=e2e-fake-token&tab=${t}`);
      const zone = page.locator('[aria-label="管理台分区"]');
      await expect(zone.getByRole('tab', { name: /概览看板/ }), `?tab=${t} 被拒 → 默认 dash`).toBeVisible();
      await expect(zone.getByRole('tab', { name: /运营指标|成员管理/ })).toHaveCount(0);
      await expect(page.getByTestId('ops-llm')).toHaveCount(0);
      // dash 真渲染了内容（非空白死页）：两种看板（全库/本部门）标题都含「资产概览」
      await expect(page.getByText(/资产概览/).first()).toBeVisible();
    }
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

});
