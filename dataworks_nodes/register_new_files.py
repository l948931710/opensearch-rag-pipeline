# -*- coding: utf-8 -*-
"""
注册 OSS 新文件到 RDS (PyODPS 节点)

功能：
  1. 扫描 OSS raw/ 下所有文件
  2. 对比 RDS，找出未注册的文件
  3. 为每个新文件创建 document_meta + document_version 记录

安全模式：DRY_RUN = True 时只报告不修改
"""
import subprocess, sys, os, hashlib, time, random
from datetime import datetime

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
DRY_RUN = True  # True = 只报告不修改; False = 实际写入 RDS

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

# 部门映射：OSS 路径前缀 → 部门代码
DEPT_MAP = {
    "raw/admin/": "ADMIN",
    "raw/hr/": "HR",
    "raw/it/": "IT",
    "raw/production/": "PRODUCTION",
    "raw/production_thermoforming/": "PRODUCTION",
    "raw/production_injection/": "PRODUCTION",
    "raw/production_papercup/": "PRODUCTION",
    "raw/finance/": "FINANCE",
    "raw/quality/": "QUALITY",
    "raw/sales/": "SALES",
    "raw/logistics/": "LOGISTICS",
}

def resolve_dept(raw_key):
    """从 raw_key 路径推断部门"""
    for prefix, dept in sorted(DEPT_MAP.items(), key=lambda x: -len(x[0])):
        if raw_key.startswith(prefix):
            return dept
    # 默认用第二级目录名
    parts = raw_key.split("/")
    if len(parts) >= 2:
        return parts[1].upper()
    return "UNKNOWN"

def generate_doc_id(dept):
    """生成唯一 doc_id: DOC_{DEPT}_{timestamp}_{random_hex}"""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    rand = "%06X" % random.randint(0, 0xFFFFFF)
    return f"DOC_{dept}_{ts}_{rand}"

# ═══════════════════════════════════════════════════════════════
# 2. 扫描 OSS
# ═══════════════════════════════════════════════════════════════
import oss2

print("=" * 60)
print("  OSS 新文件注册工具")
print(f"  模式: {'🔍 预览 (DRY_RUN)' if DRY_RUN else '⚡ 实际执行'}")
print("=" * 60)

print("\n📂 扫描 OSS raw/ 目录...")
auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
bucket = oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET_NAME)

oss_files = {}  # raw_key -> file_size

# ── 摄取准入策略 ──
# 单一来源是 opensearch_pipeline/ingest_policy.py；本脚本作为 PyODPS 独立节点
# 运行时无该包，使用内联副本（⚠️ 修改时两处同步；tests/test_ingest_generalization.py
# 的 parity test 会用 AST 抽取本副本与正本逐键对拍，单边改动会挂 CI）。
INGEST_POLICY_REV = "2026-06-10"   # 节点日志可见，核对线上跑的是哪一版策略
try:
    from opensearch_pipeline.ingest_policy import should_ingest_raw_key
    print("   [ingest-policy] using opensearch_pipeline.ingest_policy (rev %s)" % INGEST_POLICY_REV)
except ImportError:
    print("   [ingest-policy] using inline fallback copy (rev %s)" % INGEST_POLICY_REV)
    _IGNORED_PREFIXES = ("~$", ".~")
    _IGNORED_BASENAMES = {"thumbs.db", ".ds_store", "desktop.ini"}
    _IGNORED_EXTS = {"db", "tmp", "lnk", "exe", "dll",
                     "mp4", "avi", "mov", "wmv", "mp3", "wav",
                     "zip", "rar", "7z", "tar", "gz"}
    _LEGACY_EXTS = {"doc", "xls", "ppt"}
    _INGESTABLE_EXTS = {"pdf", "docx", "xlsx", "pptx",
                        "txt", "md", "csv", "html", "htm",
                        "png", "jpg", "jpeg", "webp", "tif", "tiff", "gif", "bmp"}

    def should_ingest_raw_key(key):
        if not key or key.endswith("/"):
            return False, "directory"
        if "/_quarantine/" in key or key.startswith(("raw/_quarantine/", "_quarantine/")):
            return False, "excluded path (_quarantine)"
        if "/_archive/" in key or key.startswith(("raw/_archive/", "_archive/")):
            return False, "excluded path (_archive)"
        base = os.path.basename(key).lower()
        if any(base.startswith(p) for p in _IGNORED_PREFIXES):
            return False, "temp file (~$)"
        if base in _IGNORED_BASENAMES:
            return False, "junk file"
        ext = os.path.splitext(base)[1].lstrip(".")
        if not ext:
            return False, "no extension"
        if ext in _IGNORED_EXTS:
            return False, "ignored ext (.%s)" % ext
        if ext in _LEGACY_EXTS:
            return False, "unsupported legacy ext (.%s)" % ext
        if ext not in _INGESTABLE_EXTS:
            return False, "unknown ext (.%s)" % ext
        return True, ""

skip_stats = {}
for obj in oss2.ObjectIteratorV2(bucket, prefix=OSS_RAW_PREFIX):
    key = obj.key
    ok, reason = should_ingest_raw_key(key)
    if not ok:
        skip_stats[reason] = skip_stats.get(reason, 0) + 1
        continue
    oss_files[key] = obj.size

print(f"   ✅ OSS 发现 {len(oss_files)} 个可摄取文件")
if skip_stats:
    print("   ⏭️ 跳过统计:")
    for reason, n in sorted(skip_stats.items(), key=lambda kv: -kv[1]):
        print(f"      {n:5d} × {reason}")

# ═══════════════════════════════════════════════════════════════
# 3. 查 RDS 已注册记录
# ═══════════════════════════════════════════════════════════════
import pymysql

print("\n📋 查询 RDS 已注册记录...")
conn = pymysql.connect(
    host=RDS_HOST, port=RDS_PORT,
    user=RDS_USER, password=RDS_PASSWORD,
    database=RDS_DATABASE, charset="utf8mb4"
)

with conn.cursor() as cursor:
    cursor.execute("SELECT raw_key FROM document_version")
    existing_keys = set(row[0] for row in cursor.fetchall())

print(f"   ✅ RDS 已有 {len(existing_keys)} 条记录")

# ═══════════════════════════════════════════════════════════════
# 4. 找出新文件
# ═══════════════════════════════════════════════════════════════
new_files = []
for key, size in sorted(oss_files.items()):
    if key not in existing_keys:
        new_files.append((key, size))

print(f"\n🆕 发现 {len(new_files)} 个未注册文件")

if not new_files:
    print("\n✅ 没有需要注册的新文件！")
    conn.close()
    sys.exit(0)

# 按部门统计
dept_counts = {}
for key, size in new_files:
    dept = resolve_dept(key)
    dept_counts[dept] = dept_counts.get(dept, 0) + 1

print("\n  按部门分布:")
for dept, cnt in sorted(dept_counts.items()):
    print(f"    {dept}: {cnt}")

# 按文件类型统计
ext_counts = {}
for key, size in new_files:
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else "unknown"
    ext_counts[ext] = ext_counts.get(ext, 0) + 1

print("\n  按文件类型分布:")
for ext, cnt in sorted(ext_counts.items()):
    print(f"    .{ext}: {cnt}")

# 预览前 20 条
print(f"\n── 前 20 条新文件 ──")
for key, size in new_files[:20]:
    dept = resolve_dept(key)
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else "unknown"
    print(f"  [{dept}] {os.path.basename(key)} ({size} bytes)")
if len(new_files) > 20:
    print(f"  ... 还有 {len(new_files) - 20} 个")

# ═══════════════════════════════════════════════════════════════
# 5. 注册新文件
# ═══════════════════════════════════════════════════════════════
if DRY_RUN:
    print(f"\n💡 DRY_RUN 模式，未执行修改。改为 DRY_RUN = False 后重跑即可实际注册。")
    conn.close()
    print("\n✅ 完成！")
    sys.exit(0)

print(f"\n⚡ 正在注册 {len(new_files)} 个新文件...")
registered = 0
errors = 0

with conn.cursor() as cursor:
    for key, size in new_files:
        try:
            dept = resolve_dept(key)
            filename = os.path.basename(key)
            ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
            doc_id = generate_doc_id(dept)
            raw_key_hash = hashlib.sha256(key.encode()).hexdigest()
            
            # 避免极小概率的 doc_id 冲突
            time.sleep(0.01)
            
            # 插入 document_meta
            cursor.execute("""
                INSERT INTO document_meta (doc_id, title, owner_dept, source_system, gate_status)
                VALUES (%s, %s, %s, 'oss_scan', 'pending_clean')
                ON DUPLICATE KEY UPDATE title = VALUES(title)
            """, (doc_id, filename, dept))
            
            # 插入 document_version
            cursor.execute("""
                INSERT INTO document_version (
                    doc_id, version_no, bucket_name, raw_key, raw_key_hash,
                    file_ext, gate_status, content_process_status,
                    extraction_status, index_status, status
                ) VALUES (
                    %s, 1, %s, %s, %s,
                    %s, 'pending_clean', 'NOT_STARTED',
                    'NOT_STARTED', 'NOT_INDEXED', 'active'
                )
            """, (doc_id, OSS_BUCKET_NAME, key, raw_key_hash, ext))
            
            registered += 1
            if registered % 20 == 0:
                conn.commit()
                print(f"   ... 已注册 {registered}/{len(new_files)}")
                
        except Exception as e:
            errors += 1
            print(f"   ⚠️ 注册失败 {key}: {e}")

conn.commit()
conn.close()

print(f"\n   ✅ 成功注册: {registered}")
if errors:
    print(f"   ⚠️ 失败: {errors}")
print("\n✅ 完成！")
