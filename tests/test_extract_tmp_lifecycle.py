# -*- coding: utf-8 -*-
"""stage-1 抽取的临时盘生命周期(2026-08-06,Claude↔Codex 六阶段评审共识)。

两件事,顺序不可换:

**PR-1 —— `finally` 可达性(现存缺陷)。** `_get_oss_bucket` / `_upload_clean_assets` 原先在
`finally` 里**裸调用**,而其后的两个 cache flush 与 `rmtree` 各自有 try:二者任一抛出就会让
控制流直接离开 finally ⇒ **`rmtree` 永不执行 ⇒ 整批临时目录泄漏**。单文件上限提到 300MB 后
一次泄漏可达 30-40GB。修的是**可达性,不是失败语义** —— 上传异常照旧向外传播(吞掉会把
「node 应当失败」变成「静默成功」)。

**PR-2 —— 原件提前释放。** 抽取返回后原件再无消费者,却要躺到批末。四重校验后立即删。
第 3 条(`realpath` 落在批次 tmp 内)是防「删掉仓库语料」的那道闸:LOCAL-DEV 把
`scratch/sample_corpus/` 挂成 local_path、run_simulation 直接引用 git 跟踪的
`fuling_chunk_exp/*.docx` —— 朴素的「删 task['local_path']」会把它们一起删了。
"""
import os

import pytest

from opensearch_pipeline import pipeline_nodes as pn


# ─────────────────────────── PR-2 · 原件提前释放 ───────────────────────────

def _owned(tmp_path, name="DOC1_sop.pdf", data=b"x" * 2048):
    f = tmp_path / name
    f.write_bytes(data)
    return {"doc_id": "DOC1", "local_path": str(f), "_owned_local_path": str(f)}


def test_释放_tmp_内的_owned_原件(tmp_path):
    task = _owned(tmp_path)
    n = pn._release_owned_raw(task, str(tmp_path))
    assert n == 2048
    assert not os.path.exists(task["_owned_local_path"])


def test_绝不删_tmp_之外的路径(tmp_path):
    """**本文件最重要的一条**。LOCAL-DEV 的 scratch/sample_corpus 与 run_simulation 的
    fuling_chunk_exp/*.docx 都在 tmp 之外且是**真实语料**(后者还被 git 跟踪)。
    即便有人错误地登记了 ownership,containment 校验也必须挡住。"""
    outside = tmp_path / "repo_corpus"
    outside.mkdir()
    corpus = outside / "作业指导书.docx"
    corpus.write_bytes(b"real corpus")
    batch_tmp = tmp_path / "rag_extract_x"
    batch_tmp.mkdir()

    task = {"doc_id": "D", "local_path": str(corpus), "_owned_local_path": str(corpus)}
    assert pn._release_owned_raw(task, str(batch_tmp)) == 0
    assert corpus.exists(), "tmp 之外的真实语料被删除了 —— 这会毁掉仓库/采样语料"


def test_没有_ownership_登记就不删(tmp_path):
    """采样语料分支(pipeline_nodes 的 simulate_oss 挂载)不经过下载,拿不到登记。"""
    f = tmp_path / "sampled.pdf"
    f.write_bytes(b"data")
    assert pn._release_owned_raw({"doc_id": "D", "local_path": str(f)}, str(tmp_path)) == 0
    assert f.exists()


def test_登记路径与当前路径不一致就不删(tmp_path):
    f, other = tmp_path / "a.pdf", tmp_path / "b.pdf"
    f.write_bytes(b"a")
    other.write_bytes(b"b")
    task = {"doc_id": "D", "local_path": str(other), "_owned_local_path": str(f)}
    assert pn._release_owned_raw(task, str(tmp_path)) == 0
    assert f.exists() and other.exists()


def test_symlink_指向_tmp_外_绝不删(tmp_path):
    """真实风险场景:tmp 内一条链接指向仓库语料。
    ⚠️ 实测(变异验证)**挡住它的是 containment 而非 islink 校验** —— `realpath` 已把链接
    解析成 tmp 外的目标,第 3 条就否决了。这条测的是**结果**,不是机制;
    islink 的独立价值见下一条。"""
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"corpus")
    batch = tmp_path / "rag_extract_y"
    batch.mkdir()
    link = batch / "DOC1_outside.docx"
    link.symlink_to(outside)

    task = {"doc_id": "D", "local_path": str(link), "_owned_local_path": str(link)}
    assert pn._release_owned_raw(task, str(batch)) == 0
    assert outside.exists() and link.exists()


def test_symlink_指向_tmp_内_也不删(tmp_path):
    """隔离 islink 校验本身:链接与目标**都在 tmp 内** ⇒ containment 放行,
    此时只有 islink 能拦。不拦的话 `os.remove(realpath)` 删掉的是**目标文件**
    (可能是另一篇文档的原件或已导出的资产),留下一条悬空链接 —— 删错对象。"""
    batch = tmp_path / "rag_extract_z"
    batch.mkdir()
    target = batch / "OTHER_doc.pdf"
    target.write_bytes(b"another doc")
    link = batch / "DOC1_alias.pdf"
    link.symlink_to(target)

    task = {"doc_id": "D", "local_path": str(link), "_owned_local_path": str(link)}
    assert pn._release_owned_raw(task, str(batch)) == 0
    assert target.exists(), "删到了 symlink 的目标 —— 那是别人的文件"


def test_目录不删(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    task = {"doc_id": "D", "local_path": str(d), "_owned_local_path": str(d)}
    assert pn._release_owned_raw(task, str(tmp_path)) == 0
    assert d.is_dir()


@pytest.mark.parametrize("val", ["off", "0", "false", "no", "OFF", "False"])
def test_flag_显式关闭时不释放(tmp_path, monkeypatch, val):
    monkeypatch.setenv("RAG_EXTRACT_RELEASE_RAW", val)
    task = _owned(tmp_path)
    assert pn._release_owned_raw(task, str(tmp_path)) == 0
    assert os.path.exists(task["_owned_local_path"])


def test_flag_默认为开(tmp_path, monkeypatch):
    """默认 ON 是有意的:只写在 DataWorks 节点 setdefault 的 flag 对笔记本重跑无效,
    要两条执行路径都保住就必须默认 ON(本仓既有教训)。"""
    monkeypatch.delenv("RAG_EXTRACT_RELEASE_RAW", raising=False)
    task = _owned(tmp_path)
    assert pn._release_owned_raw(task, str(tmp_path)) > 0


def test_删除失败只告警不抛(tmp_path, monkeypatch, capsys):
    """资源优化绝不能把一篇**成功的抽取**变成失败文档。"""
    task = _owned(tmp_path)
    monkeypatch.setattr(pn.os, "remove",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("EBUSY")))
    assert pn._release_owned_raw(task, str(tmp_path)) == 0
    assert "原件提前释放失败" in capsys.readouterr().out


def test_不清除任何_local_path_字段(tmp_path):
    """`ocr_text` block 的 extra['local_path'] 是 chunker 区分「图片 OCR」与
    「整页 OCR fallback」的 **load-bearing 判据**(chunker.py:863)——独立图片文档的该
    block 没有 source_image,清掉它会让 OCR 文本 fallthrough 进步骤正文。
    评审里我方据此推翻了「顺手清理悬空 local_path」的提议,这条钉住该结论。"""
    task = _owned(tmp_path)
    before = dict(task)
    pn._release_owned_raw(task, str(tmp_path))
    assert task["local_path"] == before["local_path"]
    assert task["_owned_local_path"] == before["_owned_local_path"]


# ─────────────────────────── PR-1 · 清理可达性 ───────────────────────────

def test_清理失败必须可见(tmp_path, capsys):
    """原实现 `rmtree(ignore_errors=True)` 外套 `except: pass` —— ignore_errors 已吞掉一切,
    外层 except 是死代码,磁盘泄漏零日志零指标。

    ⚠️ **用真 rmtree**,不 monkeypatch:monkeypatch 成恒抛的话 `ignore_errors` 根本不参与,
    把它改回 True 测试照样绿(本文件首版实测踩中 —— 变异验证 M7 未变红)。
    这里传一个不存在的目录:ignore_errors=False ⇒ 抛 FileNotFoundError 被我们捕获并打印;
    ignore_errors=True ⇒ 静默通过。断言据此能区分两者。"""
    pn._cleanup_batch_tmp_dir(str(tmp_path / "never_created"))   # 绝不能抛
    out = capsys.readouterr().out
    assert "清理失败" in out, "rmtree 失败被静默吞掉了(ignore_errors=True?)"


def test_清理失败日志含路径与异常类型(tmp_path, monkeypatch, capsys):
    """诊断需要:路径 / 异常类型 / 清理前字节数三样都要有。"""
    d = tmp_path / "batch"
    d.mkdir()
    (d / "big.bin").write_bytes(b"z" * 4096)

    import shutil
    monkeypatch.setattr(shutil, "rmtree",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("EPERM")))
    pn._cleanup_batch_tmp_dir(str(d))
    out = capsys.readouterr().out
    assert str(d) in out and "OSError" in out and "MB" in out


def test_清理助手永不抛出(tmp_path, monkeypatch):
    """由 finally 调用 ⇒ 抛出会**覆盖正在向外传播的原始异常**(上传失败的真实根因
    会被一个 rmtree 报错顶掉)。"""
    monkeypatch.setattr(pn, "_dir_size_bytes",
                        lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))
    pn._cleanup_batch_tmp_dir(str(tmp_path))   # 不抛即通过


def test_目录字节统计_failopen(tmp_path):
    """纯观测:统计失败必须回 -1,绝不能阻止 rmtree。"""
    assert pn._dir_size_bytes(str(tmp_path / "不存在")) in (0, -1)


def test_finally_嵌套_上传异常不吞且清理仍可达():
    """PR-1 的核心断言,直接钉源码结构:
      · 取 bucket / 上传资产必须在**内层 try** 里(异常照旧传播);
      · 两个 cache flush 与清理必须在其 **finally** 里(永远可达)。
    用源码断言而非跑整个 node,是因为构造一个能走到 finally 的完整 ctx 需要 OSS/DB 桩,
    那样的测试更脆且验的是桩不是结构。"""
    import inspect
    import re

    src = inspect.getsource(pn.node_extract_text_with_ocr)
    tail = src[src.index("    finally:"):]
    up = tail.index("_upload_clean_assets(extractions")
    # 上传之后必须还有一个 finally,且清理与两个 flush 都在它之后
    rest = tail[up:]
    assert re.search(r"\n\s+finally:", rest), "上传没有被内层 try/finally 包住 ⇒ 清理不可达"
    fin = rest.index("finally:")
    for must in ("flush_vlm_cache_to_oss", "flush_page_cache_to_oss",
                 "_cleanup_batch_tmp_dir("):
        assert rest.index(must) > fin, f"{must} 不在内层 finally 中 ⇒ 上传抛出时会被跳过"
    assert "except Exception" not in rest[:fin], (
        "上传段被 except 吞掉了 —— 那会把「node 应当失败」变成「静默成功」")


def test_释放调用点在成功路径且线程安全累加():
    """单篇异常时不在异常点删(交批末兜底);并发路径用 list.append 而非 `+=`
    (后者在多线程下非原子会丢计数)。"""
    import inspect

    src = inspect.getsource(pn.node_extract_text_with_ocr)
    assert "_released_bytes.append(_release_owned_raw(task, tmp_dir))" in src, \
        "释放未接线,或用了非线程安全的累加方式"
    # 释放必须在 _extract_one 内、return result 之前 ⇒ 异常路径走不到
    i_rel = src.index("_released_bytes.append(")
    i_ret = src.index("return result", i_rel)
    assert i_ret > i_rel
    assert "[disk] 原件提前释放" in src, "缺批末可观测日志,无法从节点日志验证是否生效"
