export const meta = {
  name: 'rag-answer-judge-panel',
  description: 'Independent Claude judge panel scoring RAG answers (faithfulness/correctness/etc.)',
  phases: [{ title: 'Judge', detail: 'N independent Claude judges score answer-bundle shards (full context)' }],
}

// args (object or JSON string): { shard_dir, n_shards, n_judges?:3 }
const A = (typeof args === 'string') ? JSON.parse(args) : (args || {})
const SHARD_DIR = A.shard_dir || '/Users/laijunchen/Downloads/opensearch-rag-pipeline/eval_harness/reports/shards'
const N_SHARDS = A.n_shards || 0
const N_JUDGES = A.n_judges || 3
const pad = (n) => String(n).padStart(3, '0')

const VERDICT_ITEM = {
  type: 'object', additionalProperties: false,
  required: ['qid','faithfulness','correctness','completeness','relevance','fabricated','appropriate_refusal','overall','verdict','rationale'],
  properties: {
    qid: { type: 'string' },
    faithfulness: { type: 'integer', minimum: 1, maximum: 5 },
    correctness: { type: 'integer', minimum: 1, maximum: 5 },
    completeness: { type: 'integer', minimum: 1, maximum: 5 },
    relevance: { type: 'integer', minimum: 1, maximum: 5 },
    fabricated: { type: 'boolean' },
    appropriate_refusal: { type: 'boolean' },
    overall: { type: 'integer', minimum: 1, maximum: 5 },
    verdict: { type: 'string', enum: ['pass','partial','fail'] },
    rationale: { type: 'string' },
  },
}
const BATCH_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['verdicts'],
  properties: { verdicts: { type: 'array', items: VERDICT_ITEM } },
}

const RUBRIC = `You are an impartial QA evaluator for an enterprise Chinese knowledge-base assistant (a manufacturing company). Judge ONLY against each item's provided context and gold points — do NOT use outside knowledge. Each bundle item has: qid, kind (positive=answerable from corpus; negative=should NOT be answerable), query, gold_answer_points (reference; may be empty for negatives), gold_keywords, context_text (the EXACT context the assistant was given — judge faithfulness against THIS), and answer.

Score each dimension as an integer 1-5:
- faithfulness: is every claim in the ANSWER supported by context_text? 5=fully grounded, 1=fabricated/contradicts. Only count a claim unsupported if it is genuinely absent from context_text.
- correctness: does the ANSWER agree with the GOLD points/facts (or context for negatives)? 5=correct, 1=wrong. For a negative, a correct refusal/"not available" = 5; a confident wrong/invented answer = 1.
- completeness: does it cover the key points asked for? 5=complete, 1=misses everything. For negatives set 3.
- relevance: does it actually address the question (not evasive/off-topic)? 5=on-point.
Also set: fabricated (bool: states material facts NOT supported by context_text); appropriate_refusal (bool — for negatives: did it correctly decline / say info unavailable instead of inventing? for positives: set true); overall (1-5 holistic given the kind); verdict (pass|partial|fail); rationale (one concise sentence).
Be strict, consistent, and calibrated. Return ONE verdict object per item, echoing its exact qid.`

phase('Judge')
log('args=' + JSON.stringify(A) + ' shards=' + N_SHARDS + ' dir=' + SHARD_DIR)

const shardIdx = Array.from({ length: N_SHARDS }, (_, i) => i)
const judges = []
for (let j = 1; j <= N_JUDGES; j++) {
  const parts = await parallel(shardIdx.map((i) => () =>
    agent(
      `${RUBRIC}\n\nYou are judge #${j} of an independent ${N_JUDGES}-judge panel (judge independently).\n` +
      `Use the Read tool to open the JSON file at:\n${SHARD_DIR}/shard_${pad(i)}.json\n` +
      `It is a JSON array of items. Judge EVERY item in it and return one verdict per item, echoing its qid.`,
      { label: `judge${j}:shard${pad(i)}`, phase: 'Judge', schema: BATCH_SCHEMA }
    ).then((r) => (r && r.verdicts) || [])
  ))
  judges.push({ judge: `claude-judge-${j}`, verdicts: parts.flat() })
}

return { panels: judges }
