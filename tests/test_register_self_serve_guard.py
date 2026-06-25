# -*- coding: utf-8 -*-
"""register_new_files.py 的自助上传孤儿守卫（#1）回归锁。

register_new_files.py 是 PyODPS 节点脚本（顶层会连 OSS/RDS），不能直接 import；
故用 AST 抽取 is_self_serve_raw_key + 其依赖的 _SELF_SERVE_DOC_SEG 正则，在隔离命名空间 exec。

守卫意图：批量注册工具绝不收编 raw/<dept>/DOC_<ULID26>/<upload_id>/<file>
（/api/kb/register 独占）。否则 PUT 成功但 register 未完成的孤儿会被硬编码 permission_level='public'
并绕过 kb_admin 审批门，把本应 dept_internal/待审批的件放成全公司可见。
"""
import ast
import re as _re
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "dataworks_nodes" / "register_new_files.py"
_ASSIGN = "_SELF_SERVE_DOC_SEG"
_FUNC = "is_self_serve_raw_key"


def _load_guard():
    """从脚本源码用 AST 只抽出正则赋值 + 守卫函数，隔离 exec（不触发脚本顶层副作用）。"""
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    ns = {"re": _re}
    for node in tree.body:
        is_assign = isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == _ASSIGN for t in node.targets
        )
        is_func = isinstance(node, ast.FunctionDef) and node.name == _FUNC
        if is_assign or is_func:
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod, str(_SCRIPT), "exec"), ns)
    assert _FUNC in ns, "未能从 register_new_files.py 抽出守卫函数"
    return ns[_FUNC]


is_self_serve = _load_guard()
_ULID = "01J9ZXC8K3QF7M2N4P5R6S7T8V"   # 26 位 Crockford base32


def test_self_serve_shape_is_skipped():
    assert is_self_serve(f"raw/marketing/DOC_{_ULID}/{_ULID}/方案.pdf") is True


def test_legacy_and_pipeline_keys_not_skipped():
    assert is_self_serve("raw/marketing/某营销方案.pdf") is False                 # 遗留扁平 key
    assert is_self_serve("raw/production/sop/作业指导书.docx") is False            # 遗留含子目录
    assert is_self_serve("raw/finance/DOC_FINANCE_20260101_AB12CD/x.pdf") is False  # 本工具旧 doc_id 形状


def test_directory_marker_and_non_crockford_not_skipped():
    assert is_self_serve(f"raw/hr/DOC_{_ULID}/up/") is False                      # 目录标记(末段空)
    assert is_self_serve("raw/it/DOC_01J9ZXC8K3QF7M2N4P5R6S7T8I/up/a.pdf") is False  # 含非 Crockford 'I'
    assert is_self_serve("") is False
    assert is_self_serve(None) is False
