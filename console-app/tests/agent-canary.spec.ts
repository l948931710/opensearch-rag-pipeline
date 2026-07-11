import { test, expect } from '@playwright/test';
import { attachConsoleGuard } from './ux-gate.helpers';

// ─────────────────────────────────────────────────────────────────────────────
// Agent 用户侧 canary（P0-F）：开关可见性（能力探测，绝不留死入口）+ 挂起卡渲染。
// 进入方式与「AI 助手」组一致：?token= 透传登录 + mock /api/kb/whoami（不用 ?preview，
// 否则 authed 请求被合成 503、page.route 截不到）。/api/agent/* 全 route mock，无需真后端。
// ─────────────────────────────────────────────────────────────────────────────
test.describe('Agent canary — 开关可见性与挂起卡', () => {
  const CHAT_ROUTE = '/console/?token=e2e-fake-token';

  const sse = (frames: object[]) =>
    frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('') + 'data: [DONE]\n\n';

  test.beforeEach(async ({ page }) => {
    await page.route('**/api/kb/whoami', (r) =>
      r.fulfill({ contentType: 'application/json', body: JSON.stringify({
        user_id: 'e2e', display_name: 'E2E 测试', role: 'employee',
        can_manage_kb: false, acl_groups: ['marketing'], managed_owner_depts: [],
      }) }));
    await page.route('**/api/hot-questions*', (r) =>
      r.fulfill({ contentType: 'application/json', body: JSON.stringify({ questions: ['示例问题一'] }) }));
    await page.route('**/api/conversations*', (r) =>
      r.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [] }) }));
  });

  test('RAG_AGENT_ENABLE 未开（探测 404）→ 「Agent 模式」开关与「运行」入口都不渲染（死入口不留）', async ({ page }) => {
    // 探测 404 是本用例的【预期信号】：放行该请求 + 浏览器对它的 "Failed to load resource" 控制台噪声
    const guard = attachConsoleGuard(page, [/\/api\/agent\/runs/, /Failed to load resource.*404/]);
    await page.route('**/api/agent/runs*', (r) =>
      r.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Not Found' }) }));
    await page.goto(CHAT_ROUTE);
    await expect(page.getByText('示例问题一')).toBeVisible();          // 页面就绪（热门问题已渲染）
    await expect(page.getByTestId('agent-toggle')).toHaveCount(0);
    await expect(page.getByTestId('agent-runs-entry')).toHaveCount(0);
    // 深度思考开关不受影响（旧路径完好）
    await expect(page.getByRole('button', { name: /深度思考/ })).toBeVisible();
    guard.assertClean();
  });

  test('探测 200 → 开关出现；开启后提问走 /api/agent/ask，approval 帧渲染挂起卡（工具/参数/审批单/撤回）', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    await page.route('**/api/agent/runs?*', (r) =>
      r.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [] }) }));
    // 断线恢复轮询：挂起 run 的详情（保持 suspended，挂起卡常驻）
    await page.route('**/api/agent/runs/r1*', (r) =>
      r.fulfill({ contentType: 'application/json', body: JSON.stringify({
        run: { run_id: 'r1', status: 'suspended', user_id: 'e2e', started_at: '2026-07-11 09:00:00', ended_at: null },
        steps: [], invocations: [],
        approval: { request_id: 'ap9', call_id: 'c9', tool_name: 'u8_writeback', status: 'pending', approver_scope: 'production', render_summary: null, proposed_args: { qty: 120 }, expires_at: '2026-07-14 09:00:00', created_at: '2026-07-11 09:00:00', decided_at: null },
      }) }));
    let agentAskHits = 0;
    await page.route('**/api/agent/ask', (r) => {
      agentAskHits += 1;
      return r.fulfill({ contentType: 'text/event-stream', body: sse([
        { type: 'session', session_id: 's1', message_id: 'm1', run_id: 'r1' },
        { type: 'chunk', content: '需要把数量写回 U8，先提交执行申请。' },
        { type: 'tool_call', call_id: 'c9', tool_name: 'u8_writeback', arguments: { qty: 120 } },
        { type: 'approval', approval_request_id: 'ap9', checkpoint_id: 'ck1', pending_call: { call_id: 'c9', tool_name: 'u8_writeback', arguments: { qty: 120 } } },
      ]) });
    });

    await page.goto(CHAT_ROUTE);
    const toggle = page.getByTestId('agent-toggle');
    await expect(toggle, '能力探测通过 → 开关渲染').toBeVisible();
    await expect(toggle).toHaveAttribute('aria-pressed', 'false');    // 默认关=旧路径
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-pressed', 'true');

    await page.getByTestId('chat-input').fill('把库存数量写回 U8');
    await page.getByTestId('chat-send').click();

    // 挂起卡：工具名 + 脱敏参数 + 审批单号 + 等待处置 + 撤回；过程 chip 收敛为「等待审批」
    const card = page.getByTestId('agent-suspended-card');
    await expect(card, 'approval 帧应渲染挂起卡').toBeVisible();
    await expect(card).toContainText('需要审批才能继续');
    await expect(card).toContainText('u8_writeback');
    await expect(card).toContainText('qty=120');
    await expect(card).toContainText('审批单 ap9');
    await expect(card.getByRole('button', { name: '撤回申请' })).toBeEnabled();
    await expect(page.getByTestId('agent-tool-chip')).toContainText('等待审批');
    // 阶段化状态条：真实事件序列（已提交→…→等待审批）
    await expect(page.getByTestId('agent-stages')).toContainText('已提交');
    await expect(page.getByTestId('agent-stages')).toContainText('等待审批');
    expect(agentAskHits, '开关开着 → 提问必须走 agent transport').toBe(1);

    // kill switch 本地持久：刷新后（sessionStorage 续存 token 自动重登）开关仍为开
    await page.reload();
    await expect(page.getByTestId('agent-toggle')).toHaveAttribute('aria-pressed', 'true');
    guard.assertClean();
  });

  test('运行中心入口：开关可用即有「运行」按钮，点开抽屉列出我的 runs 与状态', async ({ page }) => {
    await page.route('**/api/agent/runs?*', (r) =>
      r.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [
        { run_id: 'run_a', status: 'suspended', thread_id: 't', conversation_id: 'c', agent_profile: 'default', turns_used: 2, tool_calls_used: 1, tokens_used: 1800, started_at: '2026-07-11 09:30:00', ended_at: null },
        { run_id: 'run_b', status: 'succeeded', thread_id: 't', conversation_id: 'c', agent_profile: 'default', turns_used: 3, tool_calls_used: 2, tokens_used: 4200, started_at: '2026-07-10 16:00:00', ended_at: '2026-07-10 16:01:00' },
      ] }) }));
    await page.goto(CHAT_ROUTE);
    await page.getByTestId('agent-runs-entry').click();
    const center = page.getByTestId('agent-run-center');
    await expect(center).toBeVisible();
    const rows = page.getByTestId('agent-run-row');
    await expect(rows).toHaveCount(2);
    await expect(rows.first()).toContainText('等待审批');
    await expect(rows.nth(1)).toContainText('已完成');
  });
});
