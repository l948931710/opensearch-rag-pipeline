#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_audit_digest.py — agent_audit_log 摘要链只读核验（Majors γ6/M7-lite）。

对指定北京业务日区间逐日核验（全程只读，DB 读 + OSS 读，零写入）：
  ① manifest 存在且 HMAC 验签通过（多把 key 按 manifest.key_id 匹配——轮换后旧链用旧 key）；
  ② prev_digest 链完整（= 前一日 manifest 文件字节 SHA-256；区间首日若有前日文件也校）；
  ③ DB 重算 count + Merkle 根与 manifest 一致（行序/canonical 形态与产出侧共用
     opensearch_pipeline.audit_digest 同一实现，绝不各写一份漂移）。

用法：
  RAG_AUDIT_HMAC_KEY=... python scripts/verify_audit_digest.py --start 2026-07-01 --end 2026-07-20
  # 轮换期：--extra-key 可重复传旧 key
  python scripts/verify_audit_digest.py --days 7 --extra-key OLDKEY1

exit：0=全部通过；1=任一日验签/断链/根不匹配；3=环境/读取故障。
⚠️ 结论边界（M7 OPEN）：通过=「已封存日未被事后删改」；当日未封窗口不在检测面。
"""
import argparse
import hashlib
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opensearch_pipeline.audit_digest import (  # noqa: E402
    TABLE,
    _fetch_day_rows,
    _manifest_key,
    key_id_of,
    merkle_root,
    row_hash,
    verify_manifest_signature,
)


def _load_manifest(bucket, day: str):
    try:
        body = bucket.get_object(_manifest_key(day)).read()
    except Exception:   # noqa: BLE001 — 不存在/不可读由调用方定性
        return None, None
    import json
    return json.loads(body.decode("utf-8")), hashlib.sha256(body).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="audit digest 只读核验")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--days", type=int, help="核验最近 N 个已结算日（与 --start/--end 互斥）")
    ap.add_argument("--extra-key", action="append", default=[],
                    help="轮换期旧 key（可重复；主 key 走 RAG_AUDIT_HMAC_KEY）")
    args = ap.parse_args()

    keys = [k for k in ([os.environ.get("RAG_AUDIT_HMAC_KEY", "").strip()]
                        + list(args.extra_key)) if k]
    if not keys:
        print("❌ 未提供任何 key（RAG_AUDIT_HMAC_KEY / --extra-key）")
        return 3
    by_id = {key_id_of(k): k for k in keys}

    if args.days:
        end_d = datetime.now().date() - timedelta(days=1)
        start_d = end_d - timedelta(days=args.days - 1)
    elif args.start and args.end:
        start_d = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_d = datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        ap.error("需要 --start+--end 或 --days")
        return 3

    try:
        from opensearch_pipeline.clients import _get_oss_bucket
        from opensearch_pipeline.config import get_config
        from opensearch_pipeline.db import _get_db_conn
        bucket, simulated = _get_oss_bucket()
        if simulated or bucket is None:
            print("❌ OSS 不可用（simulate/无凭证），无从读取 manifest")
            return 3
        db = get_config().rds.operation_database
        conn = _get_db_conn()
    except Exception as e:   # noqa: BLE001
        print(f"❌ 环境初始化失败: {e}")
        return 3

    failures, checked = [], 0
    try:
        prev_day = start_d - timedelta(days=1)
        _prev_manifest, prev_file_digest = _load_manifest(bucket, prev_day.strftime("%Y-%m-%d"))
        d = start_d
        while d <= end_d:
            day = d.strftime("%Y-%m-%d")
            manifest, file_digest = _load_manifest(bucket, day)
            if manifest is None:
                failures.append(f"{day}: manifest 缺失")
                prev_file_digest = None
                d += timedelta(days=1)
                continue
            checked += 1
            key = by_id.get(manifest.get("key_id") or "")
            if key is None:
                failures.append(f"{day}: key_id={manifest.get('key_id')} 无匹配 key（轮换缺旧 key？）")
            elif not verify_manifest_signature(manifest, key):
                failures.append(f"{day}: HMAC 验签失败（manifest 被改或 key 不符）")
            if prev_file_digest is not None \
                    and manifest.get("prev_digest") != prev_file_digest:
                failures.append(f"{day}: prev_digest 断链（历史 manifest 被改/重写）")
            rows = _fetch_day_rows(conn, db, day)
            root = merkle_root([row_hash(r) for r in rows], table=TABLE, day=day)
            if int(manifest.get("count", -1)) != len(rows):
                failures.append(f"{day}: 行数漂移 manifest={manifest.get('count')} db={len(rows)}"
                                "（封存后删/插）")
            elif manifest.get("merkle_root") != root:
                failures.append(f"{day}: Merkle 根不匹配（封存后行内容被改）")
            prev_file_digest = file_digest
            d += timedelta(days=1)
    finally:
        conn.close()

    if failures:
        print(f"❌ 核验失败 {len(failures)} 项（覆盖 {checked} 份 manifest）：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"✅ {start_d} → {end_d} 共 {checked} 份 manifest 全部通过"
          f"（验签/链/根三面；当日未封窗口不在检测面）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
