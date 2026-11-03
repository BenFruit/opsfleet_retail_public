# Confirmed access and demographic policy

Source: Slack clarifications from Harry and Vlad (OpsFleet). These clarifications
resolve earlier policy questions. They do not imply every rule is
already implemented; see implementation status below.

## Confirmed rules

1. Names, email, street address, postal code, precise coordinates and related
   direct PII must never reach the LLM or final output.
2. City, state, country, age, gender and traffic source are aggregate-only.
3. Customer user_id or a stable pseudonymous ID may be used for individual
   non-demographic analysis and follow-ups.
4. A demographic filter followed by individual customer output reveals the
   attribute even if it is not selected. Block such output or answer with an
   aggregate instead. Example: do not list IDs of 26-year-old customers.
5. Counts, averages and grouped demographic statistics are permitted. An output
   tying a demographic attribute to one person (e.g. the age of the top spender)
   must be blocked, regardless of whether SQL uses an aggregate function.
6. Small-group suppression protects demographic results. A product's revenue
   with only two buyers may be shown without customer identities or demographics.
   Where suppression applies must be documented. No numerical minimum was
   mandated; any chosen default should be configurable and documented.
7. Brand is the access dimension. Managers' customer/spend metrics include only
   purchases of authorized brands. A simple mock entitlement mapping is accepted.
8. For the prototype, SQL checks, access enforcement and small-group suppression
   in code are sufficient. Cross-question inference checks are NOT required in
   code; describe the risk and production mitigation in the HLD.

## Implementation status

- Direct PII source references are rejected; brand-scoped SQL execution exists.
- Customer IDs and non-demographic financial analysis are supported.
- One SQL action supports ordinary queries and structurally checked demographic
  aggregates, including computed groups. Code adds configurable cohort suppression.
  The default
  is 5 distinct contributing buyers (CLI --min-cohort-size, minimum 2), not a
  client-mandated threshold. Ordinary product revenue is not suppressed.
  See QUERY_SAFETY.md for the contract.
- Generic free-text PII detection is not solved by the current email/phone checks.
- Cross-question inference remains a documented production concern, not a planned
  prototype subsystem.

## Separate operator-profile decision (not a stakeholder clarification)

Design decision: the agent may remember an analyst/operator's own voluntarily supplied
name on explicit request. This is self-declared profile context, never verified
identity or access permission. It does NOT relax the customer-dataset PII rules
above. The feature sends selected operator memory to Gemini/embedding APIs; see
USER_MEMORY.md for consent, scope and remaining free-text/trace privacy limitations.
