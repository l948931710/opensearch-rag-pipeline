# -*- coding: utf-8 -*-
"""
routes/ — api.py 的冷域 APIRouter 拆分（F-A2，2026-07-01）

拆分规则（破坏即断 tests 或 monkeypatch）：
  1. 路由模块在 api.py **全量初始化后**（文件底部）才被导入——模块顶层可以安全
     `from opensearch_pipeline.api import ...` 共享模型/助手。
  2. 路由模块**不得定义、遮蔽或调用** tests 对 api 做 monkeypatch 的属性
     （retrieve_and_enrich / log_qa_session / generate_answer* / _append_to_history /
      build_*_blocks / content_blocks_to_json / handle_feedback /
      _resign_visible_doc_ids / _success_question_pool / refresh_image_block_urls /
      search_chunks）。凡引用这些名字的端点（ask/stream/session/feedback/history/
      hot-questions）一律留在 api.py。
  3. api.py 对搬出的端点函数与域内模型做 re-export（tests 直接 `api.kb_stats(...)`）。
"""
