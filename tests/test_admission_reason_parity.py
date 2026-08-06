# -*- coding: utf-8 -*-
"""前后端契约 parity:限流拒绝码(rate_limiter.Denial.reason)↔ 控制台文案表。

为什么需要跨语言守卫:两侧各自维护、编译器和类型系统都管不到,**漂移不会报错**——
前端 `admissionReasonLabel` 的兜底是 `|| r`,漏一个码只会在运营面板上把机器码原样吐出来,
看起来"能用",实际把一个可读指标降级成了噪声。

2026-08-06 实测:后端 11 个码,前端表只有 7 个。缺的四个里,`auth_per_min` 是当天新增的
登录独立桶——而那次分桶的**核心收益**恰恰是"台账能自己区分登录挤爆 vs 控制台在刷,
不必再翻 SLS"。文案缺失会直接削掉这个收益。另三个 general_* 早就漏了。
"""
import pathlib
import re

_RL = pathlib.Path("opensearch_pipeline/rate_limiter.py")
_KB = pathlib.Path("console-app/src/lib/kb.ts")


def _backend_reasons() -> set:
    """从 `self._deny(actor, Denial(..., "<reason>"))` 抽码。

    锚点取 `"<reason>"))` —— reason 恒是 Denial 的最后一个位置参数,其后紧跟两个右括号
    (闭合 Denial 与 _deny)。⚠️ **不要**改回"从 Denial( 起匹配到第一个 )"那种写法:
    有的构造里嵌了函数调用(`_secs_to_beijing_midnight(now)`),ASCII 括号会把匹配提前截断,
    于是少抽 4 个码、让反向断言报出一批并不存在的"陈旧文案"(2026-08-06 实测踩中)。
    """
    src = _RL.read_text(encoding="utf-8")
    return set(re.findall(r',\s*"([a-z_]+)"\s*\)\s*\)', src))


def _frontend_labels() -> set:
    block = re.search(r"ADMISSION_REASON_LABEL[^{]*\{(.*?)\n\}", _KB.read_text(encoding="utf-8"), re.S)
    assert block, "kb.ts 里找不到 ADMISSION_REASON_LABEL —— 表被改名/移动了,本守卫需同步"
    return set(re.findall(r"([a-z_]+)\s*:", block.group(1)))


def test_every_backend_reason_has_a_frontend_label():
    back, front = _backend_reasons(), _frontend_labels()
    assert back, "没抽到任何后端 reason 码 —— 正则与 Denial 构造形态脱节,守卫已失效"
    missing = sorted(back - front)
    assert not missing, (
        f"以下限流拒绝码在控制台没有中文文案,运营面板会原样吐机器码:{missing}\n"
        f"补 console-app/src/lib/kb.ts 的 ADMISSION_REASON_LABEL。")


def test_no_stale_frontend_labels():
    """反向:前端有、后端已无的码 = 删码时忘了清表(误导读者以为该状态还会出现)。"""
    back, front = _backend_reasons(), _frontend_labels()
    stale = sorted(front - back)
    assert not stale, f"这些码后端已不再产出,应从 kb.ts 的表里删除:{stale}"
