# Design documents

Start with **[PRODUCTION_HLD.md](PRODUCTION_HLD.md)**: the high-level design with
architecture diagrams, technology choices, data handling, every requirement
(prototype status and production design), error handling and rollout. The
documents below are deep dives it links to.

## Deep dives

- [GOLDEN_KNOWLEDGE.md](GOLDEN_KNOWLEDGE.md): trio format, ingestion and update loop, retrieval, calibration.
- [EVALUATION.md](EVALUATION.md): answer and retrieval evaluation, A/B results, findings, release gates, UX evaluation.
- [QUERY_SAFETY.md](QUERY_SAFETY.md): SQL policy, demographic subset, contributor guard, repair budgets.
- [SAVED_REPORTS.md](SAVED_REPORTS.md): report snapshots, search, strict deletion confirmation.
- [USER_MEMORY.md](USER_MEMORY.md): explicit preferences, known gaps, production retrieval.
- [PERSONA_MANAGEMENT.md](PERSONA_MANAGEMENT.md): Google Doc MVP and the persona console design.
- [OBSERVABILITY_AND_ACCESS.md](OBSERVABILITY_AND_ACCESS.md): logging, tracing privacy, access scaling.
- [STAKEHOLDER_POLICY.md](STAKEHOLDER_POLICY.md): PII and access rules confirmed with the client.
- [REQUEST_SAFETY_CLASSIFIER.md](REQUEST_SAFETY_CLASSIFIER.md): optional distilled request classifier (proposal).
- [task.txt](task.txt): the original assignment.

## Diagrams (Mermaid)

Every diagram is embedded in a document so GitHub renders it; a test keeps the
embedded copies equal to these sources. The first five are in PRODUCTION_HLD.md,
security-pii in QUERY_SAFETY.md and report-deletion in SAVED_REPORTS.md.

- [production-hld.mmd](production-hld.mmd): production serving architecture on Google Cloud.
- [request-sequence.mmd](request-sequence.mmd): one question end to end.
- [learning-loop.mmd](learning-loop.mmd): user-level memory and reviewed system-level learning.
- [architecture-overview.mmd](architecture-overview.mmd): the local prototype as implemented.
- [agent-loop.mmd](agent-loop.mmd): the bounded planning loop.
- [security-pii.mmd](security-pii.mmd): SQL and result safety layers.
- [report-deletion.mmd](report-deletion.mmd): preview and confirmation flow.

Render locally with `mmdc -i <file>.mmd -o <file>.png` or any Mermaid viewer.
