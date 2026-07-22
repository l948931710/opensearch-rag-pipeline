import subprocess
import sys
import os
# py3.7 陷阱（批次9 S5/S6，同 stage3_node）: serverless 执行器实为 Python 3.7，
# pypdf 5.0.0 元数据谎报支持 3.7 实则用 typing.Protocol(3.8+) → 必须钉真兼容版。
# 镜像恢复 3.8+ 后自动走现代分支，本段无需再改。
if sys.version_info >= (3, 8):
    DEPS = [
        "PyMySQL", "DBUtils", "oss2", "requests",
        "alibabacloud_ha3engine_vector",
        "pdfplumber", "pypdf",
    ]
else:
    DEPS = [
        "PyMySQL==1.1.1", "DBUtils==3.1.2", "oss2==2.19.1", "requests==2.31.0",
        "alibabacloud_ha3engine_vector==1.1.19",
        "typing_extensions==4.7.1",
        "pypdf==3.17.4", "pdfplumber==0.9.0", "Pillow==9.5.0",
    ]
subprocess.check_call([
    sys.executable, "-m", "pip", "install",
    *DEPS, "-t", "/tmp/pydeps", "-q"
])
subprocess.call([sys.executable, "-m", "pip", "freeze", "--path", "/tmp/pydeps"])   # δ3（M13）：传递闭包可见化（冻结回填 user-gated）
if "/tmp/pydeps" not in sys.path:
    sys.path.insert(0, "/tmp/pydeps")

# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — Stage 3: Chunks → OpenSearch Index
包含 HA3 旧数据清理 + 全量推送

凭证来源：
  - DataWorks 节点参数（生产）
  - .env.local 文件（本地测试）
  - 绝不在源代码中硬编码密钥
"""
import sys
import zipfile

# ── 本地测试时加载 .env.local ──
try:
    from dotenv import load_dotenv
    # 优先加载 .env.local（含生产凭证），再加载 .env（通用配置）
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env.local'), override=False)
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'), override=False)
except ImportError:
    pass  # DataWorks 环境无 dotenv，凭证由节点参数注入

SIMULATE = False

os.environ["RAG_SIMULATE"] = str(SIMULATE).lower()
os.environ["RAG_ENVIRONMENT"] = "production"

# ── 生产模式：校验必要凭证 ──
if not SIMULATE:
    _REQUIRED_KEYS = [
        "DASHSCOPE_API_KEY",
        "RAG_RDS_HOST", "RAG_RDS_USER", "RAG_RDS_PASSWORD", "RAG_RDS_DATABASE",
        "RAG_OSS_ENDPOINT", "RAG_OSS_ACCESS_KEY_ID", "RAG_OSS_ACCESS_KEY_SECRET",
        "RAG_HA3_ENDPOINT", "RAG_HA3_INSTANCE_ID", "RAG_HA3_USER", "RAG_HA3_PASSWORD",
    ]
    _missing = [k for k in _REQUIRED_KEYS if not os.environ.get(k)]
    if _missing:
        print(f"🚨 缺少必要环境变量: {', '.join(_missing)}")
        print("   请通过 DataWorks 节点参数或 .env.local 文件配置凭证")
        sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# 0. 清理 HA3 旧 chunk（同版本重建专用，一次性步骤）
# ═══════════════════════════════════════════════════════════════
print("=== 0. 清理 HA3 旧 chunk ===")
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


conn = pymysql.connect(
    host=os.environ["RAG_RDS_HOST"],
    port=int(os.environ.get("RAG_RDS_PORT", "3306")),
    user=os.environ["RAG_RDS_USER"],
    password=os.environ["RAG_RDS_PASSWORD"],
    database=os.environ["RAG_RDS_DATABASE"],
    charset="utf8mb4", **_rds_ssl_kwargs()
)
try:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM chunk_meta WHERE is_active = 0")
        old_ids = [r[0] for r in cur.fetchall()]
finally:
    conn.close()

print(f"   找到 {len(old_ids)} 个旧 chunk 需要从 HA3 删除")

if old_ids:
    from alibabacloud_ha3engine_vector.client import Client
    from alibabacloud_ha3engine_vector.models import Config as HA3Config, PushDocumentsRequest

    ha3_cfg = HA3Config(
        endpoint=os.environ["RAG_HA3_ENDPOINT"],
        instance_id=os.environ["RAG_HA3_INSTANCE_ID"],
        access_user_name=os.environ["RAG_HA3_USER"],
        access_pass_word=os.environ["RAG_HA3_PASSWORD"],
    )
    ha3_client = Client(ha3_cfg)
    ha3_table = os.environ.get("RAG_HA3_TABLE_NAME", "fuling_kb_chunks")

    batch_size = 100
    deleted = 0
    for i in range(0, len(old_ids), batch_size):
        batch = old_ids[i:i + batch_size]
        ha3_deletes = [{"cmd": "delete", "fields": {"id": rid}} for rid in batch]
        try:
            request = PushDocumentsRequest(body=ha3_deletes)
            resp = ha3_client.push_documents(ha3_table, "id", request)
            status_code = getattr(resp, "status_code", 200)
            if 200 <= status_code < 300:
                deleted += len(batch)
            else:
                body_msg = str(getattr(resp, "body", "")).lower()
                if "not_found" in body_msg or "not found" in body_msg:
                    deleted += len(batch)
                else:
                    print(f"   ⚠️ Batch {i//batch_size+1} status={status_code}")
                    deleted += len(batch)  # 继续处理，不中断
        except Exception as e:
            print(f"   ⚠️ Batch {i//batch_size+1} error: {e}")
    print(f"   ✅ 已从 HA3 删除 {deleted}/{len(old_ids)} 个旧 chunk")
else:
    print("   ✅ 无需清理")

# ═══════════════════════════════════════════════════════════════
# 1. 下载并解压代码包
# ═══════════════════════════════════════════════════════════════
print("=== 1. 下载 Archive 资源 ===")
resource = odps.get_resource('opensearch_pipeline_production.zip')
with resource.open(mode='rb') as reader:
    with open('opensearch_pipeline_production.zip', 'wb') as writer:
        writer.write(reader.read())

print("=== 2. 解压代码包 ===")
def _safe_extractall(zf, dest):
    """B4（生产级外审 2026-07-17 P1-02/P1-03）安全解压：Zip-Slip 越界 + 加密成员 +
    成员数/单成员/总量/压缩比预算——资源包被替换成恶意 archive 时，不能借解压写任意
    路径或耗尽磁盘/内存。预算≈生产包实测 10×：成员≤4000、单成员≤200MB、总量≤500MB、
    压缩比≤200:1。"""
    dest_root = os.path.abspath(dest)
    infos = zf.infolist()
    if len(infos) > 4000:
        raise RuntimeError("zip 成员数超预算: %d > 4000" % len(infos))
    total = 0
    for info in infos:
        name = info.filename
        target = os.path.abspath(os.path.join(dest_root, name))
        if not (target == dest_root or target.startswith(dest_root + os.sep)):
            raise RuntimeError("zip 成员越界（Zip-Slip）: %r" % name)
        if info.flag_bits & 0x1:
            raise RuntimeError("zip 加密成员（拒绝）: %r" % name)
        if info.file_size > 200 * 1024 * 1024:
            raise RuntimeError("zip 单成员超预算: %r（%d bytes）" % (name, info.file_size))
        if info.compress_size and info.file_size / float(info.compress_size) > 200:
            raise RuntimeError("zip 压缩比异常（疑似 zip-bomb）: %r" % name)
        total += info.file_size
    if total > 500 * 1024 * 1024:
        raise RuntimeError("zip 总解压量超预算: %d bytes" % total)
    zf.extractall(dest_root)


def _verify_zip_integrity(zip_name):
    """B4（P1-02）+δ3（M13，Majors 批次 δ，codex 共识 2026-07-21）制品完整性：算 zip
    sha256 并留痕；有 sidecar 资源 <zip_name>.sha256 时硬比对（不匹配=资源被替换，拒绝
    执行）；暂缺=默认过渡期放行（打包侧 deploy/build_dataworks_zip.sh 随包生成）。
    δ3 增量：①RAG_DW_SIDECAR_STRICT=on（调度 env）→ 缺 sidecar/不可读/无 HMAC key 均
    raise（默认 off=现状放行+显著警告）；②调度 env 配 RAG_DW_ZIP_HMAC_KEY → 对
    <zip_name>.sha256.hmac 硬验（信任边界分离：能替换资源者可连 sidecar 一起换，但读
    不到调度 env 就伪造不了签名；打包侧 --hmac 产第三份资源）。威胁模型边界（如实）：
    能改节点代码本身或能读调度 env 的攻击者不在本防护面。"""
    import hashlib
    import hmac as _hmac
    _strict = (os.environ.get('RAG_DW_SIDECAR_STRICT', '') or '').strip().lower() \
        in ('1', 'true', 'yes', 'on')
    _key = (os.environ.get('RAG_DW_ZIP_HMAC_KEY', '') or '').strip()
    h = hashlib.sha256()
    with open(zip_name, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    digest = h.hexdigest()
    print("[B4] %s sha256=%s" % (zip_name, digest))
    if _strict and not _key:
        raise RuntimeError("[δ3 M13] RAG_DW_SIDECAR_STRICT=on 但调度 env 缺 "
                           "RAG_DW_ZIP_HMAC_KEY——无 key 无从硬验，拒绝执行")
    try:
        _sc = odps.get_resource(zip_name + '.sha256')  # noqa: F821 — DataWorks 运行时注入
        with _sc.open(mode='r') as r:
            expected = (r.read() or '').strip().split()[0].lower()
    except Exception as e:  # noqa: BLE001 — 默认过渡期放行；STRICT 硬拒
        if _strict:
            raise RuntimeError("[δ3 M13] STRICT：sidecar %s.sha256 不存在/不可读"
                               "（重新上传 zip+sidecar 三份资源）: %s" % (zip_name, e))
        print("[B4] ⚠️ sidecar %s.sha256 不存在/不可读（过渡期放行；"
              "RAG_DW_SIDECAR_STRICT=on 将硬拒）: %s" % (zip_name, e))
        return digest
    if expected != digest:
        raise RuntimeError("[B4] 制品完整性校验失败: sha256=%s != sidecar=%s"
                           % (digest, expected))
    if _key:
        try:
            _hc = odps.get_resource(zip_name + '.sha256.hmac')  # noqa: F821 — 同上注入
            with _hc.open(mode='r') as r:
                _sig = (r.read() or '').strip().split()[0].lower()
        except Exception as e:  # noqa: BLE001 — key 已配则 hmac sidecar 必须在场
            raise RuntimeError("[δ3 M13] 调度 env 已配 HMAC key 但 %s.sha256.hmac 缺失/"
                               "不可读（打包侧用 --hmac 重产并三份同批上传）: %s"
                               % (zip_name, e))
        _want = _hmac.new(_key.encode('utf-8'), digest.encode('ascii'),
                          hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(_sig, _want):
            raise RuntimeError("[δ3 M13] HMAC 校验失败：sha256 sidecar 可被连带伪造，"
                               "签名对不上调度 env key，拒绝执行")
        print("[δ3] ✅ HMAC 校验通过（信任边界=调度 env key）")
    print("[B4] ✅ 制品完整性校验通过（sidecar 匹配）")
    return digest


_verify_zip_integrity('opensearch_pipeline_production.zip')
with zipfile.ZipFile('opensearch_pipeline_production.zip', 'r') as zip_ref:
    _safe_extractall(zip_ref, '.')
print("✅ 解压成功")

current_dir = os.path.abspath(".")
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# ═══════════════════════════════════════════════════════════════
# 2. 解析调度参数
# ═══════════════════════════════════════════════════════════════
print("=== 3. 解析调度参数 ===")
bizdate = "20260521"
if len(sys.argv) > 1:
    arg_val = sys.argv[1]
    bizdate = arg_val.split("=")[-1].strip() if "=" in arg_val else arg_val.strip()
    print(f"💡 bizdate: {bizdate}")
else:
    print(f"⚠️ 未获取到参数，使用默认: {bizdate}")

# ═══════════════════════════════════════════════════════════════
# 3. 执行 Stage 3
# ═══════════════════════════════════════════════════════════════
print(f"=== 4. 启动 Stage 3 ({'模拟' if SIMULATE else '生产'}) ===")
from opensearch_pipeline.dataworks_orchestrator import run_stage
run_stage(stage=3, bizdate=bizdate, simulate=SIMULATE)
print("✅ Stage 3 完成！")
