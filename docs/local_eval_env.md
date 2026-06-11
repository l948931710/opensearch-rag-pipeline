# 本地评测环境 Runbook

> 本地受控 A/B 评测环境的地图与操作手册（2026-06-10 整理）。背景与实验结论见
> [local_ab_controlled_20260610.md](../eval_harness/reports/local_ab_controlled_20260610.md)。
> 本环境在六层环境矩阵中对应 **LOCAL-EVAL** 层，上位设计（环境分工/凭证分离/守卫）见
> [environment_design.md](environment_design.md)。

## 一键操作

```bash
make ab-up        # 启动双实例（:8001 新管线 / :8002 旧对照），pid/日志在 logs/local_eval/
make ab-status    # 总览：实例健康、索引 doc 数、MySQL 前缀计数、各臂解析配置
make ab-smoke     # 双端各问 1 题（BIND-01），断言命中预期文档
make ab-down      # 停双实例；make ab-down ALL=1 额外清 :8000
```

实现：[scripts/local_eval_env.sh](../scripts/local_eval_env.sh)。

## 环境地图

### 容器（前置依赖，脚本会预检）

| 容器 | 端口 | 内容 |
|---|---|---|
| `rag-mysql-local` | 3306 | 库 `fuling_knowledge`（root 密码在容器 env `MYSQL_ROOT_PASSWORD`） |
| `rag-opensearch-local` | 9200 | 本地 OpenSearch（dense kNN + BM25 两路，**无 sparse**） |

### 端口约定

| 端口 | 角色 | RAG_ENV | 索引 |
|---|---|---|---|
| :8001 | **新管线**（本地回灌产物） | `local_ab_new` | `locale2e_v1`（2,074 chunks，1,348 step_card） |
| :8002 | **旧对照**（生产 active chunks 镜像） | `local_ab_old` | `locale2e_old_v1`（1,758 chunks，0 step_card） |
| :8000 | 连**生产 HA3** 的 test 服务 | `test` | 生产索引（默认不开；需要时手动 `RAG_ENV=test uvicorn ... --port 8000`） |

### MySQL doc_id 前缀语义（库 fuling_knowledge）

| 前缀 | 含义 | 规模 |
|---|---|---|
| `LOCALE2E_` | 新管线实验组（124 docs 本地完整回灌 DAG1→3） | 2,074 chunks |
| `LOCALE2EOLD_` | 旧管线对照组（生产 RDS 只读镜像，chunk_id/parent_chunk_id 同前缀改写） | 1,758 chunks |
| 其他 | 已清理（2026-06-10 删除 79 条历史测试遗留） | 0 |

### env overlay 清单

| 文件 | 用途 |
|---|---|
| `.env.local` | 本地基础（localhost MySQL/OpenSearch、真实 OSS/DashScope key） |
| `.env.local_ab_new` / `.env.local_ab_old` | 由 .env.local 派生，仅 `RAG_OPENSEARCH_INDEX` 不同 + `RAG_RERANK_ENABLE=true`。**含密钥，已 gitignore**；缺失时按脚本预检提示重新派生 |
| `.env.test` / `.env.production` | 生产侧凭证（镜像脚本只读用） |

## ⚠️ 关键陷阱

1. **`.env.{RAG_ENV}` 以 `override=True` 加载，会覆盖 shell 导出的环境变量**（config.py）。
   切索引/开关 rerank 必须改 overlay 文件，`export RAG_OPENSEARCH_INDEX=...` 无效。
2. 摄取推送的索引名现在随 `RAG_OPENSEARCH_INDEX` 配置走（2026-06-10 修复了三处硬编码
   `fuling_knowledge_v1` 回退）。**跑 stage-3 前确认 RAG_ENV 指向正确 overlay**，否则推错索引。
3. 本地 serving 写 `qa_session_log` 会报 `Unknown database 'fuling_operation'` —— 优雅降级，
   不影响答案；本地无该库属预期。
4. `scratch/embedding_cache.json`（~190MB）是重建索引近零成本的关键，**不要删**；
   `scratch/vlm_cache.json` 同理（VLM 配额）。
5. 评测脚本 seed=20260610 固定 A/B 翻转序列；改题集或重采集后 bundle 的 sealed 映射会变，
   盲评必须用 `local_ab_judge_input.json`（不含 sealed），不要把 bundle 原文件喂给评审。

## 常用操作

### 复现盲评中的任意一题

```bash
make ab-up
curl -s -XPOST localhost:8001/api/ask -H 'Content-Type: application/json' \
  -d '{"question":"注塑成品收货报检的步骤是什么？","user_id":"manual"}' | python3 -m json.tool
# :8002 同理 = 旧管线的回答
```

### 重建实验组索引（locale2e_v1，新管线产物变更后）

```bash
# 1) 重置状态（只动 LOCALE2E_ 行）
PW=$(docker exec rag-mysql-local printenv MYSQL_ROOT_PASSWORD)
docker exec rag-mysql-local mysql -uroot -p"$PW" fuling_knowledge -e "
  UPDATE chunk_meta SET index_status='NOT_INDEXED', index_name=NULL, opensearch_doc_id=NULL, indexed_at=NULL
  WHERE doc_id LIKE 'LOCALE2E\_%' AND is_active=1;"
# 2) 删旧索引让新 mapping 生效（可选）
curl -XDELETE localhost:9200/locale2e_v1
# 3) 重推（embedding 走缓存，分钟级）
RAG_ENV=local_ab_new python3 -m opensearch_pipeline.dataworks_orchestrator --stage 3 --bizdate $(date +%Y%m%d)
```

> 注意：stage-3 会捞**所有** `NOT_INDEXED` 的 active chunk。重建某一臂前先 `make ab-status`
> 确认另一臂全部 INDEXED，避免串臂。

### 对照组重灌（生产旧 chunks 变化后）

```bash
RAG_ENV=local python3 scratch/local_e2e_old_mirror.py --wipe       # 清本地对照组行
RAG_ENV=local python3 scratch/local_e2e_old_mirror.py --preview    # 只读统计生产规模
RAG_ENV=local python3 scratch/local_e2e_old_mirror.py --register   # 只读镜像（需生产只读授权）
curl -XDELETE localhost:9200/locale2e_old_v1
RAG_ENV=local_ab_old python3 -m opensearch_pipeline.dataworks_orchestrator --stage 3 --bizdate $(date +%Y%m%d)
```

### 完整重跑受控 A/B 评测

```bash
make ab-up && make ab-smoke
python3 scratch/local_ab_eval.py collect    # 25 题双端采集（~20 分钟）
python3 scratch/local_ab_eval.py bundle     # 密封盲评包
# 生成无映射评审输入 → 3 个独立盲评（输出 local_ab_judge{1,2,3}.json，格式见上轮）
python3 -c "import json; b=json.load(open('scratch/local_ab_bundle.json')); \
  json.dump({'items':b['items']}, open('scratch/local_ab_judge_input.json','w'), ensure_ascii=False, indent=1)"
# 合并 panels → 解盲聚合
python3 scratch/local_ab_report.py
```

### 全套单测

```bash
make test   # 注意：基线有 24 个与本机环境相关的既有失败（HA3 mock/VLM 类），属已知
```

## 与生产的关系

- 本环境是**受控 A/B**（内部效度），不是生产预演：本地两路检索无 sparse，分数量纲与 HA3 不同。
- 生产回灌后的同题集复测仍是最终闭环（清单见 local_ab_controlled 报告 §效度边界）。
- 涉及生产的操作只有 `local_e2e_old_mirror.py --preview/--register`（只读 SELECT），
  生产写入走 `/fuling-ha3-rebuild` runbook，与本环境无关。
