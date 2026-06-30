import { expect, Page, Locator } from '@playwright/test';

/**
 * UX 硬指标断言库。每个函数对应 dispatcher「交互与流程(UX)」里的一条硬指标。
 * 这些是二值、可复现的断言——发现靠 agent 走查,验证靠这里。
 *
 * 用法:在 spec 里 import 后调用。带 TODO 的地方填你的应用专属选择器/路由。
 *
 * 注:本项目前端是 reka-ui + Tailwind(非 Element Plus),所以确认框/弹层默认
 *     选择器用无障碍 role([role="alertdialog"]/[role="dialog"])而非 .el-*。
 */

// ── 控制台与网络干净 ───────────────────────────────────────────────
// 在 test 开始时挂上,结束时断言为空。允许通过 allowlist 忽略已知噪音。
export function attachConsoleGuard(page: Page, allow: RegExp[] = []) {
  const errors: string[] = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !allow.some((re) => re.test(msg.text()))) {
      errors.push(`console.error: ${msg.text()}`);
    }
  });
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  page.on('response', (res) => {
    // 非预期失败请求(放行你已知会 4xx 的探针接口)
    if (res.status() >= 400 && !allow.some((re) => re.test(res.url()))) {
      errors.push(`failed request ${res.status()}: ${res.url()}`);
    }
  });
  return {
    assertClean: () => expect(errors, `控制台/网络应无错误,实际:\n${errors.join('\n')}`).toHaveLength(0),
  };
}

// ── 无整页横向滚动(≥1280 必须满足)────────────────────────────────
export async function assertNoHorizontalScroll(page: Page) {
  const overflow = await page.evaluate(() => {
    const el = document.documentElement;
    return el.scrollWidth - el.clientWidth;
  });
  expect(overflow, `出现整页横向滚动,溢出 ${overflow}px`).toBeLessThanOrEqual(1); // 留 1px 容差
}

// ── 关键操作在视口内可见(1024 下尤其要查)──────────────────────────
export async function assertKeyActionsVisible(actions: Locator[]) {
  for (const a of actions) {
    await expect(a, '关键操作应可见且在视口内').toBeInViewport();
  }
}

// ── 任务路径不出现死胡同 ──────────────────────────────────────────
// 进入某状态(空/错误/无权限)后,页面必须至少有一个可达的「前进或返回」操作。
export async function assertHasWayForward(page: Page, candidates: Locator[]) {
  let found = false;
  for (const c of candidates) {
    if ((await c.count()) > 0 && (await c.first().isVisible()) && (await c.first().isEnabled())) {
      found = true;
      break;
    }
  }
  expect(found, '当前状态下没有任何可达的前进/返回操作(死胡同)').toBeTruthy();
}

// ── 返回/取消不丢失已填数据 ────────────────────────────────────────
// 填一个字段 → 触发离开再返回(或取消)→ 字段值应保留。
export async function assertBackPreservesData(opts: {
  field: Locator;
  value: string;
  leaveAndReturn: () => Promise<void>; // TODO: 你的离开+返回动作
}) {
  await opts.field.fill(opts.value);
  await opts.leaveAndReturn();
  await expect(opts.field, '返回后已填内容应保留,实际被清空').toHaveValue(opts.value);
}

// ── 破坏性操作必须二次确认且可取消 ────────────────────────────────
// 点删除 → 出现确认框(reka-ui AlertDialog → role="alertdialog")→ 取消后该项仍在。
export async function assertDestructiveConfirmed(opts: {
  page: Page;
  trigger: Locator; // 删除/移除按钮
  rowStillThere: Locator; // 被操作对象,取消后应仍存在
  confirmDialog?: Locator; // 默认 reka-ui 弹层(role=alertdialog/dialog)
  cancelButton?: Locator;
}) {
  const dialog = opts.confirmDialog ?? opts.page.locator('[role="alertdialog"], [role="dialog"]');
  const cancel = opts.cancelButton ?? dialog.getByRole('button', { name: /取消|cancel/i });

  await opts.trigger.click();
  await expect(dialog, '破坏性操作应弹出二次确认').toBeVisible();
  await cancel.click();
  await expect(opts.rowStillThere, '取消后对象不应被删除').toBeVisible();
}

// ── 异步操作有反馈且防重复提交 ────────────────────────────────────
// 点提交 → 出现 loading 且按钮被禁用(防双击重复提交)。
export async function assertAsyncFeedbackAndNoDoubleSubmit(opts: {
  submit: Locator;
  loadingIndicator: Locator; // TODO: 你的 loading(如 [aria-busy="true"] 或按钮 loading 态)
}) {
  await opts.submit.click();
  await expect(opts.loadingIndicator, '异步操作应有 loading 反馈').toBeVisible();
  await expect(opts.submit, '提交中按钮应禁用以防重复提交').toBeDisabled();
}

// ── 错误态有可达恢复路径 ──────────────────────────────────────────
export async function assertErrorHasRecovery(opts: {
  errorRegion: Locator;
  recoveryAction: Locator; // 重试/返回/换条件
}) {
  await expect(opts.errorRegion, '应展示错误态').toBeVisible();
  await expect(opts.recoveryAction, '错误态应提供可达的恢复操作').toBeEnabled();
}
