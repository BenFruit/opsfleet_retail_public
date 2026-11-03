# User memory: explicit intent, short text entries and embeddings

## Implemented MVP

The internal `retail_agent/memory` module stores short standalone strings, not a
fixed preference taxonomy or a separate HTTP service. Each entry has a trusted
owner, ID, content (<=200 characters), normalized-text fingerprint, 768-dimensional
embedding, embedding model, source turn ID and creation timestamp. It lives in
`user_memories` in the existing application PostgreSQL/pgvector, NOT Langfuse's DB.

`remember` is an explicit model intention with at most 5 strings per call and one
write attempt per user turn. The model extracts only directly stated preferences
or permitted self-declared operator profile facts. Code validates the payload and
requires an explicit current-question marker (`remember`). Ordinary
one-off formatting does not authorize persistence; negative requests are rejected.
This lexical gate supplements model intent classification; it is NOT a semantic
proof against quoted instructions or a complete sensitive-data classifier.

The operator's own name may be remembered when THEY explicitly ask for it. This
is a design decision for the operator's own profile, not permission to store or
expose customer PII, and not a stakeholder ruling. Stored names
are self-reported, not verified identity, authorization claims or entitlements.
Customer names/demographics, contact details, credentials, transaction results and
instructions to bypass safety are outside this feature. Email/phone patterns are
blocked in code; broader free-text sensitivity and instruction safety rely on the
limited feature policy and need stronger production evaluation/enforcement.

Embedding is generated for EACH new entry using the same Google embedding client
as reports, with a fixed `User memory` document title. Up to 5 embedding calls may
occur inside one tool step; these are separate from the generative-model counter.
All vectors are obtained before one atomic transaction writes the entries.
Exact duplicates are normalized (Unicode, whitespace, case), fingerprinted and
deduplicated PER OWNER. A unique owner/fingerprint constraint and per-owner
transaction lock prevent duplicate writes/capacity races. Semantic deduplication
is NOT implemented yet. Ambiguous failures do not trigger automatic write retries.

## Context mounting and combined intentions

For the small MVP, load ALL of the current user's entries (maximum 20) at turn
start and mount them in a separate `user_memory` JSON context block with the user
question. No embeddings are sent back in that block. Memory is contextual data,
not trusted system policy. Current explicit user instructions override saved style
preferences, which override general persona defaults; safety/tool/access policy
always takes precedence. Memory cannot grant brand access or deletion consent.

One user message can require multiple sequential internal actions:

```text
"Show my most profitable brands in bullet points, remember that format."
    -> remember("Prefers bullet points.")
    -> query(financial SQL)
    -> ONE final analytical answer + checked memory outcome
```

Query and remember can run in either order; remembering returns to planning,
does not end the turn, and updated entries are visible to subsequent model steps.
Intent recognition remains model-based; tests cover both orderings. Only one
write attempt is allowed in a turn, so repeated planning does not duplicate writes.
Memory failure does not prevent analysis; the response exposes the checked outcome
instead of claiming successful persistence. Storage unavailability loads an empty
block marked unavailable rather than crashing the analytical CLI.

`/memories`, `/forget ID` and `/forget-all` bypass the LLM and act only on the
trusted current owner. These explicit commands concern personal memory, not Saved
Reports: report deletion retains its separate preview/confirmation flow. `/reset`
and restart clear conversation history but DO NOT erase persistent memory.
Memory-only acknowledgments do not replace the last analytical report snapshot.

## Production: selective retrieval, not unlimited prompt stuffing

The full-memory context block is intentional MVP simplification, not the target
architecture. At larger scale use **context-budgeted memory retrieval**:

- Separate a small pinned preference/profile layer from episodic/long-tail facts.
  Core formatting preferences should not disappear just because their embedding
  is semantically distant from a sales question.
- Apply mandatory owner/tenant scope BEFORE similarity ranking, with provenance,
  consent, retention/TTL and sensitivity filters. Never share memory across users.
- Retrieve candidates using hybrid lexical/vector search, rerank for relevance,
  recency and importance, then select a bounded top-k within a token budget.
- Consider temporal decay, salience scoring and MMR/diversity selection to avoid
  repetitive context. These affect ranking, not authorization or truthfulness.
- Track retrieval provenance and memory versions for reproducibility. Evaluate
  precision/recall@k, answer utility, token use, latency and unsafe context inclusion.

## Production: semantic deduplication and memory lifecycle

Stored per-entry embeddings enable future **owner-scoped near-duplicate candidate
generation**. Use cosine similarity as a candidate signal, with calibrated
thresholds and a semantic verification step before merging/deleting entries.
Never deduplicate users against each other or treat similarity as equivalence:
"prefers short answers" and "does not prefer short answers" can be nearby vectors.

Add conflict resolution, explicit correction/supersession, timestamps and validity
windows rather than blindly merging contradictory preferences. Preserve provenance
and enable user inspection/undo. A new preference should not silently override
unrelated facts. Measure false merges separately from missed duplicates.

Also add authenticated identity, database-enforced isolation, idempotent writes,
managed secrets/TLS, deletion/retention controls and consent-aware observability.
Both embedding requests and mounted context leave the machine for Google APIs;
local PostgreSQL is not an offline/private inference guarantee. Raw memory text
may appear in enabled Langfuse traces; sanitization remains deferred. Re-embedding
requires model/dimension compatibility and a versioned migration plan.

## Observed MVP behavior and the production fix

A request to remember both an operator name and long-answer preference saved only
the preference; the name was saved on a second request. This demonstrates incomplete
model extraction, despite a successful tool outcome. A subsequent implicit change
to short answers unnecessarily invoked `remember`; code rejected it because the
current question lacked explicit persistence intent. The old preference remained.

Updating/superseding a stored preference is not implemented. An explicit new
preference may coexist with a contradictory old one; vector similarity is not
conflict resolution. Both are accepted for the MVP. In production, preferences
become slots (format, depth, language, charts) where the latest explicit value wins
and the old one is kept as history, and every save echoes what was stored so a
partial extraction is visible at once (see the lifecycle section above and
PRODUCTION_HLD.md §5.4). Current-turn
preference precedence is an instruction to the model, not a guaranteed behavior.

This is user-level memory, NOT automatic system learning or persona administration.
See PRODUCTION_HLD.md for the separate reviewed system-learning loop.
