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

  OSS 只读:
      bucket = get_prod_oss_bucket()          # put_*/delete_* 一律 raise

注意：MySQL 的会话只读是防呆不是防恶意（同会话可被 SET 反转）。真正的物理边界
是 RDS 只读账号（fuling_ro，见 docs/environment_design.md 控制台 checklist）——
.env.prod_ro 换上只读账号后，本模块退化为第二道保险。
"""

from datetime import date
from pathlib import Path

__all__ = ["load_prod_env", "get_prod_readonly_conn", "get_prod_rw_conn",
           "get_prod_oss_bucket", "ProdAccessError"]

_REPO_ROOT = Path(__file__).resolve().parent.parent


class ProdAccessError(RuntimeError):
    """生产访问入口的令牌/配置错误。"""


def load_prod_env(overlay: str = None) -> dict:
    """解析 .env + 生产侧 overlay 为 dict（**不**写入 os.environ，不污染进程环境）。

    overlay 解析顺序：显式参数 > .env.prod_ro > .env.test(过渡 symlink) > .env.production。
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
    """
    expected = f"PROD-RW:{date.today().isoformat()}"
    if ack != expected:
        raise ProdAccessError(
            f"生产读写令牌无效（got {ack!r}）。确需写生产：传 ack={expected!r}。"
            f"批量写应优先走 DataWorks runbook 而非本地脚本。")
    env = load_prod_env(overlay)
    print(f"[prod_access] !! RW conn -> {env['RAG_RDS_HOST']} (token={ack}, creds: {env['_source_file']})")
    return _connect(env, dict_cursor=dict_cursor)


class _ReadOnlyBucket:
    """OSS 只读代理：put_*/delete_* 一律 raise，读与签名透传。"""

    _BLOCKED = ("put_object", "put_object_from_file", "delete_object",
                "batch_delete_objects", "append_object")

    def __init__(self, bucket):
        self._bucket = bucket

    def __getattr__(self, name):
        if name in self._BLOCKED:
            raise ProdAccessError(f"prod_access 的 OSS 句柄是只读的（拒绝 {name}）。"
                                  f"生产 OSS 写入走 DataWorks/SAE 注入凭证的正式管线。")
        return getattr(self._bucket, name)


def get_prod_oss_bucket(overlay: str = None, *, public_endpoint: bool = True):
    """生产 OSS 只读句柄（GetObject/ListObjects/sign_url 可用）。"""
    import oss2
    env = load_prod_env(overlay)
    endpoint = env.get("RAG_OSS_ENDPOINT", "oss-cn-hangzhou.aliyuncs.com")
    if public_endpoint:
        endpoint = endpoint.replace("-internal", "")
    auth = oss2.Auth(env["RAG_OSS_ACCESS_KEY_ID"], env["RAG_OSS_ACCESS_KEY_SECRET"])
    return _ReadOnlyBucket(oss2.Bucket(auth, endpoint, env.get("RAG_OSS_BUCKET_NAME", "fuling-knowledge-base")))
