// 小程序纯函数单测（node 内置 test runner，无须 IDE / 无依赖）：
//   node --test fuling-rag-miniapp/tests/
//
// 覆盖全客户端最易回归的两块：utils/markdown.js（步骤判定/加粗 runs/列表/切片）
// 与 utils/typewriter.js（揭示推进/finishNow/cancel）。utils 是 ESM .js，而
// 小程序 package.json 无 "type":"module"（不动生产清单）—— 拷成 .mjs 后动态导入。

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, copyFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const tmp = mkdtempSync(join(tmpdir(), 'flmini-'));
copyFileSync(join(here, '../utils/markdown.js'), join(tmp, 'markdown.mjs'));
copyFileSync(join(here, '../utils/typewriter.js'), join(tmp, 'typewriter.mjs'));
const { parseTextBlock, plainText, plainLength, sliceParas } =
  await import(pathToFileURL(join(tmp, 'markdown.mjs')));
const { createTypewriter } = await import(pathToFileURL(join(tmp, 'typewriter.mjs')));

// ── markdown.parseTextBlock ──────────────────────────────────

test('加粗解析为 runs，揭示流里绝无 ** 字面量', () => {
  const paras = parseTextBlock('请按**第 1 步**操作');
  assert.equal(paras.length, 1);
  const runs = paras[0].runs;
  assert.deepEqual(runs.map((r) => r.text), ['请按', '第 1 步', '操作']);
  assert.deepEqual(runs.map((r) => r.bold), [false, true, false]);
  assert.ok(!plainText(paras).includes('**'));
});

test('步骤判定：第N步/①/编号加粗列表 命中；裸加粗开头不误判', () => {
  assert.equal(parseTextBlock('第 1 步 打开登录窗口')[0].step, true);
  assert.equal(parseTextBlock('第1步打开')[0].step, true);          // 未空格
  assert.equal(parseTextBlock('**第 2 步** 选择账套')[0].step, true); // ** 前缀
  assert.equal(parseTextBlock('①打开桌面图标')[0].step, true);       // 圆数字
  assert.equal(parseTextBlock('1. **登录** 系统')[0].step, true);    // 存量编号加粗
  assert.equal(parseTextBlock('2、**找到**菜单')[0].step, true);     // 顿号变体
  // 关键反例：任何加粗开头的段落（如 **注意**）不得挂步骤标尺
  assert.equal(parseTextBlock('**注意** 这很重要')[0].step, false);
  assert.equal(parseTextBlock('普通段落而已')[0].step, false);
});

test('步骤段第一个加粗 run 标 brand 色；非步骤段不标', () => {
  const step = parseTextBlock('**第 3 步** 输入**密码**')[0];
  assert.equal(step.runs[0].brand, true);   // 第 3 步
  assert.equal(step.runs.filter((r) => r.brand).length, 1); // 仅第一个
  const plain = parseTextBlock('这里有**加粗**词')[0];
  assert.ok(plain.runs.every((r) => !r.brand));
});

test('行首列表星号/短横转「· 」，不误伤行首加粗', () => {
  assert.equal(plainText(parseTextBlock('* 项目一')), '· 项目一');
  assert.equal(plainText(parseTextBlock('- 项目二')), '· 项目二');
  assert.equal(plainText(parseTextBlock('**加粗开头**的段落')), '加粗开头的段落');
});

test('# 标题降级为 heading 段；未闭合 ** 保持字面量', () => {
  const h = parseTextBlock('## 操作前提')[0];
  assert.equal(h.heading, true);
  assert.equal(plainText([h]), '操作前提');
  assert.equal(plainText(parseTextBlock('看**这里')), '看**这里'); // 不吞不崩
});

test('\\n\\n 分段：引言+第1步 同块时步骤样式不被吞', () => {
  const paras = parseTextBlock('登录步骤如下：\n\n**第 1 步** 双击图标');
  assert.equal(paras.length, 2);
  assert.equal(paras[0].step, false);
  assert.equal(paras[1].step, true);
});

test('空/null 输入返回空数组', () => {
  assert.deepEqual(parseTextBlock(''), []);
  assert.deepEqual(parseTextBlock(null), []);
});

// ── markdown.sliceParas（打字机揭示切片） ────────────────────

test('切片落在加粗 run 中间：样式保留、字符数精确', () => {
  const paras = parseTextBlock('请按**第 1 步**操作');
  // '请按' = 2 字符，再揭示 3 个落进 '第 1 步' 中间
  const cut = sliceParas(paras, 5);
  const runs = cut[0].runs;
  assert.equal(runs[runs.length - 1].bold, true);     // 半截 run 仍是加粗
  assert.equal(plainText(cut).length, 5);             // 揭示字符数精确
  assert.equal(plainText(cut), '请按第 1');
});

test('跨段切片：前段完整、后段部分、后续段不出现', () => {
  const paras = parseTextBlock('第一段落\n\n第二段落\n\n第三段落');
  const cut = sliceParas(paras, 6);  // 4 + 2
  assert.equal(cut.length, 2);
  assert.equal(plainText([cut[0]]), '第一段落');
  assert.equal(plainText([cut[1]]), '第二');
});

test('plainLength 与 plainText 长度一致（typewriter 段长契约）', () => {
  const paras = parseTextBlock('引言\n\n**第 1 步** 操作**要点**\n\n* 列表项');
  assert.equal(plainLength(paras), plainText(paras).length);
});

// ── typewriter ───────────────────────────────────────────────

test('自然走完：最终揭示等于全文，onDone 触发一次', async () => {
  const ticks = [];
  let done = 0;
  await new Promise((resolve) => {
    createTypewriter({
      segments: ['前段', '后段文字'],
      intervalMs: 1,
      charsPerTick: 2,
      onTick: (r) => ticks.push(r),
      onDone: () => { done += 1; resolve(); },
    }).start();
  });
  assert.equal(done, 1);
  assert.deepEqual(ticks[ticks.length - 1], ['前段', '后段文字']);
});

test('finishNow：立即整段直出 + onDone；后续不再走表', async () => {
  const ticks = [];
  let done = 0;
  const tw = createTypewriter({
    segments: ['很长很长的一段答案文本'],
    intervalMs: 50,
    onTick: (r) => ticks.push(r),
    onDone: () => { done += 1; },
  });
  tw.start();
  tw.finishNow();
  assert.deepEqual(ticks[ticks.length - 1], ['很长很长的一段答案文本']);
  assert.equal(done, 1);
  const n = ticks.length;
  await new Promise((r) => setTimeout(r, 120));
  assert.equal(ticks.length, n);  // 定时器已清，不再产生 tick
});

test('cancel：停在当前进度，不再 tick 也不触发 onDone', async () => {
  const ticks = [];
  let done = 0;
  const tw = createTypewriter({
    segments: ['一些文本内容'],
    intervalMs: 1,
    onTick: (r) => ticks.push(r),
    onDone: () => { done += 1; },
  });
  tw.start();
  tw.cancel();
  const n = ticks.length;
  await new Promise((r) => setTimeout(r, 60));
  assert.equal(ticks.length, n);
  assert.equal(done, 0);
});

test('空 segments：start 直接 onDone', () => {
  let done = 0;
  createTypewriter({ segments: [], onDone: () => { done += 1; } }).start();
  assert.equal(done, 1);
});
