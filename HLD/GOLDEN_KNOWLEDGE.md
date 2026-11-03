# Golden Knowledge: implemented prototype and production path

Requirement: the agent must not rely on BigQuery alone. It must reuse how human
analysts interpreted earlier questions, the bucket must be updatable over time,
and relevant knowledge must be supplied at query time. Stakeholder note (Harry):
the bucket is theoretical, so the format and a small seed set are ours to define,
and AI-generated samples are fine if they are understood and reviewed.

## What a trio is

A trio (Question -> SQL -> Analyst report) is a reviewed analytical METHOD, not
a cache of true answers. One JSON file per trio in `golden/trios/` (the bucket),
validated by `retail_agent/golden/models.py`:

| Field | Purpose |
| --- | --- |
| `id`, `version`, `status` | Stable slug, monotonic version, `approved` or `withdrawn` |
| `question`, `paraphrases` | How executives ask it |
| `interpretation` | The analyst's reading: definitions, period rules, pitfalls, privacy constraints |
| `metric_definitions`, `grain` | Exact formulas and the level of aggregation |
| `sql` | Reviewed BigQuery SQL; relative dates (`CURRENT_DATE()`), no parameters, no brand filter |
| `analyst_report` | Report structure with `<placeholders>`; concrete money/percent values are rejected |
| `tags`, `provenance`, `reviewer`, `approved_at`, `supersedes` | Retrieval hints and audit trail |

Placeholders matter: a historical number in an example is the easiest thing for
a model to repeat as if it were current. The validator refuses `$1,200` or `12%`.

The seed set has 12 trios covering the assignment's examples: monthly trend,
month-over-month, top customers by ID, product/category comparison with a
"why" decomposition, brand gross profit, state comparison ("why does state X
underspend"), age bands, churn proxy, quarterly report with next-quarter actions,
returns, acquisition channels and a dataset overview. They are labelled
`synthetic-reviewed`: generated with AI assistance, then reviewed for metric
correctness and policy compliance.

## Ingestion: how the bucket is updated over time

`uv run python -m retail_agent.golden.ingest` (`--check-only` validates files without
network calls, `--bigquery-dry-run` also dry-runs every SQL, `--bucket` points to
another folder):

1. **Validate the whole bucket first.** Schema, file name equals id, unique ids,
   no contact-detail patterns, and every SQL passes the SAME policy as model SQL:
   SQLGlot column/lineage checks, aggregate-only demographic shapes and brand-scope
   rewriting. One bad trio fails the run and nothing is published.
2. **Optional `--bigquery-dry-run`:** each scoped SQL is compiled by BigQuery
   (free). All 12 seed trios pass.
3. **Plan by content hash.** Only new or edited trios (or a changed embedding
   model) are re-embedded; withdrawn or deleted trios are removed.
4. **Embed before writing**, then publish upserts and removals in ONE transaction,
   so readers never see a half-updated index and a provider outage publishes nothing.
5. **Separate roles.** Ingestion writes with the `golden_ingest` role; the chat
   agent's `retail_agent` role can only SELECT the index (tested).

Migration `docker/reports-postgres/004_golden_knowledge.sql` creates the index:
JSONB document, `tsvector` for lexical match, `vector(768)` embedding
(`gemini-embedding-2`, the same model as reports/memory).

### The update loop, demonstrated

The first live run of "Why did our churn rate spike last month?" followed
`customer-churn-proxy` v1 and found month-to-month lapse rates of 99-100%: in this
data, repeat purchases in consecutive months are rare, so the v1 method carries
no signal. That is exactly the feedback the loop exists for. The trio was revised
to v2: it keeps the definition but tells the agent to say the signal is weak and
pivot to active buyers and returning-buyer share. Re-ingestion re-embedded only
that trio. The answer evaluation later found a second case: the quarterly report
added category rows up in prose and got the total wrong, so
`quarterly-business-review` v2 asks for a separate totals query (EVALUATION.md).
In production the same path starts from flagged conversations and evaluation
failures, not from the developer noticing (see "System learning" below).

## Query-time retrieval

`retail_agent/golden/service.py`, called once per question before planning:

- **Query text:** the current question; a short follow-up (six words or fewer) is
  prefixed with the previous question so "and by month?" keeps its intent.
- **Hybrid scoring:** pgvector cosine similarity selects 10 candidates; a
  lexical `ts_rank` OR-match adds at most 0.05 to break near-ties. Semantic
  similarity decides relevance, so a paraphrase still matches its trio.
- **Threshold and top-k:** candidates below `GOLDEN_MIN_SIMILARITY` (default 0.65)
  are dropped; at most 3 are kept within a 9,000-character prompt budget.
- **Prompt contract:** the planner receives `golden_examples` with id, version,
  similarity, interpretation, definitions, grain, reviewed SQL and the report
  structure, but not reviewer metadata. Mandatory rules: precedents are not current
  data, never quote their numbers, re-derive dates, adapt SQL (which is validated
  as usual and grants no access), ignore irrelevant ones, and end with
  `Method: <trio_id> v<version>` when one was followed.
- **Graceful degradation:** any retrieval failure returns `unavailable`, logs
  `golden_retrieval_failed` with the exception class only, and planning continues
  on the schema and mandatory metric rules. `GOLDEN_RETRIEVAL_ENABLED=false`
  turns retrieval off (useful for A/B evaluation).
- **Observability:** `golden_retrieved` logs `turn_id`, selected `golden_ids`,
  candidate count, top similarity and latency.

## Evaluation and calibration

`uv run python -m retail_agent.golden.evaluate` embeds 30 cases from
`golden/eval/retrieval_cases.json`: 24 unseen phrasings
with the trio an analyst would expect, and 6 negatives (greeting, memory request,
report deletion, weather, poem, report listing). Live results on the final trios
(3 November 2026; full output in `evals/results/`):

| Threshold | hit@1 | recall@3 | Negative false-positive rate |
| --- | --- | --- | --- |
| 0.60 | 1.00 | 1.00 | 0.67 |
| 0.65 | 0.96 | 0.96 | 0.33 |
| 0.66 | 0.96 | 0.96 | 0.00 |
| 0.70 | 0.88 | 0.88 | 0.00 |

Positives scored 0.63-0.83 at the top, negatives 0.53-0.66, so a single threshold
cannot separate them perfectly. 0.65 was chosen because a missed precedent hurts
answer quality more than an extra precedent on chit-chat, which the planner is
told to ignore. On this set 0.66 would remove the two negative matches (0.654 and
0.655) at no recall cost, but a margin of one hundredth on 30 cases is too thin to
retune on; a larger set decides that. Thirty cases is an initial calibration, not
proof of generalization; the threshold was chosen on the same set it is reported on.
The evaluation ranks by vector similarity alone; at runtime a lexical match adds at
most 0.05 to break near-ties, which can reorder close candidates but does not
change which ones pass the threshold.

Live smoke (Gemini + BigQuery, read-only, before `customer-churn-proxy` moved to
v2): both assignment questions retrieved the expected trio first (similarity 0.85
and 0.79), and the answers cited `Method: customer-churn-proxy v1` and
`Method: state-spend-comparison v1`.

## Production design (not deployed)

- **Bucket:** Cloud Storage with object versioning and retention; trios are
  immutable per version. Git review (pull request + CODEOWNERS analysts) is the
  approval gate; the merge triggers a Cloud Build/Cloud Run Job that runs the same
  validation, BigQuery dry run, retrieval evaluation and an answer-level
  regression set, then publishes the index atomically.
- **Index:** Cloud SQL PostgreSQL + pgvector, as in the prototype. Add tenant and
  visibility columns and filter on them BEFORE ranking, so a tenant-specific
  precedent can never reach another tenant. Rebuild into a new index version on an
  embedding-model change instead of mixing vector spaces.
- **Retrieval:** add a small reranker (cross-encoder or an LLM relevance check) on
  the top 10 to separate the 0.63-0.67 overlap zone, and grow the evaluation set
  from real traffic.
- **System learning:** capture consented, sanitized candidates from thumbs-down,
  repeated SQL repairs, empty results and analyst escalations; cluster them; an
  analyst writes or corrects a trio (`provenance: promoted-from-feedback`); release
  only after offline evaluation and a canary. No autonomous writes from the agent
  into the bucket: its role stays read-only.
- **Metrics:** retrieval hit rate and similarity distribution per trio, "Method"
  citation rate, answer quality with and without retrieval, and stale trios
  (low use, failed dry run after a schema change).
