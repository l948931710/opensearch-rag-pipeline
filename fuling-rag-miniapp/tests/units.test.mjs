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
copyFileSync(join(here, '../utils/conversations.js'), join(tmp, 'conversations.mjs'));
const { parseTextBlock, plainText, plainLength, sliceParas } =
  await import(pathToFileURL(join(tmp, 'markdown.mjs')));
const { createTypewriter } = await import(pathToFileURL(join(tmp, 'typewriter.mjs')));
const {
  genConvId, deriveTitle, parseServerTime, relativeTimeLabel,
  mergeServerList, leanMessages, capIndex, MAX_MESSAGES, splitNrLines,
} = await import(pathToFileURL(join(tmp, 'conversations.mjs')));

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

// ── conversations（多会话纯函数；dd 存储包装不在 node 环境测） ──────────

test('genConvId：唯一、契约长度内（<=128）', () => {
  const seen = new Set();
  for (let i = 0; i < 500; i++) {
    const id = genConvId();
    assert.ok(id.length > 4 && id.length <= 128);
    assert.ok(!seen.has(id), 'duplicate conv id: ' + id);
    seen.add(id);
  }
});

test('deriveTitle：压空白、截 60 字、空入空出', () => {
  assert.equal(deriveTitle('  U8+ 如何\n\n登录？  '), 'U8+ 如何 登录？');
  assert.equal(deriveTitle('长'.repeat(99)).length, 60);
  assert.equal(deriveTitle(''), '');
  assert.equal(deriveTitle(null), '');
});

test('parseServerTime：空格/T 分隔均可解析，非法返回 0（不臆造时间）', () => {
  const t1 = parseServerTime('2026-07-02 10:11:12');
  const t2 = parseServerTime('2026-07-02T10:11:12.123');
  assert.ok(t1 > 0);
  const d = new Date(t1);
  assert.equal(d.getHours(), 10);
  assert.equal(d.getDate(), 2);
  assert.equal(t2 - t1, 0); // 同秒（毫秒段忽略）
  assert.equal(parseServerTime('not a date'), 0);
  assert.equal(parseServerTime(''), 0);
});

test('relativeTimeLabel：刚刚/分钟/同日 HH:MM/昨天/N天前/跨周 MM-DD', () => {
  const now = new Date(2026, 6, 2, 15, 0, 0).getTime(); // 2026-07-02 15:00
  assert.equal(relativeTimeLabel(now - 30 * 1000, now), '刚刚');
  assert.equal(relativeTimeLabel(now - 5 * 60 * 1000, now), '5分钟前');
  assert.equal(relativeTimeLabel(new Date(2026, 6, 2, 9, 5).getTime(), now), '09:05');
  assert.equal(relativeTimeLabel(new Date(2026, 6, 1, 23, 0).getTime(), now), '昨天');
  assert.equal(relativeTimeLabel(now - 3 * 24 * 3600 * 1000 - 3600 * 1000, now), '3天前');
  assert.equal(relativeTimeLabel(new Date(2026, 4, 20).getTime(), now), '05-20');
  assert.equal(relativeTimeLabel(0, now), '');
});

test('mergeServerList：本地行为准（补标题/取较新时间），服务端独有行 remote 占位，倒序', () => {
  const local = [
    { id: 'a', title: '本地标题', updatedAt: 1000, sessionId: 's1' },
    { id: 'b', title: '', updatedAt: 500, sessionId: 's2' },
  ];
  const server = [
    { conversation_id: 'a', title: '服务端标题', updated_at: '2026-07-02 10:00:00' },
    { conversation_id: 'b', title: '补上的标题', updated_at: '' },
    { conversation_id: 'c', title: '云端会话', updated_at: '2026-07-01 08:00:00' },
    { conversation_id: '', title: '脏行丢弃' },
  ];
  const merged = mergeServerList(local, server);
  assert.equal(merged.length, 3);
  const a = merged.find((x) => x.id === 'a');
  assert.equal(a.title, '本地标题');            // 本地已有标题不被覆盖
  assert.ok(a.updatedAt > 1000);                // 服务端时间较新 → 取之
  assert.equal(a.sessionId, 's1');              // 本地字段保留
  const b = merged.find((x) => x.id === 'b');
  assert.equal(b.title, '补上的标题');          // 本地缺标题 → 补服务端的
  assert.equal(b.updatedAt, 500);               // 服务端时间无效 → 保本地
  const c = merged.find((x) => x.id === 'c');
  assert.equal(c.remote, true);
  assert.equal(c.sessionId, '');
  assert.equal(merged[0].id, 'a');              // 倒序：a(2026) > c(7-01) > b(500)
  assert.equal(merged[1].id, 'c');
  // 入参不被原地污染
  assert.equal(local[1].title, '');
});

test('leanMessages：丢 loading/error 瞬态，保最终答案/NO_RESULT，AI 行恒 instant', () => {
  const lean = leanMessages([
    { role: 'user', text: '问题一' },
    { role: 'ai', loading: true, question: '问题一' },              // 瞬态：丢
    { role: 'ai', error: true, errorText: '失败', question: 'x' },  // 瞬态：丢
    { role: 'ai', blocks: [{ type: 'text', text: '答案' }], sources: [{ idx: 1 }],
      messageId: 'mid1', copyText: '答案', question: '问题一', guard: true, general: true },
    { role: 'ai', noResult: true, rephrase: ['换个说法'], messageId: 'mid2',
      question: '问题二',
      answer: '抱歉，知识库中暂时没有找到能直接回答这个问题的资料。\n您是不是想问：\n· 《考勤管理制度》' },
    { role: 'ai' },                                                  // 无 blocks 无 noResult：丢
  ]);
  assert.equal(lean.length, 3);
  assert.deepEqual(lean[0], { role: 'user', text: '问题一' });
  assert.equal(lean[1].instant, true);
  assert.equal(lean[1].guard, true);
  assert.equal(lean[1].general, true);   // 通用回答徽标随会话落盘往返
  assert.equal(lean[1].messageId, 'mid1');
  assert.equal(lean[2].noResult, true);
  assert.equal(lean[2].handoffDone, undefined);   // 转人工已下线：不再落盘
  // 引导式拒答话术原文随 NO_RESULT 卡落盘（nrLines 不落盘，恢复时重算）
  assert.ok(lean[2].answer.includes('《考勤管理制度》'));
  assert.equal(lean[2].nrLines, undefined);
});

test('leanMessages：NO_RESULT 无 answer（老后端/flag 关）落空串', () => {
  const lean = leanMessages([
    { role: 'ai', noResult: true, rephrase: [], messageId: 'm', question: 'q' },
  ]);
  assert.equal(lean[0].answer, '');
});

test('splitNrLines：多行拆行滤空、单行原样、空入参返回 []', () => {
  assert.deepEqual(
    splitNrLines('第一行\n\n· 《标题》 \n第三行'),
    ['第一行', '· 《标题》', '第三行'],
  );
  assert.deepEqual(splitNrLines('只有一行'), ['只有一行']);
  assert.deepEqual(splitNrLines(''), []);
  assert.deepEqual(splitNrLines(undefined), []);
});

test('leanMessages：超长会话截尾保最近 MAX_MESSAGES 条', () => {
  const msgs = [];
  for (let i = 0; i < MAX_MESSAGES + 10; i++) {
    msgs.push({ role: 'user', text: 'q' + i });
  }
  const lean = leanMessages(msgs);
  assert.equal(lean.length, MAX_MESSAGES);
  assert.equal(lean[lean.length - 1].text, 'q' + (MAX_MESSAGES + 9)); // 保尾不保头
});

test('capIndex：按 updatedAt 倒序裁剪，evictedIds = 被淘汰的最旧行', () => {
  const list = [
    { id: 'old', updatedAt: 1 },
    { id: 'new', updatedAt: 3 },
    { id: 'mid', updatedAt: 2 },
  ];
  const r = capIndex(list, 2);
  assert.deepEqual(r.list.map((c) => c.id), ['new', 'mid']);
  assert.deepEqual(r.evictedIds, ['old']);
  const r2 = capIndex(list, 10);
  assert.equal(r2.evictedIds.length, 0);
});
