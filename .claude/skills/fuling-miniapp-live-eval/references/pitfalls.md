# 实战坑清单（每条都真实付过学费）

1. **改码不重启 = 验证幽灵版本**。uvicorn 不热加载；llm_generator/retriever/
   content_blocks_builder 任何改动后先 `pkill -f "uvicorn opensearch_pipeline.api:app"`
   再起。诊断探针脚本是新进程不受影响——这正是"探针对、UI 不对"时第一个该想到的差异。

2. **打字机渲染中读最后气泡 = 读到半成品或旧答案**。取证 JS 必须带 `n_msgs` 并与
   "问候1 + 已问×2 + 1"核对；上一题的旧气泡仍在 DOM 里，少等一轮就会把退役前的
   旧答案当成新结果（2026-06-11 真实闹过：以为孪生退役无效，其实读的是退役前气泡）。

3. **UI 单次拒答 ≠ 回归**。先 API 直连同问题复测（甚至两种措辞），都正常即偶发
   （已知 ~11% 偶发尾部）。把偶发当回归会引发无意义的代码回滚。

4. **金集标题别名漂移 → 假阴**。goldset `expected_docs` 与实际标题有"财务操作手册
   vs 财务**部**操作手册"级差异，裸 `in` 子串匹配必假阴；专项脚本一律先归一化
   （去"部"/空格/扩展名）或走 `eval_harness/matching.py`。

5. **sources 展示分 ≠ guard 判定分**。扩展兄弟 chunk 继承原命中分 ×0.85 展示，
   而 guard 用 rerank_score（0-1）max 对 0.8 比。拿 sources 的 0.69 推断"该进
   低置信带"是误读（曾据此怀疑出一个不存在的 bug）。

6. **孪生文档双活会污染一切下游判断**：检索面灌满、LLM 把引用浪费在重复表单、
   后位步骤图被挤出。诊断图片/引用问题前先跑一把 `img_docs` 归属或孪生 SQL。
   退役孪生必须 **RDS + HA3 双侧**（HA3 不删会继续服务到下次 清理stage3）。

7. **OSS 资产路径别猜前缀**。assets 路径含部门段（如 `production_thermoforming`
   不是 `production`），猜错列出来是空的还以为资产丢了。从 chunk 的
   `image_refs_json[].oss_key` 反查真实前缀。

8. **`sleep N` 会被环境拦截**。等待一律 until 循环 gate（见 environment.md §时序），
   不要试图串短 sleep 绕过。

9. **zsh 的 `===`/`==` 是展开语法**。Bash 工具里 `echo ===` 会报 "== not found"，
   分隔符用 `---`。

10. **探针的 config 属性位置**：rerank_enable/rerank_pool 在
    `cfg.alibaba_vector`，max_context_chars/default_top_k/阈值/守卫在 `cfg.rag`——
    写探针时引用错对象会 AttributeError。

11. **caption 截断陷阱**：盘点图片时 `visual_summary[:46]` 看着像"手持记录单"，
    全文末尾才写着"红色激光扫描线"。下"图不存在/不相关"结论前必看全文。

12. **LLM 引用位置 ≠ 绑定步骤**。图按 `<<IMG:N>>` 占位符位置穿插，LLM 可能把
    step4 的图引在第 2 步正文后——图"在但位置怪"通常不是 bug，先确认归属
    （oss_key/doc_id）再评位置合理性。

13. **环境变量 override 方向**：`RAG_ENV` overlay 文件优先（override=True）覆盖
    shell 导出；shell 设置只对 overlay 文件里**没有**的变量生效。本 skill 的四个
    后端变量恰好都不在 .env.prod_ro 内，所以可用——新增变量前先确认不撞名。

14. **退役/重置类生产写**：一律预览断言行数 → 用户授权 → commit → 行数复核，
    影响数不符立即回滚（模板见 `scratch/retire_twin_wi007_docx.py` 与
    `scratch/deactivate_dup_twin_342731.py`）。
