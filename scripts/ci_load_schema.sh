#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# scripts/ci_load_schema.sh — 把 schema/ 权威 DDL 按编号顺序灌进本地/CI MySQL 双库
#
# 用途：CI db-integration job（与本地验证）用它从零建出 fuling_knowledge +
# fuling_operation 双库，使 DB 集成测试（tests/local_stack.py 接线的
# requires_local_db 族 + test_concurrency 自探测）不再因缺库 skip。
#
# 与 docker-compose 的 initdb.d 挂载不同：initdb 逐文件以 MYSQL_DATABASE
# （fuling_knowledge）为默认库执行，会把 010/012/013 这些无 USE 语句的
# fuling_operation 文件建错库。本脚本按 schema/README.md 的 file→DB 矩阵
# 显式指定每个文件的目标库；004 是双库混排文件，按其 `-- @DB <db>` 分段标记切开。
#
# 必须用 mysql CLI（不能用 pymysql 整读执行）：002/003b/016 的幂等守卫存储过程
# 用了 DELIMITER $$ —— 那是 mysql 客户端指令，服务端不认。
#
# 环境变量（全部有默认值，默认对准 CI service container / 本地 throwaway 容器）：
#   DB_HOST=127.0.0.1  DB_PORT=3306  DB_USER=root  DB_PASSWORD=your_password
#   MYSQL_BIN=mysql
#
# 安全闸：只允许连本机（127.0.0.1/localhost）。本脚本执行未经 gated 流程的裸 DDL，
# 指向任何远端（生产/staging RDS）都属事故——生产 DDL 只走 scratch/apply_migration_NNN.py
# （information_schema 幂等守卫 + RW token + schema_migrations 台账）。
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-3306}"
DB_USER="${DB_USER:-root}"
DB_PASSWORD="${DB_PASSWORD:-your_password}"
MYSQL_BIN="${MYSQL_BIN:-mysql}"

SCHEMA_DIR="$(cd "$(dirname "$0")/../schema" && pwd)"

die() { echo "❌ $*" >&2; exit 1; }

case "$DB_HOST" in
  127.0.0.1|localhost) ;;
  *) die "DB_HOST='$DB_HOST' 非本机——本脚本只允许灌本地/CI 容器，远端 DDL 走 scratch/apply_migration_NNN.py" ;;
esac

# 密码走 MYSQL_PWD 而非 -p，避免每次调用刷 insecure-password 警告
export MYSQL_PWD="$DB_PASSWORD"
run_sql() {  # $1（可选）= 默认库；SQL 从 stdin 读
  if [ -n "${1:-}" ]; then
    "$MYSQL_BIN" -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" --default-character-set=utf8mb4 --batch --database="$1"
  else
    "$MYSQL_BIN" -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" --default-character-set=utf8mb4 --batch
  fi
}

# ── 1. 等 MySQL 就绪（service container 的 healthcheck 之外再兜一层）──────────
ready=""
for _ in $(seq 1 30); do
  if echo "SELECT 1;" | run_sql >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
[ -n "$ready" ] || die "MySQL ${DB_HOST}:${DB_PORT} 60s 内未就绪"

# ── 2. 显式建双库（README 铁律 4：必须显式 utf8mb4_unicode_ci）────────────────
# 001/002 里的 CREATE DATABASE IF NOT EXISTS 没带 COLLATE，在 MySQL 8 上会默认
# _0900_ai_ci —— staging 就是这么漂移出跨库 JOIN 1267 的。先建好，后面全是 no-op。
run_sql <<'SQL'
CREATE DATABASE IF NOT EXISTS fuling_knowledge CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS fuling_operation CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS fuling_ontology CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
SQL

# ── 3. file→目标库清单（权威矩阵见 schema/README.md）─────────────────────────
#   knowledge/operation = 整文件灌对应库；both = 整文件对两库各跑一遍（011 台账表）；
#   split = 双库混排，按文件内 `-- @DB <db>` 标记分段（目前仅 004）。
MANIFEST="
001_opensearch_pipeline.sql knowledge
002_feedback_system.sql operation
002_step_card_enhancement.sql knowledge
003_provenance_lineage.sql knowledge
003_user_role_unique.sql knowledge
004_observability_metrics.sql split
005_cross_doc_dedup_index.sql knowledge
006_conversation_history.sql operation
006_kb_admin_authz.sql knowledge
007_kb_etag_dedup_index.sql knowledge
008_kb_access_request.sql knowledge
009_acl_projection_outbox.sql knowledge
010_kb_contribution.sql operation
011_schema_migrations.sql both
012_qa_session_log_perf_index.sql operation
013_qa_retrieved_doc_fact.sql operation
014_document_version_raw_key_hash_index.sql knowledge
015_kb_audit_log_history_index.sql knowledge
016_user_feedback_dedup_unique.sql operation
017_qa_admission_reject.sql operation
018_gen_meta_runtime_contract.sql split
019_chunk_meta_index_retry.sql knowledge
020_document_version_simhash.sql knowledge
021_ingest_quality_metrics.sql knowledge
022_agent_runtime.sql operation
023_llm_call_log.sql operation
024_agent_audit_log.sql operation
025_approval_workflow.sql operation
026_agent_family_collation_request_id.sql operation
027_ontology_core.sql ontology
028_ontology_identity.sql ontology
029_ontology_link.sql ontology
030_sem_views.sql ontology
031_agent_approval_execution_hardening.sql operation
032_schema_migrations_checksum.sql both
033_ontology_link_invariants.sql ontology
034_sem_views_product_acl.sql ontology
035_agent_checkpoint_digest_hmac.sql operation
036_agent_run_message_id.sql operation
037_agent_run_serialization.sql operation
038_ontology_object_normalized_title.sql ontology
"

db_of() { case "$1" in knowledge) echo fuling_knowledge ;; operation) echo fuling_operation ;; ontology) echo fuling_ontology ;; *) die "未知目标 '$1'";; esac; }

# 完整性闸：schema/ 里出现清单外的编号文件 = 有人加了迁移没更新本脚本 → 直接红
for f in "$SCHEMA_DIR"/[0-9]*.sql; do
  base="$(basename "$f")"
  echo "$MANIFEST" | grep -q "^$base " \
    || die "schema/$base 不在 loader 清单里——新迁移需同步更新 scripts/ci_load_schema.sh（file→DB 见 schema/README.md）"
done

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

apply_whole() {  # $1=文件 $2=库
  echo "  → $(basename "$1")  ⇒  $2"
  run_sql "$2" < "$1"
}

apply_split() {  # $1=文件：按 `-- @DB <db>` 标记切段，各段灌各库
  local file="$1" n=0 seg db
  # 标记前的导言必须是纯注释/空行——出现可执行语句说明文件结构变了，宁红勿错灌
  local stray
  stray="$(awk '/^--[[:space:]]*@DB[[:space:]]/{exit} !/^[[:space:]]*(--.*)?$/{print; exit}' "$file")"
  [ -z "$stray" ] || die "$(basename "$file") 首个 @DB 标记前有可执行语句：$stray"
  rm -f "$tmpdir"/seg* 2>/dev/null || true
  awk -v out="$tmpdir/seg" '
    /^--[[:space:]]*@DB[[:space:]]+fuling_knowledge/ {n++; print "fuling_knowledge" > (out n ".db"); next}
    /^--[[:space:]]*@DB[[:space:]]+fuling_operation/ {n++; print "fuling_operation" > (out n ".db"); next}
    n>0 {print >> (out n ".sql")}
  ' "$file"
  while :; do
    n=$((n+1)); seg="$tmpdir/seg$n"
    [ -f "$seg.db" ] || break
    db="$(cat "$seg.db")"
    echo "  → $(basename "$file") [段$n]  ⇒  $db"
    run_sql "$db" < "$seg.sql"
  done
  [ "$n" -gt 1 ] || die "$(basename "$file") 未解析出任何 @DB 分段"
}

echo "📥 按编号顺序应用 schema/*.sql → ${DB_HOST}:${DB_PORT}"
echo "$MANIFEST" | while read -r base target; do
  [ -n "$base" ] || continue
  file="$SCHEMA_DIR/$base"
  [ -f "$file" ] || die "清单里的 schema/$base 不存在"
  case "$target" in
    knowledge|operation|ontology) apply_whole "$file" "$(db_of "$target")" ;;
    both)  apply_whole "$file" fuling_knowledge; apply_whole "$file" fuling_operation; \
           apply_whole "$file" fuling_ontology ;;   # 011 台账表：三库各一份（PR-B）
    split) apply_split "$file" ;;
    *) die "schema/$base 目标 '$target' 不合法" ;;
  esac
done

# ── 4. 落库断言：库级 collation + 关键表落对库（错灌是静默的，必须显式验）────
assert_collation() {
  local got
  got="$(echo "SELECT DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='$1';" | run_sql | tail -1)"
  [ "$got" = "utf8mb4_unicode_ci" ] || die "$1 库 collation=$got（应为 utf8mb4_unicode_ci，README 铁律 4）"
}
table_count() {
  echo "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='$1' AND TABLE_NAME='$2';" | run_sql | tail -1
}
assert_table()  { [ "$(table_count "$1" "$2")" = "1" ] || die "预期表 $1.$2 不存在——落错库或应用失败"; }
assert_absent() { [ "$(table_count "$1" "$2")" = "0" ] || die "表 $1.$2 不该存在——文件灌错了库"; }

assert_collation fuling_knowledge
assert_collation fuling_operation
assert_collation fuling_ontology
for t in document_meta document_version chunk_meta pipeline_run user_role dept_admin_grant \
         kb_access_request kb_acl_projection_outbox schema_migrations; do
  assert_table fuling_knowledge "$t"
done
for t in qa_session_log user_feedback escalation_ticket qa_daily_metrics qa_conversation \
         kb_contribution qa_retrieved_doc schema_migrations; do
  assert_table fuling_operation "$t"
done
# 本体独立库（PR-B P0-02）：表族全量落 fuling_ontology，fuling_ro 不授本库
for t in ontology_object ontology_identifier ontology_resolution_case \
         ontology_resolution_candidate ontology_link ontology_ref_seq \
         ontology_attribute_source ontology_stewardship ontology_source_population \
         sem_packing sem_stacking schema_migrations; do
  assert_table fuling_ontology "$t"
done
# 错灌金丝雀（qa_session_log/user_feedback 合法地双库都有——001 初版在 knowledge，不做金丝雀）
assert_absent fuling_knowledge qa_daily_metrics
assert_absent fuling_knowledge kb_contribution
assert_absent fuling_knowledge ontology_object
assert_absent fuling_operation chunk_meta
assert_absent fuling_operation ontology_object
assert_absent fuling_ontology qa_session_log
assert_absent fuling_ontology chunk_meta

echo "✅ schema 三库加载完成并通过落库断言"
