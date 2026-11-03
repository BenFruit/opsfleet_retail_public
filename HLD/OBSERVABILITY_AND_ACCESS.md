# Observability and access: prototype decisions and production follow-up

## Implemented: optional local Langfuse tracing

Langfuse is selected instead of LangSmith because a local self-hosted dashboard
is wanted without a LangSmith Enterprise license. Its LangChain callback emits
one root trace per question (`retail_question`) with `plan`, `query`, `reports`
and `remember` child
observations. Explicit generation observations capture Gemini prompt/system
instruction, validated actions and token usage under the corresponding planning
step. Root metadata contains `turn_id` for correlation with `logs/agent.jsonl`.
`observability/tracing.py` owns opt-in initialization, callbacks and shutdown.
No LangSmith tracing is used: `langsmith` is a declared dependency only so the
agent can switch its tracing context off, which keeps implicit export disabled
even if stale `LANGSMITH_TRACING` variables remain in the environment.

Enable explicitly with `LANGFUSE_TRACING_ENABLED=true`, local project public and
secret keys, and `LANGFUSE_BASE_URL=http://localhost:3000`. There is no fallback
to a cloud endpoint when configuration is missing. Tracing is off by default.
Local allowlisted JSON logs are the always-on layer. Correlation keys: every
`query_*` event has `turn_id` + `query_id`; `agent_observation` links the turn to
its `query_id`; `model_retry` and `agent_model_completed` record retries and the
answering `model`; `startup_failed` records the failing startup stage.
There is no separate span for every SQL validation/dry-run/execution stage: those
currently live inside the `query` node. Controlled query failures are observation
data, so a successful node/run can still contain an error observation. Inspect
the observation code and counters, not just the dashboard's success indicator.

## Langfuse is a separate platform service

Langfuse is not a component of this application. It is a standalone platform
(web and worker, PostgreSQL, ClickHouse, Redis, object storage) that normally
lives outside any single project and manages tracing for many projects and
teams, each with its own organization, project and API keys. The root Compose
file runs a private copy only to develop and demo locally. In production, using
it depends on the organization: a shared self-hosted instance run by a platform
team, managed Langfuse Cloud, or no LLM tracing platform (Cloud Logging and Cloud
Trace only). The options and trade-offs are in PRODUCTION_HLD.md §5.7. Both Langfuse
options are configuration only; Cloud Trace without Langfuse needs an
OpenTelemetry adapter in place of `observability/tracing.py`.

## Production requirement: PII-safe trace export

**Full trace payload sanitization is NOT implemented.** Enabled tracing can export
question/history, saved operator memory, schema context, authorized brand names, SQL, tool observations,
model output and exception details. Graph state includes proposed actions before
downstream safety checks, and generation tracing precedes final text checks.
Existing SQL policies and pre-LLM result guards remain in place, but they are not
a guarantee that every exported trace field is free of sensitive content.

For now, enable tracing only for demo/non-sensitive inputs. Do not use it with
real personal or confidential business data. Do not commit API keys or traces.
Choosing a different endpoint alone does not solve payload privacy.

Before production:
- Define an allowlist of trace fields; remove/redact sensitive input, output,
  history, SQL literals, identifiers and exception contents before export.
- Apply the policy to BOTH graph callbacks and Gemini generation spans, including
  rejected requests/results and failure paths; test with sensitive canaries.
- Define workspace access, retention/deletion, data region and sampling policies.
- Verify that telemetry failures cannot break analysis or expose secrets through
  fallback logs; evaluate operational overhead and timeouts.
- Add trace-linked evaluation datasets and metrics for query success, repair,
  safety rejection, latency, tokens, scans, budget stops and answer quality.
- Use the Langfuse SDK `mask` hook (available in the installed SDK) as the single
  redaction point for every exported event; drop raw result rows, keep shapes.
- Emit separate spans for SQL validation, dry run and execution, and attach
  domain signals as tags/scores (partial answer, SQL recovery, ungrounded
  figures, Golden trio used) so traces can be filtered by them.

This deferred work is a known privacy gap, not a completed safety capability.

## Unchanged: prototype authorization and prompt brand list

By decision, do not redesign authorization in this iteration. Trusted startup
manager-to-brand mapping, per-source query scoping and parameter binding remain
unchanged. The full authorized-brand list is still sent as model context; this
list is informative, NOT the enforcement boundary.

The approach is acceptable for the small demo mapping but repeats tokens and
does not scale gracefully to hundreds/thousands of brands. Prompt contents must
never be trusted as proof of identity or access.

Production proposal only:
- Obtain identity from authentication and use authoritative, refreshable
  entitlements instead of user-selected demo manager IDs.
- Keep full entitlements in the data-access layer, ideally enforced by the DB
  through appropriate views/policies or an entitlement-table join.
- Give the model a scope summary/count; expose authorized brand lookup/search
  with pagination when names are actually needed.
- Resolve ambiguous brand names to stable IDs within the authorized scope.

None of these access/scaling changes are implemented by the Langfuse integration.

## Local deployment versus production HLD

The repository's root `docker-compose.yaml` adapts the upstream Langfuse stack
for local development; no separate source clone is needed. See README for commands.
It includes web/worker, PostgreSQL, ClickHouse, Redis and MinIO, with named volumes.
Public ports are bound to loopback; Langfuse databases have no published ports.
The separate application reports/memory PostgreSQL publishes loopback port 5433. Fixed demo
passwords, salt, session secret and encryption key live in Compose for a one-command
MVP startup. No `.env.langfuse` is required; real CLI project API keys stay in ignored
`.env`. Before production replace all demo secrets with managed secrets, including
the encryption key (plan rotation/migration for existing encrypted data). Preserve
volumes when stopping. Docker
Compose is not a highly available production deployment. Pin/review server image
versions, add backups, retention, TLS and access controls before production.
Local trace storage does not make Gemini or BigQuery local/offline services.
