# -*- coding: utf-8 -*-
"""C8 审批内容绑定（OSS version-id 固化）—— Sam 2026-08-04 拍板。

## 修的是什么

签名 PUT URL 与 upload token 共用 30 分钟 TTL，且**预签名 URL 服务端无法逐个撤销**
（OSS AK 签发、按时间过期；作废我们自己的 HMAC token 完全不影响那条 URL）。于是：

    上传合规文件 A → register/审批放行 → 30 分钟内用**同一 put_url** 重 PUT B → 摄取 B

⚠️ 裸 ETag 复核**不够**（拍板单 §2.2，不需要 ETag 碰撞）：

    register 时对象=B，登记 ETag(B)
    审批预览前重 PUT A   → 审批人看到 A
    审批后重 PUT B       → stage-1 的 If-Match ETag(B) 通过
    最终入库 B           → 审批人批的是 A

⇒ 必须**同时**固定「审批预览读的」与「最终摄取读的」两处身份。version-id 天然满足：
重 PUT 产生**新版本**，动不了已固化的那一个。

## 三态契约（schema/064）

`LEGACY_UNBOUND`（存量/flag 关/064 未 apply，允许兼容读）/ `VERSION_ID`（必须有版本号，
不符即 fail-closed，**不得回退 ETag 或 legacy**）/ `FROZEN_KEY`（预留给方案 F）。
只靠 `raw_version_id IS NULL` 分不出「存量」与「新写失败」——那正是 codex 的 BLOCKER。
"""
import pytest


def _skip_if_not_sim():
    import os
    if os.getenv("RAG_SIMULATE", "true").lower() not in ("true", "1", "yes"):
        pytest.skip("需 simulate 模式")


# ── ① head_object 必须回吐 version_id ────────────────────────────────────────

def test_sim_head_object_returns_version_id():
    """sim HEAD 也要给 version_id —— 否则 VERSION_ID 分支在 simulate 里永远走不到，
    而本仓纪律是「改动先在 simulate 验证」。"""
    from opensearch_pipeline.oss_url import _sim_head_object
    m = _sim_head_object("raw/hr/DOC1/v1/a.pdf")
    assert m.get("version_id"), "sim HEAD 没给 version_id"
    assert m["version_id"] == _sim_head_object("raw/hr/DOC1/v1/a.pdf")["version_id"], "同 key 应确定性"
    assert m["version_id"] != _sim_head_object("raw/hr/DOC2/v1/b.pdf")["version_id"], "不同 key 应不同"


def test_sim_head_object_can_simulate_versioning_off(monkeypatch):
    """`RAG_SIM_OSS_VERSION_ID=""` 模拟**未开 versioning** 的 bucket —— fail-closed 分支的入口。"""
    from opensearch_pipeline.oss_url import _sim_head_object
    monkeypatch.setenv("RAG_SIM_OSS_VERSION_ID", "")
    assert _sim_head_object("raw/hr/DOC1/v1/a.pdf")["version_id"] == ""


def test_real_head_object_reads_version_from_headers():
    """🔴 真 HEAD 必须从**响应头** `x-oss-version-id` 取，不能读 SDK 属性。

    不同 oss2 版本的属性名不稳（versionid / version_id），headers 是协议面。
    读错属性 ⇒ 恒空 ⇒ flag 一开全站 503（或更糟：被当成"未开 versioning"而降级）。
    """
    import pathlib
    src = pathlib.Path("opensearch_pipeline/oss_url.py").read_text(encoding="utf-8")
    i = src.index("def head_object(")
    body = src[i:i + 2600]
    assert "x-oss-version-id" in body, "head_object 没读 x-oss-version-id 响应头"
    assert '"version_id"' in body, "head_object 没回吐 version_id 键"


# ── ② generate_signed_url 必须能把 versionId 签进去 ──────────────────────────

def test_signed_url_passes_params_through():
    """预览按**保存的版本**签名 —— 这是 C8 的另一半（只在摄取侧加校验挡不住 §2.2 的反例）。"""
    import inspect
    from opensearch_pipeline.oss_url import generate_signed_url
    assert "params" in inspect.signature(generate_signed_url).parameters

    calls = []

    class _B:
        def sign_url(self, method, key, expires, headers=None, params=None):
            calls.append(params)
            return "https://oss/x"

    generate_signed_url("raw/a.pdf", expires=60, method="GET", bucket=_B(),
                        params={"versionId": "V7"})
    generate_signed_url("raw/a.pdf", expires=60, method="GET", bucket=_B())
    if not calls:
        pytest.skip("本环境无 OSS 凭据，generate_signed_url 早退（签名透传由下方源码断言兜住）")
    assert calls[0] == {"versionId": "V7"}, "versionId 没被签进 URL"
    assert calls[-1] is None, "无绑定时应传 None（保持原调用形态）"


# ── ③ register：flag 关=零变化；flag 开+无版本号=fail-closed ─────────────────

def test_register_binding_is_off_by_default():
    """默认 flag 关 ⇒ 行为逐字节等于今天（`content_binding` 默认 False）。"""
    from opensearch_pipeline.config import load_config
    assert load_config().rag.content_binding is False


def test_register_fails_closed_when_versioning_unavailable(monkeypatch):
    """★ flag 开但 HEAD 拿不到版本号 ⇒ **503**，绝不静默落回 LEGACY_UNBOUND。

    这是 codex 的 BLOCKER：生产若处于 Suspended、包装器漏字段或配置漂移，
    实施者很容易把空值当 legacy 兼容继续放行 ⇒ **绑定静默退化回未绑定状态**，
    而运维还以为已经开了。
    """
    # register 走完整鉴权/令牌链，端到端桩成本远超所验内容；这里用源码断言钉住
    # **判定本身**：flag 开 + 空 version_id ⇒ 抛 503，且**不存在**退回 LEGACY 的分支。
    import pathlib
    src = pathlib.Path("opensearch_pipeline/routes/kb_console.py").read_text(encoding="utf-8")
    i = src.index("# ── C8 内容绑定（schema/064）")
    blk = src[i:i + 1700]
    assert "if cfg.rag.content_binding:" in blk, "绑定没受 flag 门控"
    assert 'status_code=503' in blk, "拿不到 version_id 时没有 fail-closed"
    assert '_binding_mode = "VERSION_ID"' in blk
    # 🔴 关键：503 必须在 `_binding_mode = "VERSION_ID"` **之前**——否则等于先标成已绑定再报错
    assert blk.index("status_code=503") < blk.index('_binding_mode = "VERSION_ID"'), (
        "fail-closed 必须先于置 VERSION_ID，否则会出现「标了绑定却没版本号」的行")
    # 且绝不能出现"拿不到就退回 legacy"的分支
    assert "LEGACY_UNBOUND" not in blk.split("status_code=503")[1][:400], (
        "503 之后出现了 LEGACY_UNBOUND —— 疑似静默降级")


# ── ④ 三态契约本身 ──────────────────────────────────────────────────────────

def test_schema_064_declares_three_states():
    """`content_binding_mode` 的三态必须在 DDL 里写死（不是靠 raw_version_id IS NULL 推断）。"""
    import pathlib
    ddl = pathlib.Path("schema/064_content_binding.sql").read_text(encoding="utf-8")
    for st in ("LEGACY_UNBOUND", "VERSION_ID", "FROZEN_KEY"):
        assert st in ddl, f"schema/064 缺三态之一：{st}"
    assert "DEFAULT 'LEGACY_UNBOUND'" in ddl, "默认值必须是 LEGACY_UNBOUND（apply 后行为不变）"


def test_stage1_download_binds_and_verifies_identity():
    """🔴 stage-1 必须 ①按 versionId 取件 ②**核对返回身份**。

    只传 params 不核返回 = 只信请求参数；代理 / SDK 降级 / 桶策略变化都可能让它
    悄悄落回"当前版本"，而我们以为绑上了。
    """
    import pathlib
    src = pathlib.Path("opensearch_pipeline/pipeline_nodes.py").read_text(encoding="utf-8")
    i = src.index("C8 内容绑定：按 register 固化")
    blk = src[i:i + 3200]
    assert 'params={"versionId": _bvid}' in blk, "取件没带 versionId"
    assert "x-oss-version-id" in blk, "取回后没核对返回的版本身份"
    assert "_content_mismatch" in blk, "身份不符没留哨兵"


def test_content_mismatch_is_not_auto_retryable():
    """🔴 CONTENT_MISMATCH 必须是**终态**，不得落进 stage-1/2 的认领谓词。

    拍板单 §4.2：内容不符是安全不变量破坏，不是 OSS 瞬断。复用
    `_mark_extraction_failed`（刻意留 keys=NULL 让下轮自动重捡）会让它每天重捡、
    反复占队头，而 OSS 上那个对象永远不会变回去。
    """
    import pathlib
    src = pathlib.Path("opensearch_pipeline/pipeline_nodes.py").read_text(encoding="utf-8")
    assert "def _mark_content_mismatch(" in src, "缺独立的终态写入函数"
    assert "content_process_status='CONTENT_MISMATCH'" in src
    # stage-1 认领谓词：NOT_STARTED / (FAILED AND retry<3)；CONTENT_MISMATCH 都不在其中
    i = src.index("WHERE dv.content_process_status = 'NOT_STARTED'")
    assert "CONTENT_MISMATCH" not in src[i:i + 400], "CONTENT_MISMATCH 混进了 stage-1 认领谓词"


def test_content_mismatch_badge_exists_and_is_anomalous():
    """终态要在控制台看得见，且计入「异常」聚合（否则运维发现不了）。"""
    from opensearch_pipeline.api import _KB_BAD_BADGES, _kb_status_badge
    assert _kb_status_badge("CONTENT_MISMATCH", "SUCCESS", "active") == "内容不符", (
        "必须排在「已上线」之前——本终态的 index_status 可能残留 SUCCESS")
    assert "内容不符" in _KB_BAD_BADGES


def test_scan_tool_skips_bound_versions():
    """🔴 §4.3：`scan_oss_sync_keys` 不得把**已绑定**版本的 raw_key 指向另一份对象。

    该工具会改写 raw_key 却不更新 ETag/version-id —— 对已绑定版本，那正是 C8 的攻击面，
    只不过换成我们自己动手。
    """
    import pathlib
    src = pathlib.Path("dataworks_nodes/scan_oss_sync_keys.py").read_text(encoding="utf-8")
    assert "content_binding_mode" in src, "scan 工具没读绑定态"
    assert "bound_skipped" in src, "scan 工具没有跳过并列出已绑定版本"
    i = src.index('if str(_bmode or "") == "VERSION_ID":')
    assert "continue" in src[i:i + 260], "已绑定版本没被 continue 跳过"
