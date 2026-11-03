# Persona Management: Google Doc MVP and admin-console target

## Implemented MVP

Split the model system instruction into two layers:

1. **Editable presentation:** tone, audience, brevity and organization of narrative.
   A small Google Doc is a convenient editing surface for a non-developer.
2. **Mandatory application policy:** allowed scope, action schemas, SQL/PII/access
   rules, business assumptions, evidence requirements, budgets and deletion flow.
   This remains in code and overrides conflicting presentation instructions.

Dynamic values (question, schema, brand entitlements, history, observations and
budgets) remain structured runtime context. There is no template interpolation
of document contents, no executable code and no credentials in the document.

`llm/persona.py` has two parts: `GoogleDocReader` fetches the configured Google
document as bounded plain text, and `PersonaStyle` keeps the current style, falling
back to the last good version or the built-in default (`llm/prompts/default_style.md`).
The document is read at the start of each accepted question, once per turn. All subsequent plan/query/report/remember/model
steps retain that immutable snapshot. Changes take effect on the NEXT question
without restarting/redeploying the application. Existing reports are unchanged;
opening a saved document prints its stored content, not a newly styled rewrite.
Deterministic CLI messages and checked tables are not edited by the persona.

The MVP uses unauthenticated HTTPS text export, so a configured document must
permit **Anyone with the link: Viewer**, while editor access stays restricted to
trusted administrators. It must contain ONLY non-sensitive style instructions.
Do not publish secrets, user/customer data, business results or internal security
instructions to make this feature work. An organization that prohibits public-link
documents should leave it disabled; authenticated Docs API access is future work.

Accept a document ID or standard HTTPS `docs.google.com/document/.../d/ID` link.
Extract the ID and reconstruct the fixed text-export endpoint; arbitrary URLs and
file paths are rejected. Fetches use a 3-second socket
timeout and bounded reads (16 KB and 4,000 characters). HTML/login pages, empty,
oversized or invalid responses are rejected. Failures retain the last successfully
loaded IN-PROCESS version, or the built-in default on a fresh process. This is not
a durable configuration store or a hard total wall-clock fetch deadline.

A SHA-256 content fingerprint (12 hex characters) is included in model instructions
and local `persona_loaded` events; Langfuse model inputs show the selected text and
version. Raw document contents/URLs/errors are not included in local log events.
This fingerprint is for correlation, not approval, authenticity or release control.

Style/policy precedence inside a prompt is probabilistic, not a security barrier.
Deterministic tool authorization, column/result checks and CLI confirmation remain
independent and cannot be disabled by editing this document. The document editor
is trusted, and current arbitrary-text style configuration is not production-safe
instruction management. Sensitive trace handling remains separately deferred.

## Production design: administrator console (beyond the MVP)

- Provide authenticated admin UI with RBAC and an audit trail of who changed what.
- Store structured style settings/templates in a versioned configuration store
  (e.g. PostgreSQL/Cloud SQL), rather than a publicly readable Google Doc.
- Keep security/tool policies separate and not editable through the tone editor.
- Support draft -> preview against representative reports -> regression evaluation
  -> approve/publish. Reject changes that alter security intent or tool contracts.
- Resolve a published version at turn start; use bounded cache/refresh and a
  persistent last-known-good version. Record the version on traces/reports.
- Support rollback, staged rollout and clear behavior for active conversations.
- Evaluate tone adherence AND numerical fidelity, privacy, injection resistance,
  confirmation and latency before publishing. Prompt precedence alone is insufficient.

Persona is global presentation configuration, NOT per-user preference memory and
NOT system-level learning. The user-memory MVP is now described in USER_MEMORY.md:
current explicit requests override remembered style preferences, which override
general persona defaults; mandatory safety/tool policy always wins. Curated
system-level improvement remains a separate design requirement.
