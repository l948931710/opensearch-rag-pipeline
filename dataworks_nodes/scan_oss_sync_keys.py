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
import subprocess
import sys
import os

# ═══════════════════════════════════════════════════════════════
# 0. 安装依赖
# ═══════════════════════════════════════════════════════════════
# py3.7 serverless 执行器钉真兼容版（批次9 同族清扫，同 stage3_node 分支法）
if sys.version_info >= (3, 8):
    DEPS = ["PyMySQL", "DBUtils", "oss2"]
else:
    DEPS = ["PyMySQL==1.1.1", "DBUtils==3.1.2", "oss2==2.19.1"]

def ensure_deps():
    dep_dir = "/tmp/pydeps"
    try:
        import pymysql  # noqa: F401  仅探测依赖是否可用
        import oss2     # noqa: F401
        return
    except ImportError:
        pass
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        *DEPS, "-t", dep_dir, "-q"
    ])
    subprocess.call([sys.executable, "-m", "pip", "freeze", "--path", dep_dir])   # δ3（M13）：传递闭包可见化（冻结回填 user-gated）
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

# 部门映射：OSS 路径前缀 → 部门代码（与 register_new_files.py 保持一致）
#F-oss-raw-key 用于校验按文件名匹配到的候选 key 与 doc 的 owner_dept 同部门
DEPT_MAP = {
    "raw/admin/": "ADMIN",
    "raw/hr/": "HR",
    "raw/it/": "IT",
    "raw/production/": "PRODUCTION",
    # ── production 家族：子线目录 → 子线值（2026-07-17 落地「增量分流」拍板）──
    # owner_dept 永不归一到伞值；读侧由 production 伞形白名单展开放行（retriever）。
    # 未来新子线目录（吹膜/纸箱/纸浆等）无需加映射：resolve_dept fallback 取第二级
    # 目录名即得子线值；此处显式列出仅为双拼写归一与可读性。
    "raw/production_mold/": "PRODUCTION_MOLD",
    "raw/production_thermoforming/": "PRODUCTION_THERMOFORMING",
    "raw/production_injection/": "PRODUCTION_INJECTION",
    "raw/production_straw/": "PRODUCTION_STRAW",
    "raw/production_paper_cup/": "PRODUCTION_PAPER_CUP",
    "raw/production_papercup/": "PRODUCTION_PAPER_CUP",  # OSS 双拼写目录归一到规范子线值
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


def _rds_ssl_kwargs():
    """P0-02/B3：显式 TLS 语义——配 RAG_RDS_SSL_CA 即验证 TLS，未配显式明文。
    不能留空 kwargs：RDS 服务端开 SSL 后 pymysql 2.x PREFERRED 会自动试握手且
    失败不回退，行为随客户端 OpenSSL 漂移（与 prod_access._connect 同语义）。"""
    ca = (os.environ.get("RAG_RDS_SSL_CA") or "").strip()
    if not ca:
        return {"ssl_disabled": True}
    verify = (os.environ.get("RAG_RDS_SSL_VERIFY_CERT", "true").strip().lower()
              not in ("0", "false", "no"))
    return {"ssl_ca": ca, "ssl_verify_cert": verify, "ssl_verify_identity": verify}
    # ⚠️ 只用顶层 ssl_* 参数,绝不与 ssl={...} 字典混传:pymysql 见任一顶层参数为真即用
    # 顶层参数【重建】ssl 配置并丢弃字典(ca 变 None→退到系统信任库,ApsaraDB 链必挂
    # "unable to get local issuer";2026-07-21 SAE 首开 CA 实弹踩坑)。


print("\n📋 查询 RDS document_version 记录...")
conn = pymysql.connect(
    host=RDS_HOST, port=RDS_PORT,
    user=RDS_USER, password=RDS_PASSWORD,
    database=RDS_DATABASE, charset="utf8mb4", **_rds_ssl_kwargs()
)

with conn.cursor() as cursor:
    # 🔴 C8 §4.3（Sam 2026-08-04 拍板「排除已绑定版本」）：本工具会把既有 raw_key 改写到
    # 另一个 OSS 对象，**却不更新 ETag / version-id**。对内容已绑定的版本，那等于
    # 「把同一个审批过的版本悄悄指向另一份字节」—— 正是 C8 要修的攻击面，只不过换成
    # 我们自己动手。故已绑定版本一律**跳过**并单独列出，交人工按「重新上传成新版本」处理。
    # 064 未 apply 的环境无该列 ⇒ 回退旧查询（行为逐字节不变）。
    _bind_ok = False
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='document_version' "
            "AND COLUMN_NAME='content_binding_mode'")
        _r = cursor.fetchone()
        _bind_ok = bool(_r and _r[0])
    except Exception:      # noqa: BLE001 — 探测失败 ⇒ 当未 apply
        _bind_ok = False
    _bind_col = ", dv.content_binding_mode" if _bind_ok else ", NULL"
    cursor.execute(f"""
        SELECT dv.doc_id, dv.version_no, dv.raw_key, dv.file_ext, dm.owner_dept{_bind_col}
        FROM document_version dv
        LEFT JOIN document_meta dm ON dv.doc_id = dm.doc_id
        WHERE dv.status = 'active'
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

bound_skipped = []   # C8：内容已绑定，禁止改写 raw_key

for doc_id, version_no, raw_key, file_ext, owner_dept, _bmode in db_records:
    db_raw_keys.add(raw_key)

    # C8 §4.3：已绑定版本绝不改写（改写=把审批过的版本指向另一份字节）。
    # 放在最前：连"存在性匹配"的统计都不参与，避免它被计进 matched_ok 掩盖掉。
    if str(_bmode or "") == "VERSION_ID":
        bound_skipped.append((doc_id, version_no, raw_key))
        continue

    if raw_key in oss_files:
        matched_ok += 1
        continue
    
    # raw_key 不存在，尝试按文件名匹配
    basename = os.path.basename(raw_key) if raw_key else ""
    candidates = oss_by_name.get(basename, [])
    #F-oss-raw-key 仅接受与该 doc owner_dept 同部门的候选：候选 key 经 resolve_dept
    # 推断的部门须与 owner_dept 一致，才允许改写；否则会把 raw_key 指向他部门同名
    # 文件 → 抽取/索引到错配内容且越权可见。跨部门/不唯一/owner_dept 缺失一律降级
    # 人工（ambiguous），绝不静默改写。
    want_dept = (owner_dept or "").strip().lower()
    dept_ok = [c for c in candidates if resolve_dept(c).strip().lower() == want_dept]
    
    if want_dept and len(dept_ok) == 1:
        needs_update.append((doc_id, version_no, raw_key, dept_ok[0]))
    elif candidates:
        # 有同名候选但部门不唯一/不匹配（含 owner_dept 缺失）→ 歧义，交人工
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
    print("\n── OSS 新文件 (未注册) 前 20 条 ──")
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
                # 批次8（ultra scan_oss_sync_keys:253）：status-only 退役留双活——本记录的
                # chunk_meta 仍 is_active=1/INDEXED、HA3 PK 仍在线，检索永远同时返回两个
                # doc_id 且无任何 reconciler 覆盖此形态。复用 spot_checker 的 PENDING_DELETE
                # 握手：index_status 置 PENDING_DELETE（已在终态删除链上的不动），由
                # reconcile_pending_deletes（spot-check 启动自动跑/可独立调）删 HA3 PK +
                # 灭活 chunk_meta + dv 落 DELETED——与控制台退役同一条收敛路径。
                cursor.execute("""
                    UPDATE document_version
                    SET status = 'superseded',
                        index_status = CASE WHEN index_status IN ('DELETED', 'PENDING_DELETE')
                                            THEN index_status ELSE 'PENDING_DELETE' END
                    WHERE doc_id = %s AND version_no = %s
                """, (doc_id, version_no))
                deactivated += 1
                print(f"  🔄 {doc_id} → 停用+排 HA3 PENDING_DELETE (与 {existing[0]} 重复；"
                      f"reconcile_pending_deletes 收敛索引与 chunk_meta)")
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
    print("\n💡 DRY_RUN 模式，未执行修改。改为 DRY_RUN = False 后重跑即可实际更新。")


# C8 §4.3：内容已绑定的版本本轮被跳过 —— 单独列出交人工（绝不静默改写）。
if bound_skipped:
    print(f"\n🔒 C8 内容已绑定、跳过改写: {len(bound_skipped)} 条"
          f"（这些版本的字节已随审批固化；确需换文件请重新上传形成**新版本**并重新审批）")
    for _d, _v, _k in bound_skipped[:20]:
        print(f"     · {_d} v{_v}  {_k}")
    if len(bound_skipped) > 20:
        print(f"     … 另有 {len(bound_skipped) - 20} 条")

conn.close()
print("\n✅ 完成！")
