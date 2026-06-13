"""End-to-end evaluation harness for the rebuilt HA3 RAG system.

Layers: L0 index-health · L1 retrieval-ranking · L2 score-calibration ·
L3 answer-quality (Claude-judged) · L4 multimodal · L5 permission · L6 latency.

⚠️ envboot 不再在 package import 时自动跑 — 由实际跑 prod-only 操作的 layer 文件
自己顶部 `from .. import envboot` 触发 boot()。这样纯逻辑子包(如 .binding/)
可以被外部 import 而不污染 os.environ(测试间状态独立)。

2026-06-12 改动:移除顶部 `from . import envboot`(原会强写 RAG_ENVIRONMENT=test
等 env,任何 from eval_harness.<anything> 都会触发,泄漏到无关 pytest 进程后续
所有测试,造成 ~60 个不相关测试莫名 fail)。layer 文件仍 import envboot(没变),
run_eval.py 也直接 import envboot(没变),功能等价。
"""
