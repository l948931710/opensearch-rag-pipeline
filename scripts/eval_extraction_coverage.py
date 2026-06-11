#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_extraction_coverage.py — 数据管线对真实 OSS 分布的泛化覆盖测试

与 GT 评测（9 个手挑文档）和绑定评测（fuling_chunk_exp 的 docx）互补：
本工具按【真实语料分布】分层抽样 OSS raw/ 的活跃文件，对每个文件跑
完整 提取→切块 离线链路，断言结构不变量 —— 回答"管线对全部真实文件
类型都能产出可检索/可渲染的 chunk 吗"，而不是"质量多高"。

不变量（逐文件）：
  I1  提取不抛异常
  I2  extract_method 非 unsupported（被 ingest_policy 排除的类型不计入）
  I3  text 非空 或 assets 非空（空文档 = 用户不可见）
  I4  chunks ≥ 1
  I5  CLEAN 图片资产 → 至少一个 chunk 携带可渲染 image 引用
      （image_refs[].source_image 或 chunk.extra.source_image 非空）

默认 VLM/OCR 走 simulate（结构检验，免 API 费、确定性好）；--real 用真实 API。
OSS 下载始终真实（需要 RAG_ENV=test 的凭据）。

用法：
  RAG_ENV=test python3 scripts/eval_extraction_coverage.py             # 每类抽 12 个
  RAG_ENV=test python3 scripts/eval_extraction_coverage.py --per-ext 5 --ext pdf docx
  RAG_ENV=test python3 scripts/eval_extraction_coverage.py --real      # 真实 VLM/OCR
退出码：有不变量失败 → 1（可挂 CI / 回灌前预检）。
"""

import argparse
import contextlib
import io
import json
import os
import random
import sys
import tempfile
import traceback
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

os.environ.setdefault("RAG_ENV", "test")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-ext", type=int, default=12, help="每个扩展名抽样数（默认 12）")
    ap.add_argument("--ext", nargs="*", default=None, help="只测这些扩展名")
    ap.add_argument("--seed", type=int, default=42, help="抽样随机种子（可复现）")
    ap.add_argument("--real", action="store_true", help="VLM/OCR 用真实 API（默认 simulate）")
    ap.add_argument("--json-out", default="", help="结果 JSON 输出路径")
    args = ap.parse_args()

    # 注意：RAG_ENV=test 的 .env.test 会覆盖进程环境变量里的 RAG_SIMULATE，
    # 所以 simulate 不能靠 env 传递 —— 用 UnifiedExtractor(simulate=...) 构造参数。
    simulate_vlm = not args.real

    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.ingest_policy import should_ingest_raw_key

    import oss2

    cfg = get_config()
    auth = oss2.Auth(cfg.oss.access_key_id, cfg.oss.access_key_secret)
    bucket = oss2.Bucket(auth, "https://" + cfg.oss.endpoint, cfg.oss.bucket_name)

    # ── 1. 清单 + 分层抽样（按 ingest_policy 的活跃视角） ──
    print("📂 扫描 OSS raw/ 活跃文件清单 …")
    by_ext = defaultdict(list)
    skip_stats = defaultdict(int)
    for obj in oss2.ObjectIterator(bucket, prefix="raw/"):
        ok, reason = should_ingest_raw_key(obj.key)
        if not ok:
            skip_stats[reason] += 1
            continue
        ext = os.path.splitext(obj.key)[1].lower().lstrip(".")
        by_ext[ext].append(obj.key)

    print("   活跃分布: " + ", ".join(f"{e}×{len(v)}" for e, v in
                                       sorted(by_ext.items(), key=lambda kv: -len(kv[1]))))
    top_skips = sorted(skip_stats.items(), key=lambda kv: -kv[1])[:6]
    print("   策略跳过: " + ", ".join(f"{r}×{n}" for r, n in top_skips))

    if args.ext:
        args.ext = [e.lower().lstrip(".") for e in args.ext]
    sample = []
    for ext, keys in sorted(by_ext.items()):
        if args.ext and ext not in args.ext:
            continue
        # 每个扩展名独立 RNG（字符串种子，跨进程确定）：增删某一类文件
        # 不会扰动其他类的抽样，--ext 过滤也不改变同类的选择
        rng = random.Random(f"{args.seed}:{ext}")
        picked = keys if len(keys) <= args.per_ext else rng.sample(keys, args.per_ext)
        sample.extend((ext, k) for k in sorted(picked))
    if not sample:
        print("❌ 抽样为空（--ext 拼写？活跃语料为空？）")
        sys.exit(2)
    print(f"🎯 抽样 {len(sample)} 个文件（seed={args.seed}, per-ext={args.per_ext}）\n")

    # ── 2. 逐文件 提取→切块 + 不变量 ──
    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
    from opensearch_pipeline.pipeline_nodes import node_chunk_documents

    results = []
    for i, (ext, key) in enumerate(sample, 1):
        name = os.path.basename(key)
        row = {"ext": ext, "key": key, "ok": True, "violations": [], "warnings": 0}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                local = os.path.join(tmp, name)
                bucket.get_object_to_file(key, local)
                r = UnifiedExtractor(simulate=simulate_vlm).extract({
                    "doc_id": f"COV{i:04d}", "version_no": 1, "local_path": local,
                    "file_ext": ext, "filename": name, "raw_key": key, "_tmp_dir": tmp,
                })
                row["extract_method"] = r.extract_method
                row["text_len"] = r.text_length
                row["n_assets"] = len(r.assets or [])
                row["warnings"] = len(r.warnings or [])

                if r.extract_method.startswith("unsupported"):
                    row["violations"].append("I2:unsupported")
                if not (r.text_length or 0) and not (r.assets or []):
                    row["violations"].append("I3:empty (no text, no assets)")

                doc = {
                    "doc_id": f"COV{i:04d}", "version_no": 1, "title": name,
                    "filename": name, "file_ext": ext, "text": r.text,
                    "blocks": r.blocks, "assets": r.assets, "source_key": key,
                    "canonical_key": "", "owner_dept": key.split("/")[1] if key.count("/") > 1 else "x",
                    "category_l1": "", "category_l2": "", "permission_level": "public",
                    "kb_type": "public", "risk_level": "low", "redaction_action": "CLEAN",
                }
                ctx = {"canonicals": [doc], "split_mode": "dynamic",
                       "prepend_title": True, "prepend_section": True}
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    node_chunk_documents(ctx)
                chunks = ctx.get("chunks", [])
                row["n_chunks"] = len(chunks)
                if not chunks and "I3:empty (no text, no assets)" not in row["violations"]:
                    row["violations"].append("I4:no chunks")

                clean_assets = [a for a in (r.assets or [])
                                if a.get("status") in ("ROUTE_TO_VECTOR", "ROUTE_TO_TEXT")]
                if clean_assets:
                    # I5 按 serving 真实可达性判定（对抗评审修正）：
                    #   - chunk 级 source_image（image/visual_knowledge 类型）经 to_ha3_doc 存活 ✓
                    #   - image_refs 经 RDS image_refs_json 恢复，仅覆盖
                    #     step_card/procedure_parent/visual_knowledge 类型 ✓
                    #   - 其余文本类 chunk 上的 refs 在 serving 端不可达，仅当其带
                    #     durable oss_key（如独立图片文档指向 raw/ 对象）才算可渲染
                    _SERVING_REF_TYPES = {"step_card", "procedure_parent", "visual_knowledge"}
                    renderable = False
                    for c in chunks:
                        cx = getattr(c, "extra", {}) or {}
                        ctype = getattr(c, "chunk_type", "")
                        if cx.get("source_image") and ctype in ("image", "visual_knowledge"):
                            renderable = True
                            break
                        for ref in (cx.get("image_refs") or []):
                            if ref.get("oss_key"):
                                renderable = True
                                break
                            if ref.get("source_image") and ctype in _SERVING_REF_TYPES:
                                renderable = True
                                break
                        if renderable:
                            break
                    if not renderable:
                        row["violations"].append("I5:images extracted but none serving-renderable")
        except Exception as e:
            row["ok"] = False
            row["violations"].append(f"I1:exception {type(e).__name__}: {e}")
            row["traceback"] = traceback.format_exc(limit=4)

        if row["violations"]:
            row["ok"] = False
        mark = "✅" if row["ok"] else "❌"
        print(f"{mark} [{i}/{len(sample)}] .{ext:5s} {name[:52]:52s} "
              f"m={row.get('extract_method','-'):20s} txt={row.get('text_len','-')} "
              f"chk={row.get('n_chunks','-')} {';'.join(row['violations'])}")
        results.append(row)

    # ── 3. 汇总 ──
    print("\n" + "=" * 78)
    print(f"{'ext':8s} {'sampled':>8s} {'pass':>6s} {'fail':>6s}")
    failed_total = 0
    by = defaultdict(lambda: [0, 0])
    for row in results:
        by[row["ext"]][0 if row["ok"] else 1] += 1
    for ext, (n_ok, n_fail) in sorted(by.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
        print(f"{ext:8s} {n_ok + n_fail:8d} {n_ok:6d} {n_fail:6d}")
        failed_total += n_fail
    print(f"\n{'PASS ✅' if failed_total == 0 else f'FAIL ❌ ({failed_total} files)'}")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"seed": args.seed, "per_ext": args.per_ext,
                       "simulate": not args.real, "results": results}, f,
                      ensure_ascii=False, indent=1, default=str)
        print(f"📄 详情: {args.json_out}")

    sys.exit(1 if failed_total else 0)


if __name__ == "__main__":
    main()
