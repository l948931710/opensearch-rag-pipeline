# Main + Agent v2 Production-Readiness Review — 2026-07-21

## Executive verdict

**REVISE / NO-GO. Do not release Agent v2 or merge it as a release candidate yet.**

Agent v2 has strong foundations—broad tests, real-MySQL CI contracts, bounded execution,
durable-dispatch machinery, production posture guards, dependency locking, SBOM/security gates,
and a substantial console—but several verified defects can still produce unauthorized or
out-of-contract writes, stale-approval execution, confidential-data egress, unsafe migration replay,
or a green deployment that cannot serve Agent traffic.

Current `main` is the safer RAG baseline because it contains several July 21 reconciliation,
identity, readiness, and migration fixes missing from Agent v2. It is not, however, a self-proving
fresh production artifact: authentication is default-off, verified RDS TLS is not mandatory, its
README overstates multi-replica capabilities, and live deployment/monitoring posture was not
verified in this review.

A limited **read-only Agent canary** may become reasonable after the branches are integrated,
non-write blockers are closed, current live baselines are frozen, and the actual deployment topology
is proven. **HIGH_WRITE must remain disabled** until execution ownership and approval invariants are
fixed and survive staging failure injection.

## Scope and reviewed revisions

| Target | Revision | State |
|---|---|---|
| `main` | `bcc7acb5189d11b2bba6b0696abd9d2104e90823` | Matches `origin/main`; tracked tree unchanged |
| Agent v2 worktree | `9d7a6318c6bd8d07698da88ece3e44944506fd56` | Branch `claude/ontology-p0`; matches origin; clean |
| Merge base | `9b09aaa66425929e527cf3c3fc5eb15ec941ecfd` | Branches diverged materially |

Agent v2 is 265 commits ahead of and 30 commits behind `main`. The merge-base comparison spans
444 files and approximately 74,315 additions / 1,170 deletions. A read-only three-way merge
projection found textual conflicts in 23 files, including production hot paths (`api.py`,
`config.py`, `qa_logger.py`, `clients.py`), schema/CI controls, and 11 console files.

The review covered:

- branch integration and regression risk;
- runtime ownership, concurrency, cancellation, recovery, and idempotency;
- authentication, ACLs, approval governance, tool policy, data classification, and auditability;
- schema migration/replay behavior and cross-store reconciliation;
- RAG and Agent behavior evaluation gates;
- API, DingTalk, SSE, console, Playwright, and canary behavior;
- deployment artifacts, supply chain, DataWorks, monitoring, SLOs, capacity, and cost controls;
- documentation-to-code consistency and release-operability unknowns.

No production tier was named. The review therefore stayed in local/simulate/read-only modes and did
not run production RDS/HA3, DingTalk, DataWorks, SAE deployment, paid model evaluation, or any
production write.

## Verification matrix

| Gate | `main` | Agent v2 |
|---|---:|---:|
| Python tests | 2,868 passed, 35 skipped | 4,318 passed, 1 skipped |
| Ruff | Tracked core passed; full `make lint` failed only on pre-existing untracked `.agents` scripts | Passed |
| Console unit tests | 326 passed | 420 passed |
| Console production build | Passed | Passed |
| Offline general-ability gate | 258 gold questions, 0 hijacked; 84/84 routing cases | 251 gold questions, 0 hijacked; 84/84 routing cases |
| Baseline freshness | Passed | **Failed**: gold-set SHA, LLM version, missing Agent regime |
| Current branch CI | Not re-queried as part of final synthesis | Overall green, but freshness failure masked by `continue-on-error` |
| Live RAG release gate | Not run | Not run |
| Live Agent evaluation | N/A | Not run; current frozen result is stale |
| Staging stress / multi-replica chaos | Not run | No current-head GitHub stress run; stored reports predate head |

## Release blockers

### B1. The integrated release candidate does not exist

Production evidence from either branch cannot be transferred to the eventual merge result. The
projected merge has conflicts in security, readiness, migration, frontend, and test control files.
Conflict resolution can silently choose away either side's hardening.

Agent v2 is also missing current-main fixes:

1. **Irreversible HA3 reconciliation fencing.** Agent v2's
   `opensearch_pipeline/spot_checker.py::reconcile_pending_deletes` scans candidates, deletes HA3
   chunks, then performs RDS updates without a document lock, state revalidation, or checked CAS.
   A concurrent restore/visibility change can be followed by deletion of the restored document's
   search chunks. Current main adds `document_meta ... FOR UPDATE`, lock-time revalidation, a bounded
   search client, consistent lock order, and CAS/rollback handling (`9e87131`).
2. **Identity tombstones.** Agent v2 filters `user_role` with `is_active=1`, making an inactive row
   indistinguishable from absence; cache-miss/API refresh can resurrect department access. Main
   reads `is_active` and treats `0` as authoritative empty access (`79b924c`).
3. **Serving readiness and QA drift alerts.** Main probes operation-DB table/column contracts and
   DingTalk Stream connectivity and pages on QA schema drift (`c7b8723`). Agent v2's richer Agent
   readiness lacks these current-main checks.
4. **Migration statement replay guards.** Main added information-schema skip guards for supported
   `ADD COLUMN` / `CREATE INDEX` shapes (`a7219a9`); Agent v2 lacks them.

**Required disposition:** integrate current main first; independently review all 23 conflict
resolutions; rerun every test, security, build, eval, migration, and failure-injection gate on the
exact resolved merge SHA.

### B2. A terminal run can continue executing production side effects

Agent v2's shutdown drain can mark a live run `failed` after its timeout without cancelling or
terminating its thread (`agent_runtime/executor.py::drain`). The stale-run reaper likewise performs
`running -> failed` based on heartbeat age (`agent_runtime/run_store.py::reap_stale_runs`).

The driver checks only the in-memory cancellation flag before adjudicating the next tool; it does
not revalidate durable run ownership/status. `record_tool_call` updates counters and inserts steps
without a `status='running'` or execution-epoch predicate. The final completion CAS fences only the
answer and cannot undo an already committed tool mutation.

Failure scenario:

1. a model or tool call blocks;
2. drain or the stale reaper marks the run failed;
3. the blocked call returns before process death;
4. the same thread advances to another tool and commits a HIGH_WRITE mutation under a terminal run;
5. final answer persistence loses its CAS, but the mutation remains.

Existing drain coverage only verifies final-completion fencing; it never attempts a later write
after ownership loss.

**Required fix:** durable execution lease/epoch fencing. Every side-effect boundary must validate
`(run_id, active status, expected execution_epoch)` and couple that validation atomically to the
mutation where possible. External writes require provider idempotency, outbox, or saga semantics.
Drain should signal cooperative cancellation as well as changing durable state.

### B3. Durable approval redrive drops the approval-time stewardship scope

Inline approval carries `approver_scope` into execution, allowing the HIGH_WRITE ontology tool to
compare the approval-time scope with current stewardship immediately before mutation. The durable
resume command/redrive path persists/passes the request ID and approver but omits that scope.
`ontology_identity_resolve` only performs the comparison when `ctx.approval_scope` is not `None`.

Failure scenario: steward A approves; the process dies after committing the decision but before
inline resume; stewardship rotates to B; redrive executes A's approval with a missing scope, which
disables the TOCTOU check.

**Required fix:** persist the approval-time scope atomically with the decision/resume outbox and
compare that persisted snapshot against a fresh live scope before any side effect. Recomputing scope
only during redrive is insufficient because it would compare "current" with itself.

### B4. Approved-write requester revalidation is not actually fail-closed

`_requester_ctx(..., fail_closed_on_error=True)` rejects exceptions, but the functions it calls
swallow the relevant failures:

- department resolution returns `([], False)`;
- knowledge identity resolution returns an ordinary employee;
- tombstone lookup returns `False` on read failure.

Empty ACL still permits public ontology objects. A requester disabled while approval is pending can
therefore resume an approved public-object mutation during a transient identity-store failure.
Tests mock the helpers to throw, so they do not exercise their real fallback returns.

**Required fix:** propagate explicit authoritative/unknown/revoked tri-state results. Approved-write
resume must reject `unknown`, not reinterpret it as empty ACL + employee + active.

### B5. Confidential tool results defeat the model-egress ceiling

`ontology_resolve` has a static ToolSpec classification of `internal`, while its ACL legitimately
allows a same-department caller to receive a `confidential` object. Tool execution stamps the static
spec classification on the run context. The next model call therefore sees only `internal`, so even
`RAG_AGENT_EGRESS_MAX_CLASS=internal` permits confidential content to leave for DashScope.

**Required fix:** compute classification from actual returned rows/artifacts, propagate the maximum
observed classification into the context, and test the real tool -> executor -> next-model-call path.

### B6. Current RAG and Agent behavior has no valid release scorecard

Agent v2's `scripts/check_baseline_freshness.py` exits nonzero because:

- current `golden_full.json` SHA is `b3403db07db7b71a`, but the frozen RAG baseline expects
  `3bed9881eefaf0ee`;
- the frozen RAG LLM is `qwen3.6-plus`, while current defaults resolve to `qwen3.7-plus`;
- the Agent baseline is old-format and has no cases/model/tier/prompt/provider regime fingerprint.

CI deliberately masks this through `continue-on-error: true`. In addition:

- `make release-gate` runs RAG L0-L6 only; Agent evaluation is a separate manual target;
- Agent gating supports only the light tier;
- ontology trigger/argument metrics are displayed but are not gated;
- the default 31-case set excludes five on-arm ontology cases;
- no model-behavior ACL case family proves permission correctness;
- the frozen live Agent report dates from July 12 and cannot be compared to current code/model/prompt.

**Required disposition:** run current live RAG and Agent evaluations on the final merge artifact,
human-adjudicate, refreeze with regime fingerprints, include every enabled tool/tier and ACL/adversarial
cases, and make both freshness and Agent regression gates blocking.

### B7. Agent v2's generic migration runner is unsafe to replay

`_ledger_conflict()` returns the same `None` for both "not applied" and "matching checksum already
applied." The commit path then executes every DDL statement again. Several Agent migrations contain
naked `ALTER TABLE ... ADD COLUMN` or multi-action ALTER statements.

`schema/049_acl_outbox_generation.sql` explicitly claims that `apply_migration.py` will skip its
naked `ADD COLUMN`, but Agent v2 contains no such guard. Replay can fail with MySQL 1060. Because
MySQL DDL uses implicit commits, a multi-statement migration can become partially applied even when
the ledger write never occurs.

CI builds schema directly with `ci_load_schema.sh` and therefore does not test this production
migration path. Existing tests do not apply the same migration twice through the generic runner.

**Required fix:** absorb/mainline the supported information-schema statement guards, distinguish
"already applied" from "not applied," add real MySQL replay-twice tests, and define explicit behavior
for unsupported multi-action ALTERs.

### B8. Canary success does not prove the released Agent works

`deploy/sae_canary_deploy.py::verify_health` checks only `/api/version` and `/api/ready`. A release
can pass when:

- Agent/ontology flags are off (`skipped` readiness checks);
- Redis is down (noncritical by default although asks fail closed with 503);
- DashScope is unreachable;
- no worker owns durable recovery;
- runtime construction, SSE, tools, answer persistence, and resume recovery are broken.

The script explicitly says it does not update or verify environment flags. It deploys an arbitrary
image tag rather than enforcing a tested digest/attestation relationship.

**Required fix:** add expected-profile flag attestation and an authenticated synthetic transaction
that proves submit -> model/tool -> SSE -> terminal state -> persisted run/QA row. Seed and recover a
durable command as a worker check. Tie the evidence to the exact immutable artifact digest.

### B9. Durable recovery has no guaranteed cold-start owner

Reaper and dispatcher loops start lazily when an Agent endpoint constructs the runtime. FastAPI
startup does not initialize them, Docker launches only Uvicorn, and the standalone `agent_worker`
is described as a user-gated second deployment with no checked-in deployment manifest.

After a restart, queued commands or orphan runs can remain untouched until the next Agent request.
This is incompatible with a durable-dispatch promise unless an independently deployed worker is
proven live.

**Required fix:** represent the worker in deployment-as-code, fail deployment if it is absent for a
durable profile, and test a pre-seeded backlog on cold boot with no incoming HTTP request.

## Major concerns

### M1. Main is default-open unless deployment posture is independently proven

`main` keeps `RAG_REQUIRE_AUTH` default-off and intentionally allows anonymous callers through to
company-wide `public` knowledge. The code itself states that `public` means employees, not the
internet. Main's production configuration does not force either `RAG_REQUIRE_AUTH` or
`RAG_ACL_FAIL_CLOSED`; tests lock the default-open behavior.

Agent v2 materially improves this by failing production/staging startup when authentication and
ACL fail-closed are missing, unless a date-bound legacy-open acknowledgement is explicitly supplied.
Before any main deployment, prove both flags live or port the v2 posture assertion.

### M2. DingTalk's primary RAG path bypasses normal admission and starts unbounded threads

Both trees import the shared limiter, but the DingTalk primary RAG path does not call `admit_ask`.
Only the general-ability fallback calls `admit_general`. Every accepted message starts a new daemon
thread for retrieval and generation without a semaphore or bounded executor.

An authenticated/internal user or burst can therefore bypass the normal per-user/global cost gate,
exhaust threads/DB connections/model capacity, and create spend outside the API admission metrics.

### M3. Commit-ACK ambiguity is misclassified on HIGH_WRITE tools

The ontology store can commit its mutation and operation ledger, then lose the DB acknowledgement.
The tool catches the resulting exception and returns a definite failure; ToolExecutor records
`failed`, while automatic reconciliation only scans `uncertain` invocations. Operators and the
model can be told the mutation failed even though it succeeded.

The same ambiguity can double Agent steps/budgets: combined bookkeeping commits rethrow on an ACK
loss, after which the executor performs fallback writes. Resume then seeds from inflated counters.

### M4. Idempotency is run-scoped, not business-request scoped

Agent ask requests have no client idempotency key; every POST generates a new message/run ID. Tool
keys incorporate run/turn/provider call identity. If the first mutation commits but the HTTP response
is lost, a retry creates another run and a distinct operation key. The current ontology tool has a
natural uniqueness backstop, but the platform contract does not protect future ERP/external writes.

### M5. Stewardship rotation can deadlock pending approvals

Approval visibility is filtered by the stored proposal-time scope before live authorization. After
scope A -> B, old steward A may still list but cannot approve; new steward B cannot list or pass the
pre-visibility gate. Only `kb_admin` can rescue the request. Queue visibility and decision
authorization must use the same live resolver.

### M6. Approval backlog has no admission budget or usable pagination

Authenticated users can generate distinct-thread proposals up to the normal ask allowance. Pending
approvals live for days, while the queue is capped at 200, oldest-first, without cursor pagination.
One or a few users can bury newer legitimate approvals and consume the shared model budget.

Add per-requester/per-tool pending quotas, a global backlog breaker, pagination, aging metrics, and
abuse tests.

### M7. Audit evidence is neither privilege-separated nor tamper-evident

The Agent audit table is ordinary InnoDB written with the application's operational connection. It
has no hash chain, signed sequence, write-only DB identity, or independently immutable sink.
Retention may delete it, and the optional archive is an ordinary gzip OSS object without a signed
manifest or Object Lock evidence.

A compromised application/RDS credential can alter or erase approvals, kill-switch events, and
HIGH_WRITE evidence without detection.

### M8. RDS TLS remains warning-only

Both trees explicitly disable TLS when `RAG_RDS_SSL_CA` is absent. Production logs a warning but
continues. These connections carry identity, ACL, approval, audit, and conversation data.

Production readiness requires a configured CA and an observed verified TLS cipher in the deployed
instance, preferably enforced by startup rather than operator memory.

### M9. Agent latency, availability, and queue health are absent from authoritative SLOs

Agent QA rows record `latency_ms=0`; the rollup excludes nonpositive latency, so arbitrarily slow
Agent executions do not degrade p95. The generic dashboard excludes Agent rows and no equivalent
Agent-specific availability path exists.

Monitoring lacks explicit SLOs/probes for dispatch backlog, approval age, stale runs, uncertain
invocations, relay lag, worker heartbeat, cancellation latency, and recovery convergence.

### M10. Standing production monitoring is not proven

Repository docs and code state that DataWorks scheduling was not set up, the Mac LaunchAgent is
unloaded, and the checked-in DataWorks node is paused/manual-paste. That may be stale external state,
but the repository contains no current attestation that a dependable monitor and alert path exist.

The checked-in node runs only read-only reconciliation/queue/funnel work; the full QA SLO rollup is
commented out. `RAG_OPS_ALERT_WEBHOOK` can also be absent, turning alerts into logged no-ops.

### M11. Capacity evidence is stale, mocked, and nonblocking

Stored full stress evidence predates the reviewed branch head by many commits and uses mocked
DashScope/embedding/HA3 paths. G1-G9 capacity thresholds remain draft/nonblocking, RSS was not
measured, and no current-head GitHub stress workflow exists because the branch has no PR. Real
staging load, multi-replica behavior, and SSE idle-timeout compatibility remain unproven.

### M12. No single reproducible and tested shipping artifact is authoritative

Agent v2's Docker path is substantially stronger than main: digest-pinned base images, hashed
production lock, non-root runtime, and frontend multi-stage build. But README also describes a
manually assembled SAE code ZIP using floating dependencies, while the canary deploys an arbitrary
image/tag. CI does not build/smoke the final container or bind its digest to test/eval attestations.

### M13. DataWorks integrity and dependency controls are weaker than documented

All stage-node ZIP checks allow execution when the SHA sidecar is missing or unreadable. The sidecar
and ZIP also share the same upload trust boundary and are unsigned. Modern-Python branches install
unbounded packages from the network during every run; Python 3.7 pins live outside the production
lock and CVE audit.

### M14. Critical Agent UX E2E tests do not run in CI

Playwright scenarios exist for Agent flag visibility, SSE/approval cards, persistence, and run
center behavior. Frontend CI runs only Vitest/build; Vitest explicitly excludes `tests/**`, and the
advertised UX hard-gate suite is itself skipped.

### M15. Cost controls are proxies, not financial controls

Agent LLM ledger rows record `cost_estimate=None`. Default run budgets allow 12 turns and 200,000
tokens. Global controls count admissions/calls rather than tokens or currency, and no tested model
price table, currency budget, or cost alert exists.

### M16. Runtime/operations documentation materially contradicts current code

Examples:

- `docs/agent-platform-v2/execution-model.md` states there is no independent worker layer and still
  lists loop/executor/relay/drain as pending, although those components exist;
- `implementation-plan.md` is frozen against an early commit and promises a deploy workflow and
  configurable workers that do not exist as described;
- Agent v2 still reports a 251-question baseline while current main has 258;
- main README claims Redis four-state backends, durable dispatch, and an expected-replica topology
  guard that main does not contain.

This is an operational correctness issue: deployment staff can choose the wrong topology or believe
a safety gate exists when it does not.

## Minor issues

1. Approval rejection feedback is unbounded, stored raw for a long retention period, and fed into a
   subsequent model turn without the standard PII/secret sanitizer.
2. `knowledge_search` returns `str(e)` to the model despite a comment claiming internal exceptions
   are hidden; backend host/schema/index/driver details may enter model context.
3. Redis relay recovery can emit `[DONE]` without a semantic terminal frame after publication loss;
   the console may repair through detail polling, but the SSE API contract remains partial.
4. Cancellation during a tool can still enter another paid model call because no cancellation check
   occurs between `gen.send(result)` and the generator's synchronous next model call.
5. Each real tool call can create duplicate `agent_step(kind='tool_call')` records through both
   executor bookkeeping and adjudicator tracing.
6. Agent execution is currently console-only. DingTalk and miniapp do not invoke the Agent runtime;
   release messaging must not imply otherwise.

## Strong foundations worth preserving

- Agent v2's production/staging posture guard requires authentication and ACL fail-closed unless a
  date-bound legacy acknowledgement is explicitly renewed.
- HIGH_WRITE tools have a separate default-off flag layered on top of read-only ontology tools.
- Production Agent startup requires prompt-injection protection, a dedicated checkpoint HMAC key,
  mandatory HMAC verification, and checkpoint encryption.
- Run budgets, per-instance execution capacity, event queues, and many network operations are bounded.
- Final answer persistence and `running -> succeeded` are transactionally coupled with
  commit-ambiguity handling.
- Approval decision and resume command are transactionally coupled.
- The current ontology write and its operation ledger share a transaction.
- Dispatch command -> run binding has a DB uniqueness fence.
- Agent v2 CI spans Python 3.10/3.11, real MySQL contracts with zero-skip enforcement, secret/CVE
  scans, production-lock audit, SBOM, Trivy, Ruff, tests, and frontend build.
- No generic shell/command tool or user-controlled URL-fetch tool is registered in the reviewed
  Agent tool surface; schemas generally reject extra identity parameters.

These strengths reduce the remaining work. They do not compensate for the blocker-class ownership,
approval, egress, release-gate, and integration failures.

## Unknown-unknown probes required before release

1. **Side-effect ownership matrix:** kill/fence before and after model response, invocation insert,
   tool start, mutation commit, operation-ledger commit, final-answer commit, drain timeout, and stale
   reaping. Assert no side effect after durable ownership loss.
2. **Approval crash/rotation matrix:** crash after decision/before inline resume, rotate stewardship,
   revoke requester, inject identity DB/API failures, and redrive. Assert zero unauthorized mutation.
3. **Cross-run retry:** commit a write, drop the response, submit the same business request again, and
   assert exactly one mutation across distinct runs.
4. **Commit-ACK loss:** inject acknowledgement loss into tool mutation/ledger and Agent bookkeeping;
   prove `uncertain` classification and deterministic reconciliation without doubled budgets/steps.
5. **Two-replica topology:** real MySQL + Redis under rolling deploy, packet loss, DB failover, Redis
   failover, lease expiry, clock skew, and asymmetric process death.
6. **Cold-start backlog:** start with queued submit/resume commands and stale runs, send no Agent HTTP
   request, and prove bounded worker recovery/reaping.
7. **Redis event continuity:** stream trimming, TTL expiry, publication loss, terminal-frame loss, and
   reconnect with `Last-Event-ID`; reconstruct the exact final answer with one semantic terminal frame.
8. **Live effect evaluation:** current RAG + Agent gold sets on the final artifact, including ACL,
   ontology, prompt-injection, tool restraint, approval, grounded answer, and every enabled model tier.
9. **Authenticated release canary:** desired flags, runtime/worker health, one read-only tool call,
   SSE completion, run row, QA row, and restart recovery bound to the image digest.
10. **Staging capacity/cost:** real DashScope/HA3/RDS latency, token/currency cost, quotas, long silent
    spans through the SAE/SLB idle timeout, memory/thread/connection growth, and global-cap behavior.
11. **Live deployment attestation:** actual SAE replica count, Redis backends, event relay, durable
    dispatch, independent worker, schema ledger 022-053, RDS TLS cipher, DingTalk Stream, alert
    webhook, branch protection, and standing monitor schedule.
12. **DataWorks supply chain:** mandatory signed artifact identity, offline or locked dependencies,
    clean-build import smoke, sidecar absence fail-closed, and rollback on package mismatch.

## Production-readiness sequence

1. **Integrate current main into Agent v2.** Resolve all conflicts deliberately; do not choose either
   branch wholesale for hot files. Preserve main's reconciliation, tombstone, readiness, QA alert,
   and migration fixes.
2. **Close blocker invariants.** Implement execution epoch fencing, approval-scope persistence,
   tri-state requester identity, result-level classification, migration replay safety, and a guaranteed
   recovery worker.
3. **Add missing failure-injection tests.** At minimum: zombie HIGH_WRITE after drain/reaper,
   decision/resume crash with stewardship rotation, identity failure during resume, confidential
   result under an internal egress ceiling, migration replay twice, and cross-run duplicate requests.
4. **Make effect gates real.** Run and refreeze live RAG and Agent results on the final merge SHA;
   extend cases; make freshness and Agent regression blocking.
5. **Create one release artifact.** Build and smoke an immutable container digest, generate an SBOM,
   and attach test/eval/security attestations to that digest. Remove the ambiguous manual ZIP path or
   make it equally reproducible and tested.
6. **Prove topology and recovery.** Deploy web + independent worker as code; exercise two replicas,
   Redis/MySQL failures, rolling deployment, and a cold-start backlog.
7. **Activate monitoring before traffic.** Agent SLOs, worker heartbeat, queue age, uncertain writes,
   relay lag, p95 latency, cost, DingTalk Stream, RDS TLS, and alert-delivery probes must be live.
8. **Ship in risk order.** Begin with authenticated, ACL-fail-closed, read-only Agent canary and
   `RAG_ONTOLOGY_WRITE_TOOLS_ENABLE=false`. Enable HIGH_WRITE only after staging chaos and governance
   sign-off prove the ownership and approval invariants.

## Review disposition

- **Current main:** retain as the production RAG baseline; do not infer readiness from README claims.
  Verify authentication, ACL fail-closed, RDS TLS, DingTalk capacity controls, and monitoring before a
  fresh deployment.
- **Current Agent v2 tip:** not releasable and not suitable for merge-to-release.
- **Future resolved merge candidate:** requires a new full review because conflict resolution changes
  the security and runtime artifact.
- **Repository changes made by this review:** this report only; no product code, configuration,
  migration, or deployment state was changed.

