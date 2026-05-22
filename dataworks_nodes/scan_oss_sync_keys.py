# -*- coding: utf-8 -*-
"""
OSS → RDS raw_key 同步脚本 (PyODPS 节点)

功能：
  1. 扫描 OSS bucket 下 raw/ 目录的所有文件
  2. 对比 RDS document_version 中的 raw_key
  3. 按文件名匹配，自动修复路径不一致的记录
  4. 报告新发现的文件（OSS 有但 RDS 没注册）

安全模式：DRY_RUN = True 时只报告不修改
"""
import subprocess, sys, os

# ═══════════════════════════════════════════════════════════════
# 0. 安装依赖
# ═══════════════════════════════════════════════════════════════
DEPS = ["PyMySQL", "DBUtils", "oss2"]

def ensure_deps():
    dep_dir = "/tmp/pydeps"
    try:
        import pymysql, oss2
        return
    except ImportError:
        pass
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        *DEPS, "-t", dep_dir, "-q"
    ])
    if dep_dir not in sys.path:
        sys.path.insert(0, dep_dir)

ensure_deps()

# ═══════════════════════════════════════════════════════════════
# 1. 配置
# ═══════════════════════════════════════════════════════════════
DRY_RUN = True  # True = 只报告不修改; False = 实际更新 RDS

OSS_ENDPOINT        = os.environ.get("RAG_OSS_ENDPOINT", "https://oss-cn-hangzhou-internal.aliyuncs.com")
OSS_ACCESS_KEY_ID   = os.environ["RAG_OSS_ACCESS_KEY_ID"]
OSS_ACCESS_KEY_SECRET = os.environ["RAG_OSS_ACCESS_KEY_SECRET"]
OSS_BUCKET_NAME     = os.environ.get("RAG_OSS_BUCKET_NAME", "fuling-knowledge-base")
OSS_RAW_PREFIX      = "raw/"

RDS_HOST     = os.environ.get("RAG_RDS_HOST", "localhost")
RDS_PORT     = int(os.environ.get("RAG_RDS_PORT", "3306"))
RDS_USER     = os.environ.get("RAG_RDS_USER", "root")
RDS_PASSWORD = os.environ["RAG_RDS_PASSWORD"]
RDS_DATABASE = os.environ.get("RAG_RDS_DATABASE", "fuling_knowledge")

# ═══════════════════════════════════════════════════════════════
# 2. 扫描 OSS
# ═══════════════════════════════════════════════════════════════
import oss2
from collections import defaultdict

print("=" * 60)
print("  OSS → RDS raw_key 同步工具")
print(f"  模式: {'🔍 预览 (DRY_RUN)' if DRY_RUN else '⚡ 实际执行'}")
print("=" * 60)

print("\n📂 扫描 OSS raw/ 目录...")
auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
bucket = oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET_NAME)

# 收集所有 OSS 文件: { basename -> [full_key, ...] }
oss_files = {}           # full_key -> file_size
oss_by_name = defaultdict(list)  # basename -> [full_key, ...]

for obj in oss2.ObjectIteratorV2(bucket, prefix=OSS_RAW_PREFIX):
    key = obj.key
    if key.endswith("/"):
        continue  # 跳过目录
    if "/_quarantine/" in key or key.startswith("raw/_quarantine/"):
        continue  # 跳过隔离区
    size = obj.size
    basename = os.path.basename(key)
    oss_files[key] = size
    oss_by_name[basename].append(key)

print(f"   ✅ 发现 {len(oss_files)} 个文件")

# ═══════════════════════════════════════════════════════════════
# 3. 查 RDS 记录
# ═══════════════════════════════════════════════════════════════
import pymysql

print("\n📋 查询 RDS document_version 记录...")
conn = pymysql.connect(
    host=RDS_HOST, port=RDS_PORT,
    user=RDS_USER, password=RDS_PASSWORD,
    database=RDS_DATABASE, charset="utf8mb4"
)

with conn.cursor() as cursor:
    cursor.execute("""
        SELECT doc_id, version_no, raw_key, file_ext
        FROM document_version
        WHERE status = 'active'
    """)
    db_records = cursor.fetchall()

print(f"   ✅ 找到 {len(db_records)} 条活跃记录")

# ═══════════════════════════════════════════════════════════════
# 4. 对比分析
# ═══════════════════════════════════════════════════════════════
print("\n🔍 对比分析...")

matched_ok = 0       # raw_key 在 OSS 上存在
needs_update = []    # raw_key 不在 OSS，但按文件名找到了新路径
not_found = []       # raw_key 不在 OSS，文件名也匹配不到
ambiguous = []       # 文件名匹配到多个 OSS 路径

db_raw_keys = set()

for doc_id, version_no, raw_key, file_ext in db_records:
    db_raw_keys.add(raw_key)
    
    if raw_key in oss_files:
        matched_ok += 1
        continue
    
    # raw_key 不存在，尝试按文件名匹配
    basename = os.path.basename(raw_key) if raw_key else ""
    candidates = oss_by_name.get(basename, [])
    
    if len(candidates) == 1:
        needs_update.append((doc_id, version_no, raw_key, candidates[0]))
    elif len(candidates) > 1:
        ambiguous.append((doc_id, version_no, raw_key, candidates))
    else:
        not_found.append((doc_id, version_no, raw_key, file_ext))

# 新文件：OSS 上有但 RDS 没注册
new_files = []
for key in oss_files:
    if key not in db_raw_keys:
        # 检查是否有按文件名匹配过的
        new_files.append(key)

# ═══════════════════════════════════════════════════════════════
# 5. 报告
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  📊 分析报告")
print("=" * 60)

print(f"\n  ✅ 路径匹配正常: {matched_ok}")
print(f"  🔄 可自动修复 (唯一匹配): {len(needs_update)}")
print(f"  ⚠️  多路径歧义: {len(ambiguous)}")
print(f"  ❌ 完全找不到: {len(not_found)}")
print(f"  🆕 OSS 新文件 (未注册): {len(new_files)}")

if needs_update:
    print(f"\n── 可自动修复的 {len(needs_update)} 条 ──")
    for doc_id, ver, old_key, new_key in needs_update[:20]:
        print(f"  {doc_id}: {old_key}")
        print(f"         → {new_key}")
    if len(needs_update) > 20:
        print(f"  ... 还有 {len(needs_update) - 20} 条")

if ambiguous:
    print(f"\n── 歧义的 {len(ambiguous)} 条 (需手动处理) ──")
    for doc_id, ver, old_key, candidates in ambiguous[:10]:
        print(f"  {doc_id}: {old_key}")
        for c in candidates:
            print(f"         ? {c}")

if not_found:
    print(f"\n── 完全找不到的 {len(not_found)} 条 ──")
    for doc_id, ver, raw_key, ext in not_found[:20]:
        print(f"  {doc_id} (.{ext}): {raw_key}")
    if len(not_found) > 20:
        print(f"  ... 还有 {len(not_found) - 20} 条")

if new_files:
    print(f"\n── OSS 新文件 (未注册) 前 20 条 ──")
    for key in sorted(new_files)[:20]:
        print(f"  🆕 {key}")
    if len(new_files) > 20:
        print(f"  ... 还有 {len(new_files) - 20} 个")

# ═══════════════════════════════════════════════════════════════
# 6. 执行修复 (非 DRY_RUN 模式)
# ═══════════════════════════════════════════════════════════════
if not DRY_RUN and needs_update:
    import hashlib
    updated = 0
    deactivated = 0
    print(f"\n⚡ 正在处理 {len(needs_update)} 条 raw_key...")
    with conn.cursor() as cursor:
        for doc_id, version_no, old_key, new_key in needs_update:
            new_hash = hashlib.sha256(new_key.encode()).hexdigest()
            
            # 检查目标 raw_key_hash 是否已被其他活跃记录占用
            cursor.execute("""
                SELECT doc_id, version_no FROM document_version
                WHERE raw_key_hash = %s AND status = 'active'
            """, (new_hash,))
            existing = cursor.fetchone()
            
            if existing:
                # 目标路径已有记录 → 当前记录是重复的，停用
                cursor.execute("""
                    UPDATE document_version
                    SET status = 'superseded'
                    WHERE doc_id = %s AND version_no = %s
                """, (doc_id, version_no))
                deactivated += 1
                print(f"  🔄 {doc_id} → 停用 (与 {existing[0]} 重复)")
            else:
                # 正常更新路径
                cursor.execute("""
                    UPDATE document_version
                    SET raw_key = %s, raw_key_hash = %s
                    WHERE doc_id = %s AND version_no = %s
                """, (new_key, new_hash, doc_id, version_no))
                updated += 1
    conn.commit()
    print(f"\n   ✅ 更新路径: {updated} 条")
    print(f"   🔄 停用重复: {deactivated} 条")
elif DRY_RUN and needs_update:
    print(f"\n💡 DRY_RUN 模式，未执行修改。改为 DRY_RUN = False 后重跑即可实际更新。")

conn.close()
print("\n✅ 完成！")
