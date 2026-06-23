# -*- coding: utf-8 -*-
"""
prod_access.py — 生产资源访问的唯一官方入口

scratch/、eval_harness/ 等脚本**不得**再自行解析 .env.production/.env.test 拿凭证
（"配置走私"——绕过 config 的环境守卫与 banner）。统一从这里取连接：

  只读（默认，日常诊断/镜像/监控）:
      from opensearch_pipeline.prod_access import get_prod_readonly_conn
      conn = get_prod_readonly_conn()        # SESSION TRANSACTION READ ONLY，写语句报 ERROR 1792

  读写（罕见，必须显式带当日令牌）:
      conn = get_prod_rw_conn(ack=f"PROD-RW:{date.today():%Y-%m-%d}")

  OSS 只读（默认，日常诊断/镜像/对账）:
      bucket = get_prod_oss_bucket()          # put_*/copy_*/delete_* 等一律 raise

  OSS 窄口读写（罕见，必须显式带当日令牌——与 RDS RW 同一道闸）:
      bucket = get_prod_oss_rw_bucket(ack=f"PROD-RW:{date.today():%Y-%m-%d}")
      # 默认只放行 copy_object/put_object；delete_object/batch_delete_objects 仍拦，
      # 需另传当日强令牌 allow_delete_ack=f"PROD-DELETE:{date.today():%Y-%m-%d}"。
      # 其余写方法（put_object_acl/put_symlink/restore_object/...）始终拦。

注意：MySQL 的会话只读是防呆不是防恶意（同会话可被 SET 反转）。真正的物理边界
是 RDS 只读账号（fuling_ro，见 docs/environment_design.md 控制台 checklist）——
.env.prod_ro 换上只读账号后，本模块退化为第二道保险。
"""

from datetime import date
from pathlib import Path

__all__ = ["load_prod_env", "get_prod_readonly_conn", "get_prod_rw_conn",
           "get_prod_oss_bucket", "get_prod_oss_rw_bucket", "ProdAccessError"]

_REPO_ROOT = Path(__file__).resolve().parent.parent


class ProdAccessError(RuntimeError):
    """生产访问入口的令牌/配置错误。"""


def load_prod_env(overlay: str = None) -> dict:
    """解析 .env + 生产侧 overlay 为 dict（**不**写入 os.environ，不污染进程环境）。

    overlay 默认 fallback 顺序：.env.prod_ro > .env.test(过渡 symlink) > .env.production。
    这条链是为 read-only 路径设计的（fuling_ro 在 .env.prod_ro）。

    ⚠️ **RW 路径不要走这个 fallback**。get_prod_rw_conn 会显式传 overlay='.env.production'
    （挂 fuling_admin），否则会加载 .env.prod_ro 拿到只读凭证再 ERROR 1142。
    """
    candidates = [overlay] if overlay else [".env.prod_ro", ".env.test", ".env.production"]
    chosen = None
    for name in candidates:
        if name and (_REPO_ROOT / name).exists():
            chosen = _REPO_ROOT / name
            break
    if chosen is None:
        raise ProdAccessError(f"未找到生产侧 env 文件（尝试了 {candidates}）")

    env: dict = {}
    for p in (_REPO_ROOT / ".env", chosen):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env["_source_file"] = str(chosen.name)
    return env


def _connect(env: dict, *, init_command: str = None, dict_cursor: bool = True):
    import pymysql
    return pymysql.connect(
        host=env["RAG_RDS_HOST"], port=int(env.get("RAG_RDS_PORT", "3306")),
        user=env["RAG_RDS_USER"], password=env["RAG_RDS_PASSWORD"],
        database=env.get("RAG_RDS_DATABASE", "fuling_knowledge"),
        charset="utf8mb4", connect_timeout=10,
        init_command=init_command,
        cursorclass=pymysql.cursors.DictCursor if dict_cursor else pymysql.cursors.Cursor,
    )


def get_prod_readonly_conn(overlay: str = None, *, dict_cursor: bool = True):
    """生产 RDS 只读连接：会话级 READ ONLY，对后续所有事务（含 autocommit 隐式事务）生效。"""
    env = load_prod_env(overlay)
    print(f"[prod_access] READONLY conn -> {env['RAG_RDS_HOST']} (creds: {env['_source_file']})")
    return _connect(env, init_command="SET SESSION TRANSACTION READ ONLY", dict_cursor=dict_cursor)


def get_prod_rw_conn(ack: str, overlay: str = None, *, dict_cursor: bool = True):
    """生产 RDS 读写连接。必须显式传当日令牌 ack='PROD-RW:<YYYY-MM-DD>'。

    令牌按日过期：复制昨天的命令不会静默生效。

    默认 overlay='.env.production'（含 RW 账号 fuling_admin），不再 fallback 到
    .env.prod_ro——因为 .env.prod_ro 现在挂的是 fuling_ro 只读账号，拿来写会
    在 MySQL 层 ERROR 1142 而非 ERROR-out 在 prod_access 这里，调试不友好。
    """
    expected = f"PROD-RW:{date.today().isoformat()}"
    if ack != expected:
        raise ProdAccessError(
            f"生产读写令牌无效（got {ack!r}）。确需写生产：传 ack={expected!r}。"
            f"批量写应优先走 DataWorks runbook 而非本地脚本。")

    # RW 默认从 .env.production 加载（fuling_admin），不走默认 fallback 链
    env = load_prod_env(overlay or ".env.production")

    # 守卫：如果加载到的账号看起来是只读账号，立即 raise——防止用户配错
    # overlay 误指 .env.prod_ro 时不要静默拿到 fuling_ro 然后 ERROR 1142
    _user = (env.get("RAG_RDS_USER") or "").lower()
    if _user.endswith("_ro") or _user.endswith("-ro") or _user == "fuling_ro":
        raise ProdAccessError(
            f"get_prod_rw_conn 加载的 RAG_RDS_USER={_user!r} 看起来是只读账号"
            f"（_source_file={env['_source_file']}）。RW 入口请走 fuling_admin 之类的"
            f"账号。如果你确实要用 _ro 账号做 RW（不会成功），显式 overlay 指别处。")

    print(f"[prod_access] !! RW conn -> {env['RAG_RDS_HOST']} "
          f"(token={ack}, creds: {env['_source_file']}, user={env['RAG_RDS_USER']})")
    return _connect(env, dict_cursor=dict_cursor)


class _ReadOnlyBucket:
    """OSS 只读代理：所有写方法（put_*/copy_*/delete_*/ACL/symlink/bucket 级）一律
    raise，读与签名透传。

    ``allow`` 是显式放行的方法白名单——给 get_prod_oss_rw_bucket 的窄口 RW 句柄用：
    白名单内的方法不再被 _BLOCKED 拦、直接透传到底层 bucket。默认 None = 全拦（纯只读）。

    ⚠️ _BLOCKED 必须覆盖底层 oss2.Bucket 上**所有**会改变服务端状态的方法，否则
    任何遗漏的写方法都会经 __getattr__ 静默透传——这正是本次（HR-4，copy_object 漏拦）
    要堵的洞。新增 oss2 写方法时同步往这里加。
    """

    _BLOCKED = (
        # 对象写入 / 拷贝 / 追加 / 解冻 / 处理（图像处理落盘）
        "put_object", "put_object_from_file", "append_object",
        "copy_object", "restore_object", "process_object",
        # 删除
        "delete_object", "batch_delete_objects",
        # 元数据 / ACL / 符号链接 写
        "put_object_acl", "put_symlink",
        # bucket 级写
        "create_bucket", "delete_bucket",
    )

    def __init__(self, bucket, allow=None):
        self._bucket = bucket
        self._allow = frozenset(allow or ())

    def __getattr__(self, name):
        if name in self._BLOCKED and name not in self._allow:
            raise ProdAccessError(
                f"prod_access 的 OSS 句柄拒绝写方法 {name}。只读诊断用 get_prod_oss_bucket()；"
                f"确需写生产 OSS 走 get_prod_oss_rw_bucket(ack='PROD-RW:<today>')，"
                f"或 DataWorks/SAE 注入凭证的正式管线。")
        return getattr(self._bucket, name)


def _build_oss_bucket(env: dict, *, public_endpoint: bool = True):
    """从已解析的 env dict 构造底层 oss2.Bucket（公网 / 内网 endpoint 切换）。"""
    import oss2
    endpoint = env.get("RAG_OSS_ENDPOINT", "oss-cn-hangzhou.aliyuncs.com")
    if public_endpoint:
        endpoint = endpoint.replace("-internal", "")
    auth = oss2.Auth(env["RAG_OSS_ACCESS_KEY_ID"], env["RAG_OSS_ACCESS_KEY_SECRET"])
    return oss2.Bucket(auth, endpoint, env.get("RAG_OSS_BUCKET_NAME", "fuling-knowledge-base"))


def get_prod_oss_bucket(overlay: str = None, *, public_endpoint: bool = True):
    """生产 OSS 只读句柄（GetObject/ListObjects/sign_url 可用，所有写方法 raise）。"""
    env = load_prod_env(overlay)
    return _ReadOnlyBucket(_build_oss_bucket(env, public_endpoint=public_endpoint))


def get_prod_oss_rw_bucket(ack: str, overlay: str = ".env.production", *,
                           allow_delete_ack: str = None, public_endpoint: bool = True):
    """生产 OSS 窄口读写句柄。必须显式传当日令牌 ack='PROD-RW:<YYYY-MM-DD>'
    （与 get_prod_rw_conn 同一道闸——令牌按日过期，复制昨天的命令不会静默生效）。

    默认只放行 ``copy_object`` / ``put_object``；``delete_object`` /
    ``batch_delete_objects`` 仍被拦——除非另传当日强令牌
    allow_delete_ack='PROD-DELETE:<YYYY-MM-DD>'（删除不可逆，单设一道更高的闸）。
    其余写方法（put_object_acl / put_symlink / restore_object / ...）始终拦。

    默认 overlay='.env.production'（admin AK，可写），**不**走 .env.prod_ro fallback——
    与 get_prod_rw_conn 一致：RW 入口绝不静默拿只读凭证。
    """
    expected = f"PROD-RW:{date.today().isoformat()}"
    if ack != expected:
        raise ProdAccessError(
            f"生产 OSS 读写令牌无效（got {ack!r}）。确需写生产 OSS：传 ack={expected!r}。"
            f"批量写应优先走 DataWorks/SAE 正式管线而非本地脚本。")

    allow = {"copy_object", "put_object"}
    if allow_delete_ack is not None:
        expected_del = f"PROD-DELETE:{date.today().isoformat()}"
        if allow_delete_ack != expected_del:
            raise ProdAccessError(
                f"生产 OSS 删除强令牌无效（got {allow_delete_ack!r}）。"
                f"确需删生产 OSS：传 allow_delete_ack={expected_del!r}。")
        allow |= {"delete_object", "batch_delete_objects"}

    env = load_prod_env(overlay)
    print(f"[prod_access] !! OSS RW handle -> "
          f"{env.get('RAG_OSS_BUCKET_NAME', 'fuling-knowledge-base')} "
          f"(token={ack}, allow={sorted(allow)}, creds: {env['_source_file']})")
    return _ReadOnlyBucket(_build_oss_bucket(env, public_endpoint=public_endpoint), allow=allow)
