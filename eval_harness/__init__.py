"""End-to-end evaluation harness for the rebuilt HA3 RAG system.

Layers: L0 index-health · L1 retrieval-ranking · L2 score-calibration ·
L3 answer-quality (Claude-judged) · L4 multimodal · L5 permission · L6 latency.

Importing `eval_harness` (or any submodule) boots the live/public/read-only env.
"""
from . import envboot  # noqa: F401
