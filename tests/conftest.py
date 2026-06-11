# -*- coding: utf-8 -*-
"""全局测试装置。

在收集任何测试模块之前接线本地 dev 栈（见 tests/local_stack.py）：
凭证/地址修正必须先于一切存储集成测试（含各模块 import 期的可用性探测）发生。
"""

from tests.local_stack import ensure_local_db_wired, ensure_local_opensearch_wired

ensure_local_db_wired()
ensure_local_opensearch_wired()
