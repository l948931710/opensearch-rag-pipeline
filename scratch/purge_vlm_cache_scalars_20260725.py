#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理 VLM 缓存 sqlite 里「值不是 JSON object」的脏条目（D1，2026-07-25）。

背景
----
本机 scratch/vlm_cache.sqlite3 里有一批条目的值是 JSON 数字而非 funnel 结果 dict
（rowid 连续 ⇒ 一次性批量写入，仓库内 5 个写入点全部经 _vlm_cache_entry() 返回 dict，
遗留 vlm_cache.json 也全是 dict，来源无法从仓库证据确证，不作结论）。

**代码侧已修**：unified_extractor._vlm_cache_entry_usable 把形态校验收到唯一读入口，
非法条目视同未命中 → 重算 → 同 key 被合法 dict 覆盖（自愈）。所以本脚本是**可选的
卫生操作**，不是正确性前置。

⚠️ OSS 传播（务必读）
--------------------
本脚本自身**不做任何 OSS 调用**。但缓存的 OSS 镜像推送是「checkpoint 后整库上传」
（embedding_cache.SqliteKVStore._push_oss_mirror）：**下一次开着 RAG_VLM_CACHE_OSS_MIRROR
的真实运行，会把清理后的整个库推到 processing/cache/vlm_cache.sqlite3**。也就是说本地
删除会**间接传播到生产镜像**。要不要让它传播，是使用者的决定，不是本脚本的默认行为。
如果不希望传播，请对副本操作（--db 指向拷贝），或在授权前不要让该库被真实运行复用。

用法
----
    python3 scratch/purge_vlm_cache_scalars_20260725.py                 # dry-run 盘点（默认库）
    python3 scratch/purge_vlm_cache_scalars_20260725.py --db /path/x.sqlite3
    python3 scratch/purge_vlm_cache_scalars_20260725.py --db /path/x.sqlite3 --apply

--apply 必须显式给 --db（不提供默认值，杜绝误清会被真实运行复用的本地库）。
删除前用 sqlite3 online backup API 做备份（WAL 安全；裸 cp 会漏 WAL 内容），备份文件名
取第一个未占用序号，**绝不覆盖既有备份**，并对备份跑 PRAGMA integrity_check。
"""
import argparse
import collections
import json
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scratch", "vlm_cache.sqlite3")
TABLE = "vlm_cache"

OSS_NOTICE = (
    "⚠️  OSS 传播提醒：本脚本不调用 OSS，但缓存镜像是「整库上传」——下一次开着\n"
    "    RAG_VLM_CACHE_OSS_MIRROR 的真实运行会把这个库整体推到\n"
    "    processing/cache/vlm_cache.sqlite3，本地删除会因此间接传播到生产镜像。\n"
    "    不希望传播就对副本操作（--db 指向拷贝），或在授权前别让该库被真实运行复用。")


def _key_shape(k):
    """{hash}:{ns} → ns（无冒号=裸键）；同时给出 hash 长度以区分 MD5(32)/SHA-256(64)。"""
    parts = k.split(":")
    return (":".join(parts[1:]) or "<bare>", len(parts[0]))


def survey(db_path):
    """只读盘点：返回 (总数, 坏条目 [(rowid, key, value)], 形态计数 dict)。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        total = con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        bad, shapes = [], collections.Counter()
        for rowid, k, v in con.execute(f"SELECT rowid, k, v FROM {TABLE}"):
            try:
                obj = json.loads(v)
            except Exception:
                obj = "<unparseable>"
            if isinstance(obj, dict):
                continue
            bad.append((rowid, k, obj))
            ns, hlen = _key_shape(k)
            shapes[(ns, hlen, type(obj).__name__)] += 1
        return total, bad, shapes
    finally:
        con.close()


def _next_backup_path(db_path):
    """第一个未占用的序号备份名——绝不覆盖既有备份。"""
    i = 1
    while True:
        cand = f"{db_path}.bak-purge{i:02d}"
        if not os.path.exists(cand):
            return cand
        i += 1


def backup_and_verify(db_path):
    """online backup API（WAL 安全）+ 备份侧 integrity_check。返回备份路径。"""
    dst = _next_backup_path(db_path)
    src = sqlite3.connect(db_path)
    try:
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    chk = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        res = chk.execute("PRAGMA integrity_check").fetchone()[0]
        n = chk.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    finally:
        chk.close()
    if res != "ok":
        raise SystemExit(f"❌ 备份 integrity_check 未通过（{res}），已中止，未删除任何行。备份: {dst}")
    print(f"  备份: {dst}  (integrity_check=ok, {n} 条)")
    return dst


def purge(db_path, rowids):
    """单事务删除指定 rowid。返回实际删除行数。"""
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        cur.execute("BEGIN")
        for chunk_start in range(0, len(rowids), 500):
            chunk = rowids[chunk_start:chunk_start + 500]
            qs = ",".join("?" * len(chunk))
            cur.execute(f"DELETE FROM {TABLE} WHERE rowid IN ({qs})", chunk)
        deleted = con.total_changes
        con.commit()
        return deleted
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="清理 VLM 缓存里值不是 JSON object 的脏条目（默认 dry-run）。",
        epilog=OSS_NOTICE, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None,
                    help=f"sqlite 路径。省略=只读盘点默认库 {DEFAULT_DB}；--apply 时必须显式给。")
    ap.add_argument("--apply", action="store_true",
                    help="真正删除（需 --db）。删除前自动备份+校验。")
    args = ap.parse_args(argv)

    if args.apply and not args.db:
        ap.error("--apply 必须显式给 --db（不提供默认值，避免误清会被真实运行复用的本地库）")
    db_path = args.db or DEFAULT_DB
    if not os.path.exists(db_path):
        raise SystemExit(f"❌ 找不到数据库: {db_path}")

    print(f"数据库: {db_path}")
    print(OSS_NOTICE)
    print()
    total, bad, shapes = survey(db_path)
    print(f"总条目 {total}，值非 object 的坏条目 {len(bad)} 条")
    if not bad:
        print("✅ 无需清理。")
        return 0
    print("\n按 (命名空间, hash 长度, 值类型) 分组：")
    for (ns, hlen, tname), n in sorted(shapes.items(), key=lambda x: -x[1]):
        kind = {32: "MD5", 64: "SHA-256"}.get(hlen, f"len={hlen}")
        print(f"  {ns:<10} {kind:<8} {tname:<12} {n:>5}")
    print("\n样例（最多 5 条）：")
    for rowid, k, v in bad[:5]:
        print(f"  rowid={rowid:<6} {k}  ->  {v!r}")

    if not args.apply:
        print("\n[DRY-RUN] 未删除任何行。加 --apply --db <路径> 才会真正删除。")
        return 0

    print("\n[APPLY] 备份中…")
    backup_and_verify(db_path)
    deleted = purge(db_path, [r for r, _, _ in bad])
    total_after, bad_after, _ = survey(db_path)
    print(f"  已删除 {deleted} 行；现存 {total_after} 条，剩余坏条目 {len(bad_after)} 条")
    if bad_after:
        print("  ⚠️ 仍有坏条目残留，请复查。")
        return 2
    print("✅ 清理完成。")
    print()
    print(OSS_NOTICE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
