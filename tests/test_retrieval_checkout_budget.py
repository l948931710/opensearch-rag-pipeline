# -*- coding: utf-8 -*-
"""D4-①：每请求 RDS checkout **预算**断言（2026-08-06）。

为什么要有这条：`docs/main_code_review_verification_2026-08-06.md` §5.1 说
「一次检索 8 处独立 checkout」——那是**静态调用点计数**。真跑下来默认只有 3 次
（多处互斥、且 `_conn_scope` 已把 stitch+expand 合成 1 次）。整个 D1「合并连接」批次
就是因为这个数字才被撤销的。

但仓里**此前零处**钉住这个数：谁再加一个自取连接的消费点，没有任何东西会红。
本文件把它变成预算。

⚠️ **这是预算断言，不是性能断言**：数字变大不代表一定错，但必须有人看一眼、
   并显式改掉这里的期望值 —— 而不是悄悄多一次 checkout。
⚠️ 口径：桩连接只量**次数与线程归属**，不量 SQL 往返延迟与 pool wait。
"""
import threading
import traceback

import pytest


def _skip_if_not_sim():
    import os
    if os.environ.get("RAG_SIMULATE", "true").lower() not in ("1", "true", "yes"):
        pytest.skip("需要 simulate 环境")


class _Cur:
    def __init__(self, conn, as_dict=False):
        self.conn, self.as_dict, self._rows = conn, as_dict, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._rows = self.conn.rows_for(" ".join(str(sql).split()).lower(), self.as_dict)
        return len(self._rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    """每次 `_get_db_conn()` 产一个新实例 = 一次 checkout；记录线程与消费点。"""

    def __init__(self, ledger):
        self.ledger = ledger
        ledger.append((threading.current_thread().name, _caller()))

    def cursor(self, cursor_class=None, *a, **k):
        return _Cur(self, as_dict=bool(cursor_class))

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def rows_for(self, low, as_dict):
        if as_dict:
            if "chunk_index" in low or "parent_chunk_id" in low or "step_no" in low:
                return [{"doc_id": "D1", "version_no": 1, "chunk_index": i, "chunk_id": f"c{i}",
                         "chunk_text": f"t{i}", "chunk_type": "text", "image_refs_json": None,
                         "parent_chunk_id": None, "step_no": None} for i in (1, 2, 3, 4)]
            return []
        if "is_active" in low and "permission_level" in low and "chunk_meta" in low:
            return [(f"c{i}", 1, "public", "") for i in (1, 2, 3)]
        if "created_at" in low and "document_version" in low:
            return [("D1", "2026-08-01")]
        return []


def _caller() -> str:
    """checkout 归属到最近的 retriever 消费点，而不是 db 层——预算超了要能一眼看出是谁。"""
    import os
    for fr in reversed(traceback.extract_stack()[:-2]):
        if fr.filename.endswith(("retriever.py", "access_grants.py")):
            return f"{os.path.basename(fr.filename).split('.')[0]}.{fr.name}"
    return "<unknown>"


class _Ha3:
    """HA3 客户端桩：三臂融合走 `.search()`，cosurface 走 `.query()`；返回值由
    `_parse_ha3_response` 的桩接管，这里只需要方法存在。"""

    def search(self, *a, **k):
        return None

    def query(self, *a, **k):
        return None


def _hits(with_image=False):
    # ⚠️ 必须含一条 `step_card`：`expand_step_context:1961-1966` 的 `need_expand` 为假时**整段早退**，
    # 连 `_stitch_expand_conn` 都不调 —— 第一版 fixture 全是 text，于是「stitch/expand 共享连接」
    # 那条断言恒为 1、变异（把 `_conn_scope` 关掉）照样绿。**假绿**，2026-08-06 变异抓出。
    out = [{"chunk_id": f"c{i}", "doc_id": "D1", "chunk_index": i, "chunk_text": f"t{i}",
            "score": 1.0 / i, "permission_level": "public", "owner_dept": "",
            "chunk_type": ("step_card" if i == 1 else "text"),
            "parent_chunk_id": ("p1" if i == 1 else None), "step_no": (1 if i == 1 else None),
            "version_no": 1} for i in (1, 2, 3)]
    if with_image:
        out.append({"chunk_id": "ci", "doc_id": "D1", "chunk_index": 9, "chunk_text": "",
                    "score": 0.4, "permission_level": "public", "owner_dept": "",
                    "chunk_type": "image", "source_image": "oss://x.png", "version_no": 1})
    return out


def _measure(monkeypatch, *, with_image=False, cosurface=False):
    """跑一趟 retrieve_and_enrich，回 [(线程名, 消费点), ...]。

    `search_chunks` **不桩**：它内部的三个 authority 策略要一起计入预算。
    只桩 HA3 客户端/解析/embedding，并给 endpoint 假值避开本地 OpenSearch 回退分支
    （那是另一条执行路径，与生产不同）。
    """
    _skip_if_not_sim()
    from opensearch_pipeline import retriever as R
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    ledger = []
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg, "simulate_opensearch", True)
    monkeypatch.setattr(cfg.alibaba_vector, "endpoint", "http://dummy-ha3")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _Conn(ledger))
    monkeypatch.setattr(R, "get_query_embedding", lambda *a, **k: ([0.1] * 8, [1], [0.5]))
    monkeypatch.setattr(R, "_get_ha3_client", lambda *a, **k: _Ha3())
    monkeypatch.setattr(R, "_parse_ha3_response", lambda *a, **k: _hits(with_image))
    R.retrieve_and_enrich("预算探测", top_k=3, cosurface_images=cosurface)
    return ledger


# 预算：改这三个数字前请先回答「为什么多出来的那次 checkout 是必要的」。
_BUDGET_DEFAULT = 3
_BUDGET_IMAGE = 3


def test_default_path_checkout_budget(monkeypatch):
    """默认纯文本全链 = 3 次：主命中复核 / stitch+expand（已共享）/ doc_date。"""
    led = _measure(monkeypatch)
    assert len(led) == _BUDGET_DEFAULT, (
        f"每请求 checkout 预算被打破：期望 {_BUDGET_DEFAULT}，实得 {len(led)} —— {led}\n"
        "多半是新增了一个自取连接的消费点，或 `_conn_scope` 的覆盖窗口变了。")
    assert {c for _, c in led} == {
        "retriever._revalidate_main_hits",
        "retriever._stitch_expand_conn",
        "retriever._attach_doc_dates",
    }, f"消费点集合变了：{sorted({c for _, c in led})}"


def test_image_path_checkout_budget(monkeypatch):
    """含图输入不得让预算膨胀（版本门在全链下与主命中复核共处一条路径）。"""
    led = _measure(monkeypatch, with_image=True)
    assert len(led) == _BUDGET_IMAGE, f"含图路径预算被打破：实得 {len(led)} —— {led}"


def test_stitch_and_expand_still_share_one_connection(monkeypatch):
    """`_conn_scope` 是本仓**已经**做对的那次合并（D1 撤销的前提）——不许被改回去。

    stitch 与 expand 两阶段必须只 checkout 一次；退回成各取各的，这里立刻红。
    """
    led = _measure(monkeypatch)
    n = sum(1 for _, c in led if c == "retriever._stitch_expand_conn")
    assert n == 1, f"stitch/expand 不再共享连接（checkout×{n}）—— `_conn_scope` 被破坏"


def test_all_default_path_checkouts_are_on_the_calling_thread(monkeypatch):
    """默认路径的 checkout 全在调用线程上。

    这条钉的是**可合并性的前提**：一旦有消费点跑到 worker 线程上，
    「请求级共享一条连接」就不再安全（pymysql 连接非线程安全）。
    ⚠️ 已实测：`multi_query_mode` 打开后 `_revalidate_main_hits` 确实会落到
    `ThreadPoolExecutor` 的 worker 上——所以本断言**只覆盖默认配置**。
    """
    led = _measure(monkeypatch)
    me = threading.current_thread().name
    assert {t for t, _ in led} == {me}, f"默认路径出现了跨线程 checkout：{led}"
