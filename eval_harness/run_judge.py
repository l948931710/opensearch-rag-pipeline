"""Auto-judge runner (DRAFT) — turn a judge_bundle into judge_verdicts via the local claude CLI.

Closes the answer-correctness AUTOMATION gap: the eval's correctness/faithfulness/completeness gates
need a panel of verdicts that were historically HAND-authored, so `merge --strict` could never run
unattended. This runs N independent claude passes over the bundle with JUDGE_RUBRIC (or CHUNK_RUBRIC
for L6), parses the structured verdicts, and writes `{panels:[{judge,verdicts}]}` for run_eval merge.

Usage:
  python -m eval_harness.run_judge --bundle <dir>/judge_bundle.json --out <dir>/judge_verdicts.json
        [--panels 3] [--rubric answer|chunk] [--batch 20]

Needs the claude CLI authed (RAG_CLAUDE_BIN, default `claude`). DRAFT — validate output schema +
inter-judge agreement on the real eval host before gating releases on it. The judge is Claude while
answers are Qwen (no self-grading); for a defensible gate, also keep a small human-labelled
calibration subset and gate inter-judge stdev (see docs/eval_release_gate_DRAFT.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

from .judge import (
    CHUNK_RUBRIC,
    CHUNK_VERDICT_ITEM_SCHEMA,
    JUDGE_RUBRIC,
    MM_RUBRIC,
    MM_VERDICT_ITEM_SCHEMA,
    VERDICT_ITEM_SCHEMA,
)

CLAUDE = os.environ.get("RAG_CLAUDE_BIN", "claude")

# P2-24：judge 模型显式 pin。不 pin 时 judge 是环境里 claude CLI 恰好解析到的默认模型,
# CLI/默认模型升级会静默重基 faithfulness/correctness/completeness/fabrication 四个硬门。
# RAG_EVAL_JUDGE_MODEL 可覆写——换 judge 模型属于换 regime（run_eval._regime() 把该值记入
# baseline 指纹）,换后需 refreeze。
JUDGE_MODEL = os.environ.get("RAG_EVAL_JUDGE_MODEL", "claude-opus-4-8")

# rubric → (rubric 文本, verdict schema, item id 键)。
# #F-mm13：mm rubric 新增；顺带修存量坑——chunk rubric 此前也用 answer 的
# VERDICT_ITEM_SCHEMA 列 keys（prompt 里 keys 行与 rubric 文本自相矛盾，全靠
# rubric 文本压住），现在逐 rubric 用自己的 schema。
_RUBRICS = {
    "answer": (JUDGE_RUBRIC, VERDICT_ITEM_SCHEMA, "qid"),
    "chunk": (CHUNK_RUBRIC, CHUNK_VERDICT_ITEM_SCHEMA, "item_id"),
    "mm": (MM_RUBRIC, MM_VERDICT_ITEM_SCHEMA, "qid"),
}


def _extract_json_array(text: str):
    """Pull the JSON array out of claude's reply (tolerates ```json fences / prose around it)."""
    t = (text or "").strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", t, re.S)
        if m:
            return json.loads(m.group(1))
    m = re.search(r"\[.*\]", t, re.S)
    if not m:
        raise ValueError(f"no JSON array in claude output: {t[:200]!r}")
    return json.loads(m.group(0))


def _judge_batch(rubric: str, items: list, panel_idx: int, item_id_key: str,
                 schema: dict = VERDICT_ITEM_SCHEMA) -> list:
    keys = sorted(schema["required"])
    prompt = (
        f"{rubric}\n\nYou are judge #{panel_idx + 1} of an INDEPENDENT panel; judge on your own merits.\n"
        f"Judge EVERY item below. Return ONLY a JSON array — one object per item — each with EXACTLY "
        f"these keys: {keys}. The '{item_id_key}' field MUST equal the item's '{item_id_key}'. "
        f"No prose outside the JSON.\n\nITEMS (JSON):\n{json.dumps(items, ensure_ascii=False)}"
    )
    # --model 显式 pin（P2-24）：判分模型不再跟随 CLI 默认漂移
    r = subprocess.run([CLAUDE, "-p", prompt, "--model", JUDGE_MODEL],
                       cwd="/tmp", capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"claude rc={r.returncode}: {(r.stderr or '')[:300]}")
    return _extract_json_array(r.stdout)


def run(bundle_path: str, out_path: str, panels: int = 3, rubric: str = "answer", batch: int = 20):
    bundle = json.load(open(bundle_path, encoding="utf-8"))
    rub, schema, id_key = _RUBRICS[rubric]
    out_panels = []
    for pi in range(panels):
        verdicts = []
        for i in range(0, len(bundle), batch):
            chunk = bundle[i:i + batch]
            # Graceful degradation: a single stochastic claude batch returning malformed JSON
            # (missing comma etc.) must NOT crash the whole panel/run. Retry once, then skip
            # that batch's items in THIS panel (the other panels still cover them).
            got = None
            for attempt in range(2):
                try:
                    got = _judge_batch(rub, chunk, pi, id_key, schema=schema)
                    break
                except (ValueError, RuntimeError) as e:  # JSONDecodeError ⊂ ValueError
                    print(f"[run_judge] panel {pi + 1} batch@{i} attempt {attempt + 1} "
                          f"failed: {type(e).__name__}: {str(e)[:120]}")
            if got is None:
                print(f"[run_judge] panel {pi + 1} batch@{i}: SKIPPED {len(chunk)} items after retries")
                continue
            verdicts.extend(got)
        out_panels.append({"judge": f"claude-auto-{pi + 1}", "verdicts": verdicts})
        print(f"[run_judge] panel {pi + 1}/{panels}: {len(verdicts)} verdicts")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"panels": out_panels}, f, ensure_ascii=False, indent=1)
    print(f"[run_judge] wrote {out_path} ({panels} panels x {len(bundle)} items)")
    return out_path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--panels", type=int, default=3)
    ap.add_argument("--rubric", choices=["answer", "chunk", "mm"], default="answer")
    ap.add_argument("--batch", type=int, default=20)
    a = ap.parse_args(argv)
    if not os.path.exists(a.bundle):
        print(f"[run_judge] bundle not found: {a.bundle} (nothing to judge)")
        return 0
    run(a.bundle, a.out, panels=a.panels, rubric=a.rubric, batch=a.batch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
