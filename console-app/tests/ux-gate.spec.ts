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

test.describe('UX 硬门 — 目标页面', () => {
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
