"""摄取台账租约/栅栏协议（PR-4，schema/048）。

两本台账（document_version.content_process_status＝stage-1/2、index_status＝stage-3）
的认领此前只有一次性 CAS + 2h 年龄式失效接管：>2h 的存活运行会被当尸体接管，被接管的
僵尸继续写回（撕裂 write_chunk_meta 的 DELETE→INSERT、终态互相覆盖、基于陈旧视图跑
不可逆的 deactivate/HA3 删除）。本模块给认领加租约（holder + SQL 侧到期 + fencing
epoch），运行中续租，终态/破坏性写回带栅栏——丢锁僵尸的写原子性失败（LeaseLost，
弃单文档继续批）。设计与拍板见 docs/ingest_lease_fencing_scope_2026-07-17.md。

接线原则：
  · 总闸 RAG_INGEST_LEASE_ENABLE 默认 off——off 时本模块所有 SQL 片段返回空串、
    所有动作 no-op，调用点拼出的 SQL 与既有实现逐字节一致（off 臂零行为变化）；
  · 本模块不管理连接：全部函数在调用方事务的 cursor 上执行——「验租与副作用同事务」
    是栅栏成立的前提（形态同 schema/045 的同事务回执）；
  · 时钟纪律：租约时间算术全在 SQL 侧（NOW(3)），Python 永不比较墙钟；
  · changed-rows 陷阱（连接池未开 CLIENT_FOUND_ROWS，同值 UPDATE rowcount=0）：
    claim 恒 +1 epoch、renew 恒改 lease_expires_at，天然保证匹配行 rowcount≥1。
"""

import logging
import os
import socket
import threading
import time
import uuid
from typing import Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

DocVersionKey = Tuple[str, int]  # (doc_id, version_no)

#: 无租约行（旧包/裸跑认领）的失效兜底，与既有 stage-2 清扫 / stage-3 接管的 2h 一致。
LEGACY_STALE_HOURS = 2


class LeaseLost(RuntimeError):
    """续租/验租/栅栏写发现租约已被接管——放弃该文档剩余写回，继续批内其他文档。"""


def lease_enabled() -> bool:
    return os.environ.get("RAG_INGEST_LEASE_ENABLE", "").strip().lower() in (
        "1", "true", "yes", "on")


def lease_ttl_s() -> int:
    return max(60, int(os.environ.get("RAG_INGEST_LEASE_TTL_S", "900")))


def lease_renew_s() -> int:
    return max(10, int(os.environ.get("RAG_INGEST_LEASE_RENEW_S", "300")))


_HOLDER_LOCK = threading.Lock()
_HOLDER_ID = None  # type: Optional[str]


def holder_id() -> str:
    """进程级实例 id：{hostname[:24]}-{pid}-{uuid8}（风格同 agent_dispatch lease_holder）。"""
    global _HOLDER_ID
    with _HOLDER_LOCK:
        if _HOLDER_ID is None:
            host = (socket.gethostname() or "unknown").split(".")[0][:24]
            _HOLDER_ID = "%s-%d-%s" % (host, os.getpid(), uuid.uuid4().hex[:8])
        return _HOLDER_ID


# ── 认领（claim）────────────────────────────────────────────────────────────
# 用法：把片段追加进既有认领 UPDATE 的 SET 尾部（谓词不变），params 对应追加。
#   cursor.execute(f"UPDATE document_version SET index_status='PROCESSING'"
#                  f"{claim_set_sql()} WHERE ...", claim_set_params() + (...))
# off 时片段为空串、params 为空元组——SQL 逐字节等于现状。

def claim_set_sql() -> str:
    if not lease_enabled():
        return ""
    return (", lease_holder = %s, lease_expires_at = NOW(3) + INTERVAL %s SECOND"
            ", lease_epoch = lease_epoch + 1")


def claim_set_params() -> tuple:
    if not lease_enabled():
        return ()
    return (holder_id(), lease_ttl_s())


def clear_set_sql() -> str:
    """终态写（DONE/FAILED/SUCCESS/NOT_INDEXED/清扫复位）顺手清租约——
    「租约列非空 ⇔ 有人自认在跑」。epoch 保留（永远单调）。off 时为空串。"""
    if not lease_enabled():
        return ""
    return ", lease_holder = NULL, lease_expires_at = NULL"


# ── 失效接管谓词 ─────────────────────────────────────────────────────────────

def takeover_where_sql(alias: str = "") -> str:
    """替换既有 `updated_at < NOW() - INTERVAL 2 HOUR` 的接管/计数谓词。

    on：带租约的行按到期判死；无租约行（旧包/裸跑）回退 2h 年龄兜底——新旧混跑无缝。
    off：返回与既有实现逐字节一致的 2h 年龄谓词。
    alias 形如 "dv."（stage-3 计数查询带表别名）。
    """
    p = alias
    legacy = "%supdated_at < NOW() - INTERVAL %d HOUR" % (p, LEGACY_STALE_HOURS)
    if not lease_enabled():
        return legacy
    return ("((%slease_expires_at IS NOT NULL AND %slease_expires_at < NOW(3))"
            " OR (%slease_holder IS NULL AND %s))" % (p, p, p, legacy))


# ── 租约集（每次 DAG/stage 运行一个，挂 ctx）────────────────────────────────

def get_lease_set(ctx: dict) -> "LeaseSet":
    ls = ctx.get("ingest_lease_set")
    if ls is None:
        ls = LeaseSet()
        ctx["ingest_lease_set"] = ls
    return ls


class LeaseSet:
    """本次运行持有的 (doc_id, version_no)→epoch 台账 + 节流续租。线程安全。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._epochs = {}       # type: Dict[DocVersionKey, int]
        self._last_renew = {}   # type: Dict[DocVersionKey, float]
        self._last_renew_all = 0.0

    # -- 注册 --------------------------------------------------------------
    def fetch_and_register(self, cursor, keys: Iterable[DocVersionKey]) -> int:
        """认领 UPDATE 提交同事务内回读 epoch 并登记。只登记 holder=本进程的行
        （集合式认领的部分成功/他方行天然被过滤）。返回登记数。off 时 no-op。"""
        if not lease_enabled():
            return 0
        keys = list(keys)
        if not keys:
            return 0
        clause = " OR ".join(["(doc_id = %s AND version_no = %s)"] * len(keys))
        params = tuple(p for k in keys for p in k) + (holder_id(),)
        cursor.execute(
            "SELECT doc_id, version_no, lease_epoch FROM document_version"
            " WHERE (" + clause + ") AND lease_holder = %s", params)
        rows = cursor.fetchall() or ()
        n = 0
        with self._lock:
            for row in rows:
                doc_id, version_no, epoch = row[0], int(row[1]), int(row[2])
                self._epochs[(doc_id, version_no)] = epoch
                self._last_renew[(doc_id, version_no)] = time.monotonic()
                n += 1
        return n

    def epoch(self, key: DocVersionKey) -> Optional[int]:
        with self._lock:
            return self._epochs.get(key)

    def discard(self, key: DocVersionKey) -> None:
        with self._lock:
            self._epochs.pop(key, None)
            self._last_renew.pop(key, None)

    # -- 续租 --------------------------------------------------------------
    def maybe_renew(self, cursor, key: DocVersionKey) -> None:
        """按 RAG_INGEST_LEASE_RENEW_S 节流续租；调用点=既有循环的逐文档/逐批
        DB 触点（不开后台线程）。rowcount==0 ⇒ 租约已被接管 ⇒ LeaseLost。
        off/未登记的 key（如 flag 关、非租约认领路径）恒 no-op。"""
        if not lease_enabled():
            return
        with self._lock:
            ep = self._epochs.get(key)
            last = self._last_renew.get(key, 0.0)
        if ep is None:
            return
        if time.monotonic() - last < lease_renew_s():
            return
        cursor.execute(
            "UPDATE document_version SET lease_expires_at = NOW(3) + INTERVAL %s SECOND"
            " WHERE doc_id = %s AND version_no = %s"
            " AND lease_holder = %s AND lease_epoch = %s",
            (lease_ttl_s(), key[0], key[1], holder_id(), ep))
        if cursor.rowcount == 0:
            self.discard(key)
            raise LeaseLost("lease lost on renew: doc=%s v=%s (epoch %s preempted)"
                            % (key[0], key[1], ep))
        with self._lock:
            self._last_renew[key] = time.monotonic()

    def should_renew(self) -> bool:
        """批量续租的免 DB 节流预判——调用点先问这句，到期才开池连接跑 renew_all。"""
        if not lease_enabled():
            return False
        with self._lock:
            if not self._epochs:
                return False
            return time.monotonic() - self._last_renew_all >= lease_renew_s()

    def renew_all(self, cursor) -> int:
        """批量续租本集全部 key（认领是批语义——逐 key 完成时续租护不住还在排队的
        文档，故按整集续）。谓词只按 holder（epoch 不进谓词：epoch 只在认领时变，
        变则 holder 必被改写，holder=me 即足判自有）。rowcount 短缺 ⇒ 部分被接管
        ⇒ 回读 holder=me 存活集、丢弃已失 key（不 raise——失主文档走到栅栏写时
        自然 LeaseLost 弃单文档）。返回丢弃数。"""
        if not lease_enabled():
            return 0
        with self._lock:
            keys = list(self._epochs.keys())
        if not keys:
            return 0
        clause = " OR ".join(["(doc_id = %s AND version_no = %s)"] * len(keys))
        params = (lease_ttl_s(),) + tuple(p for k in keys for p in k) + (holder_id(),)
        cursor.execute(
            "UPDATE document_version SET lease_expires_at = NOW(3) + INTERVAL %s SECOND"
            " WHERE (" + clause + ") AND lease_holder = %s", params)
        lost = 0
        if cursor.rowcount < len(keys):
            cursor.execute(
                "SELECT doc_id, version_no FROM document_version"
                " WHERE (" + clause + ") AND lease_holder = %s",
                tuple(p for k in keys for p in k) + (holder_id(),))
            alive = {(r[0], int(r[1])) for r in (cursor.fetchall() or ())}
            for k in keys:
                if k not in alive:
                    self.discard(k)
                    lost += 1
                    logger.warning("ingest lease lost (renew_all): doc=%s v=%s", k[0], k[1])
        now = time.monotonic()
        with self._lock:
            self._last_renew_all = now
            for k in keys:
                if k in self._epochs:
                    self._last_renew[k] = now
        return lost

    # -- 栅栏 --------------------------------------------------------------
    def fence_where_sql(self, key: DocVersionKey) -> str:
        """单语句写回的栅栏谓词（追加进 WHERE）。off/未登记 ⇒ 空串（=现状 SQL）。"""
        if not lease_enabled() or self.epoch(key) is None:
            return ""
        return " AND lease_holder = %s AND lease_epoch = %s"

    def fence_where_params(self, key: DocVersionKey) -> tuple:
        ep = self.epoch(key)
        if not lease_enabled() or ep is None:
            return ()
        return (holder_id(), ep)

    def check_fenced_write(self, cursor, key: DocVersionKey, expected_rowcount: int = 1) -> None:
        """带栅栏的 UPDATE 执行后调用：rowcount 短缺 ⇒ 租约已被接管 ⇒ LeaseLost。
        仅在栅栏真的拼进了 SQL 时判定（off/未登记时 no-op——rowcount 语义归原逻辑）。"""
        if not lease_enabled() or self.epoch(key) is None:
            return
        if cursor.rowcount < expected_rowcount:
            self.discard(key)
            raise LeaseLost("fenced write rejected: doc=%s v=%s" % (key[0], key[1]))

    def verify_still_held(self, cursor, key: DocVersionKey) -> bool:
        """无锁快照验租（不 FOR UPDATE、不 raise）：True=仍持有；False=已失（并本地丢弃）。
        用于不可逆外部副作用（HA3 删除）前的归属过滤——那里不能抱着行锁跨网络调用。
        off/未登记 ⇒ True（不介入，语义=现状放行）。"""
        if not lease_enabled():
            return True
        ep = self.epoch(key)
        if ep is None:
            return True
        cursor.execute(
            "SELECT lease_holder, lease_epoch FROM document_version"
            " WHERE doc_id = %s AND version_no = %s",
            (key[0], key[1]))
        row = cursor.fetchone()
        if row is not None and row[0] == holder_id() and int(row[1] or 0) == ep:
            return True
        self.discard(key)
        return False

    def verify_for_update(self, cursor, key: DocVersionKey) -> None:
        """多语句破坏性写回（write_chunk_meta 的 DELETE→INSERT、deactivate 的
        is_active=0）前，在**同一事务**内 FOR UPDATE 锁 dv 行并验租——通过后
        本事务内不可能再被接管（接管 UPDATE 会阻塞在行锁上直到本事务 commit）。
        off/未登记 ⇒ no-op。"""
        if not lease_enabled():
            return
        ep = self.epoch(key)
        if ep is None:
            return
        cursor.execute(
            "SELECT lease_holder, lease_epoch FROM document_version"
            " WHERE doc_id = %s AND version_no = %s FOR UPDATE",
            (key[0], key[1]))
        row = cursor.fetchone()
        holder = row[0] if row else None
        row_ep = int(row[1]) if row and row[1] is not None else None
        if row is None or holder != holder_id() or row_ep != ep:
            self.discard(key)
            raise LeaseLost("lease verify failed: doc=%s v=%s (held by %s epoch %s, expected %s)"
                            % (key[0], key[1], holder, row_ep, ep))
