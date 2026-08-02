# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — Stage 3: Chunks → OpenSearch Index
上传到 DataWorks 调度节点中直接使用。

⚠️ 默认生产模式（SIMULATE=False）。冒烟测试：在节点环境变量设 RAG_NODE_SIMULATE=true。
⚠️ Stage 3 额外需要 HA3 / OpenSearch 凭证
"""
import os
import sys
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone

# 📦 安装 Stage 3 依赖（按运行时 Python 版本选择；2026-07-17 发现 serverless 执行器 = py3.7）
# py3.7 陷阱: pypdf 5.0.0 元数据谎报支持 3.7 实则用 typing.Protocol(3.8+) → 必须钉真兼容版。
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

# ═══════════════════════════════════════════════════════════════
# 🔧 模式开关：默认生产（False）。冒烟测试设环境变量 RAG_NODE_SIMULATE=true。
#    旧写法硬编码 True，部署忘改 → 整阶段跑 mock 却 exit 0（DataWorks 绿），语料静默停更。
# ═══════════════════════════════════════════════════════════════
# ⚠️ 冒烟开关:临时取消下一行注释 = 干跑(全 mock,不碰真实 RDS/OSS/HA3/LLM)。
# ⚠️ 测完必须重新注释掉,否则每日调度永远跑 mock 且 DataWorks 全绿(语料静默停更事故)!
# os.environ["RAG_NODE_SIMULATE"] = "true"
SIMULATE = os.environ.get("RAG_NODE_SIMULATE", "false").strip().lower() in ("true", "1", "yes")

# ═══════════════════════════════════════════════════════════════
# 🔐 环境变量配置（必须在 import pipeline 代码之前设置）
# ═══════════════════════════════════════════════════════════════
os.environ["RAG_SIMULATE"] = str(SIMULATE).lower()
os.environ["RAG_ENVIRONMENT"] = "production"

# ── 生产安全姿态断言(批次5 P0-07d)——不设这两行,节点在 load_config() 就 ValueError 崩 ──
# config.py 要求 production 环境启动时【显式表态】:要么开这两个 flag,要么签当日
# RAG_ALLOW_LEGACY_OPEN_PROD=ack:<YYYY-MM-DD>(午夜过期,不适合偶发手动重跑)。
# 2026-07-21 蒸发补推时实地踩过:节点 61.9s 后 exit 1。
# (2026-08-02 起代码包从 main 打——此前十天为 op0 包;两侧 config 同款守卫,本段不变。)
# 安全性说明:这两个 flag 只被 api.py / retriever.py / readiness.py 读取(服务侧);
# 摄取侧 pipeline_nodes / dataworks_orchestrator / dag_definitions 【零读取】——已 grep 核实,
# 故在批处理节点上设为 true 对管线行为无任何影响,纯粹满足启动表态。
# 用 [] 直赋而非 setdefault:守卫要的是"确定为真",setdefault 在外部注入 false 时会失守。
os.environ["RAG_REQUIRE_AUTH"] = "true"
os.environ["RAG_ACL_FAIL_CLOSED"] = "true"
# ── PR-4 摄取租约(RAG_INGEST_LEASE_ENABLE)：模块已移植回 main(2026-08-02,task_68097fb4),
# flag **默认关**——本模板刻意不设该 env(关=逐字节 legacy 行为)。启用走
# docs/ingest_lease_fencing_scope_2026-07-17.md 附录 A 的 runbook(先清残留租约列再开)。
# ⚠️ 重贴正式节点必须用 main 生成器版(本文件):旧 op0 paste 版此处有 setdefault "true",
# 若沿用旧贴片+新 main 包(现已含租约模块),租约会**隐式开启**,绕过 runbook 前置清理。
# 开 flag 前**不得提高摄取并发**;开 flag 后并发才允许升(设计顺序约束)。

# ── Robustness features (validated GO 2026-06-23; default-OFF in code, enabled here for prod) ──
# Stage-3 node 04b. setdefault → overridable (set the env var to 'false' to disable).
# Parity-verify: re-read HA3 after push + bounded re-push of silent drops (first prod runs healed
# 96+1 cleanly). Drift: PK-present-but-stale-content sub-check (requires parity-verify on; verbatim
# hash = false-positive-proof). SETTLE_SEC stays default 30s.
os.environ.setdefault("RAG_STAGE3_PARITY_VERIFY", "true")
os.environ.setdefault("RAG_STAGE3_PARITY_DRIFT", "true")

# ── node-ACL 必须开（2026-07-31）：⚠️ 这不再是「Phase D 的可选加固」，而是节点通道的命脉 ──
# 节点授权值 `d:<id>` / `dx:<id>` **就住在 allowed_depts 字段里**（设计稿 §2.1：复用现有
# MULTI_STRING 字段 ⇒ HA3 零 schema 变更）。而推送侧受本 flag 门控：
#     pipeline_nodes.py:7652   _incl_ad = get_config().rag.allowed_depts_acl
#     → to_ha3_doc(include_allowed_depts=_incl_ad)
# flag 关 ⇒ 该字段**根本不进 HA3 文档** ⇒ node 文档的 d:/dx: 永远到不了检索侧
# ⇒ 检索器的 `allowed_depts="d:123"` 分支永远匹配不上 ⇒ **节点授权全盘失效**
#   （RDS 里有值、HA3 里没有；write_chunk_meta 的 node 投影不受本 flag 门控，故只断这一头）。
# 而 `reconcile_allowed_depts` 在 flag 关时**直接 skipped 返回**，自愈路径也兜不住。
#
# 原注释写「不在此设默认，由 DataWorks 环境注入（双端 flip 前为 OFF）」——那是 Phase D 时代的
# 谨慎，但 serving 侧早已 ON（T7 控制台确认），摄取侧一直没跟上 = T7 记的「少一边即半条链」。
# 2026-07-31 只读实测现网，四个受影响集合全为空 ⇒ **此刻打开的行为变化为零**：
#     approved 跨部门授权 0 · chunk_meta.allowed_depts 非空 0 · acl_mode='node' 文档 0 ·
#     kb_doc_node_grant 在册 0   （唯一变化＝新推 chunk 多带一个空 allowed_depts，字段本就存在）
# 等 node 文档出现再开，就变成「两个变量同时动」——所以趁零半径先固化。
#
# 用 [] 直赋而非 setdefault：与上方 REQUIRE_AUTH/ACL_FAIL_CLOSED 同理——这是「必须确定为真」
# 的安全通道，setdefault 在外部注入 false 时会失守，而失守的表现是 node 文档**静默不可见**。
os.environ["RAG_ALLOWED_DEPTS_ACL"] = "true"

if not SIMULATE:
    # 生产凭证：由 DataWorks 调度参数注入
    required_vars = [
        "DASHSCOPE_API_KEY", "RAG_RDS_HOST", "RAG_RDS_PASSWORD",
        "RAG_OSS_ACCESS_KEY_ID", "RAG_OSS_ACCESS_KEY_SECRET",
        "RAG_HA3_ENDPOINT", "RAG_HA3_USER", "RAG_HA3_PASSWORD",
    ]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"Missing required env vars for production: {missing}")

# ═══════════════════════════════════════════════════════════════
# 1. 下载并解压代码包
# ═══════════════════════════════════════════════════════════════
print("=== 1. 下载 Archive 资源 ===")
resource = odps.get_resource('opensearch_pipeline_production.zip')
with resource.open(mode='rb') as reader:
    with open('opensearch_pipeline_production.zip', 'wb') as writer:
        writer.write(reader.read())

print("=== 2. 解压代码包 ===")
if not os.path.exists('opensearch_pipeline_production.zip'):
    raise RuntimeError("❌ 未能下载 opensearch_pipeline_production.zip")

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
# 兜底 bizdate = 北京 T-1（与 DataWorks ${bizdate} 语义一致）。原先硬编码 '20260521'：
# 节点漏配调度参数时会永远跑在那个过期日期上且毫无报错。
# P3-10：曾用容器本地 datetime.now()——DataWorks pod 是 UTC，其翻日时刻比北京晚 8h，
# 北京 00:00-08:00 间兜底会算错业务日 → 改按固定 UTC+8 求北京 T-1（bizdate 仅作
# 溯源标注，各阶段选行是纯状态 drain，不按日期过滤——见 dataworks_orchestrator docstring）。
bizdate = (datetime.now(timezone.utc) + timedelta(hours=8) - timedelta(days=1)).strftime("%Y%m%d")
if len(sys.argv) > 1:
    arg_val = sys.argv[1]
    bizdate = arg_val.split("=")[-1].strip() if "=" in arg_val else arg_val.strip()
    print(f"💡 bizdate: {bizdate}")
else:
    print(f"⚠️ 未获取到调度参数，按 T-1 兜底: {bizdate}")

# ═══════════════════════════════════════════════════════════════
# 3. 执行 Stage 3（排空式：drain 整批 + pre-drain 三对账）
# ═══════════════════════════════════════════════════════════════
# ⚠️ 用 run_stage_drained（非单批 run_stage）：生产排空整个 stage-3 待处理集，并在 drain【之前】跑
#    三个对账——reconcile_stranded_versions / reconcile_pending_deletes / reconcile_allowed_depts
#    （Phase D 撤销·授权投影自愈，RAG_ALLOWED_DEPTS_ACL 关时 skipped no-op）。这些对账只在
#    run_stage_drained 里；旧的单批 run_stage 既不排空也不跑对账。SIMULATE=True 时 drained 内部
#    退化为单次 run_stage（行为不变）。
#    ⚠️ RAG_ALLOWED_DEPTS_ACL 见上方【node-ACL 必须开】段落——它现在是显式直赋 true。
print(f"=== 4. 启动 Stage 3 排空（{'模拟' if SIMULATE else '生产'}）===")
from opensearch_pipeline.dataworks_orchestrator import run_stage_drained
run_stage_drained(stage=3, bizdate=bizdate, simulate=SIMULATE)
print("✅ Stage 3 完成！")
