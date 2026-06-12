# -*- coding: utf-8 -*-
"""
注册 OSS 新文件到 RDS (PyODPS 节点)

功能：
  1. 扫描 OSS raw/ 下所有文件
  2. 对比 RDS，找出未注册的文件
  3. 为每个新文件创建 document_meta + document_version 记录

安全模式：DRY_RUN = True 时只报告不修改

变更 2026-06-11：新增注册侧同名(stem)防重 —— 同部门同名(去扩展名)已有 active
注册则跳过 + 告警，跨部门同名仅告警不拦截；含批内防重与 DRY_RUN 预览统计。
⚠️ 本文件是 DataWorks PyODPS 节点的粘贴源：下次发布窗口需把本文件重新粘贴到节点。
"""
import hashlib
import os
import random
import subprocess
import sys
import time
from datetime import datetime

# ═══════════════════════════════════════════════════════════════
# 0. 安装依赖
# ═══════════════════════════════════════════════════════════════
DEPS = ["PyMySQL", "DBUtils", "oss2"]

def ensure_deps():
    dep_dir = "/tmp/pydeps"
    try:
        import oss2     # noqa: F401  仅探测依赖是否可用
        import pymysql  # noqa: F401
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
# 0.5 PyODPS 节点凭证注入
# ═══════════════════════════════════════════════════════════════
# 本文件是 DataWorks PyODPS 节点的粘贴源，节点环境没有任何 RAG_* 变量。
# 粘贴进节点后：取消下面注释，两把 OSS key 和 RDS 密码从 清理stage3 节点
# 顶部的同名赋值复制（仓库内严禁写真值）。本地有环境变量的场景无需此块。
# os.environ["RAG_OSS_ACCESS_KEY_ID"]     = ""
# os.environ["RAG_OSS_ACCESS_KEY_SECRET"] = ""
# os.environ["RAG_RDS_HOST"]              = "rm-bp15j7wekd5738f09.rwlb.rds.aliyuncs.com"  # 内网地址
# os.environ["RAG_RDS_USER"]              = "fuling_admin"
# os.environ["RAG_RDS_PASSWORD"]          = ""

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
    "raw/production_paper_cup/": "PRODUCTION",  # OSS 实际目录拼写（带下划线），与上行双拼写并存
    "raw/marketing/": "MARKETING",
    "raw/pmc/": "PMC",
    "raw/rd/": "RD",
    "raw/supply/": "SUPPLY",
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
import oss2  # noqa: E402  PyODPS 节点须先 ensure_deps() 再导入

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
INGEST_POLICY_REV = "2026-06-11"   # 节点日志可见，核对线上跑的是哪一版策略
try:
    from opensearch_pipeline.ingest_policy import (
        raw_key_stem,
        should_ingest_raw_key,
        stem_twin_action,
    )
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

    # ── 同名(stem)防重（与正本 ingest_policy.py 逐字符一致，parity test 对拍） ──
    def raw_key_stem(key: str) -> str:
        """basename 去掉最后一层扩展名（只去一层："a.b.docx" → "a.b"；无扩展名原样返回）。"""
        base = os.path.basename(key or "")
        return os.path.splitext(base)[0].strip()

    def stem_twin_action(dept: str, stem: str, existing: dict) -> tuple:
        """同名（同 stem）注册防重裁决。existing 形如 {stem: set(已注册部门小写)}。

        Returns:
            ("skip", reason)  同部门（大小写不敏感）已有 active 同名注册 → 跳过（防孪生 doc_id）；
            ("warn", reason)  仅异部门已有同名 → 告警不拦截（归属是 ACL 问题，防重不替它决定）；
            ("ok", "")        无同名注册。reason 为中文，列出已注册部门。
        """
        if not stem:
            return "ok", ""
        depts = existing.get(stem) or set()
        if not depts:
            return "ok", ""
        dept_l = (dept or "").strip().lower()
        depts_l = sorted({str(d).strip().lower() for d in depts})
        listed = ", ".join(depts_l)
        if dept_l in depts_l:
            return "skip", f"已有: {listed}"
        return "warn", f"已有: {listed}"

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
import pymysql  # noqa: E402  PyODPS 节点须先 ensure_deps() 再导入

print("\n📋 查询 RDS 已注册记录...")
conn = pymysql.connect(
    host=RDS_HOST, port=RDS_PORT,
    user=RDS_USER, password=RDS_PASSWORD,
    database=RDS_DATABASE, charset="utf8mb4"
)

with conn.cursor() as cursor:
    cursor.execute("SELECT raw_key FROM document_version")
    existing_keys = set(row[0] for row in cursor.fetchall())

    # ── 防重：active 注册的同名(stem)→部门映射（raw_key 精确比对抓不住
    # 同名异扩展/换路径重传，它们会注册成孪生 doc_id）──
    cursor.execute("""
        SELECT dv.raw_key, dm.owner_dept
        FROM document_version dv
        JOIN document_meta dm ON dv.doc_id = dm.doc_id
        WHERE dv.status = 'active' AND dm.status = 'active'
    """)
    existing_stem_depts = {}  # stem -> set(部门小写)
    for raw_key, owner_dept in cursor.fetchall():
        stem = raw_key_stem(raw_key or "")
        if not stem:
            continue
        existing_stem_depts.setdefault(stem, set()).add((owner_dept or "").strip().lower())

print(f"   ✅ RDS 已有 {len(existing_keys)} 条记录（active 同名防重映射 {len(existing_stem_depts)} 个 stem）")

# ═══════════════════════════════════════════════════════════════
# 4. 找出新文件
# ═══════════════════════════════════════════════════════════════
new_files = []
for key, size in sorted(oss_files.items()):
    if key not in existing_keys:
        new_files.append((key, size))

# 批内孪生裁决顺序：同 (部门, stem) 的双格式同批上传时，先注册 .pdf——
# 字典序 'docx' < 'pdf' 会让 docx 先注册、pdf 被防重跳过，与转换对
# 「一般留 pdf（布局+页码）」的既定取舍相反（2026-06-11 对抗评审）。
new_files.sort(key=lambda ks: (
    resolve_dept(ks[0]).lower(), raw_key_stem(ks[0]),
    0 if ks[0].lower().endswith(".pdf") else 1, ks[0]))

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
print("\n── 前 20 条新文件 ──")
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
    # ── 防重预览（与实际注册同一裁决逻辑，含批内防重）──
    preview_map = {s: set(d) for s, d in existing_stem_depts.items()}
    would_skip, would_warn = [], []
    for key, size in new_files:
        dept_l = resolve_dept(key).lower()
        stem = raw_key_stem(key)
        action, reason = stem_twin_action(dept_l, stem, preview_map)
        if action == "skip":
            would_skip.append((key, reason))
            continue
        if action == "warn":
            would_warn.append((key, reason))
        if stem:  # 视为将注册成功 → 批内后续同名同部门可被识别
            preview_map.setdefault(stem, set()).add(dept_l)
    print(f"\n🛡️ 防重预览: 同部门同名将跳过 {len(would_skip)} 个，跨部门同名将告警 {len(would_warn)} 个")
    for key, reason in would_skip[:10]:
        print(f"   ⏭️ [防重] 同部门同名已注册，将跳过: {key}（{reason}）")
    if len(would_skip) > 10:
        print(f"   ... 还有 {len(would_skip) - 10} 个")
    for key, reason in would_warn[:10]:
        print(f"   ⚠️ [防重] 跨部门同名已注册，仍会注册: {key}（{reason}）")
    if len(would_warn) > 10:
        print(f"   ... 还有 {len(would_warn) - 10} 个")

    print("\n💡 DRY_RUN 模式，未执行修改。改为 DRY_RUN = False 后重跑即可实际注册。")
    conn.close()
    print("\n✅ 完成！")
    sys.exit(0)

print(f"\n⚡ 正在注册 {len(new_files)} 个新文件...")
registered = 0
errors = 0
twin_skipped = 0   # 防重：同部门同名跳过
twin_warned = 0    # 防重：跨部门同名告警（仍注册）

with conn.cursor() as cursor:
    for key, size in new_files:
        try:
            dept = resolve_dept(key)

            # ── 注册防重：同部门同 stem（同名异扩展/换路径重传）跳过，防孪生 doc_id；
            # 跨部门同 stem 仅告警（归属是 ACL 问题，防重不替它做决定）──
            stem = raw_key_stem(key)
            action, reason = stem_twin_action(dept.lower(), stem, existing_stem_depts)
            if action == "skip":
                twin_skipped += 1
                print(f"   ⏭️ [防重] 同部门同名已注册，跳过: {key}（{reason}）")
                continue
            if action == "warn":
                twin_warned += 1
                print(f"   ⚠️ [防重] 跨部门同名已注册，仍注册: {key}（{reason}）")

            filename = os.path.basename(key)
            ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
            doc_id = generate_doc_id(dept)
            raw_key_hash = hashlib.sha256(key.encode()).hexdigest()
            
            # 避免极小概率的 doc_id 冲突
            time.sleep(0.01)
            
            # 插入 document_meta —— 列严格对齐生产表（无 source_system/gate_status 两列，
            # 2026-06-11 实测 1054 报错）；owner_dept 跟存量行一致用小写；status/permission_level/
            # kb_type 显式写存量惯例值（chunk 级权限仍由处理期路径启发式决定，此处仅元数据）
            cursor.execute("""
                INSERT INTO document_meta (doc_id, title, owner_dept, status, permission_level, kb_type)
                VALUES (%s, %s, %s, 'active', 'public', 'public')
                ON DUPLICATE KEY UPDATE title = VALUES(title)
            """, (doc_id, filename, dept.lower()))
            
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

            # 批内防重：注册成功即回填 (stem, dept)，本批后续同名同部门文件同样被跳过
            if stem:
                existing_stem_depts.setdefault(stem, set()).add(dept.lower())

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
if twin_skipped:
    print(f"   ⏭️ 防重跳过（同部门同名）: {twin_skipped}")
if twin_warned:
    print(f"   ⚠️ 防重告警（跨部门同名，已注册）: {twin_warned}")
if errors:
    print(f"   ⚠️ 失败: {errors}")
print("\n✅ 完成！")
