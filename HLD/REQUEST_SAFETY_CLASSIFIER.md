# Proposed request-safety classifier (design only)

Not implemented in the MVP. Add a small, locally deployable classifier before
the analytical LLM as an additional defense layer, not an authorization boundary.

## Dataset and teacher labeling

1. Collect policy-compliant examples from approved, sanitized interactions and
   generate synthetic allowed/prohibited requests, including adversarial variants.
   Never use actual customer PII as training examples.
2. Include ordinary retail questions, allowed demographic aggregates, prohibited
   individual demographic disclosure, direct PII extraction, access bypass,
   prompt injection, destructive-action confirmation bypass, and unrelated tasks.
   Include typos, paraphrases and difficult near-boundary pairs.
3. Give a strong teacher LLM the versioned stakeholder policy and relevant
   conversation/access context. Have it label requests `allow`, `deny`, or
   `uncertain`, with a policy reason/category. A restricted request is not proof
   that the user is malicious: classify the requested action, not the person.
4. Review ambiguous and representative labels with humans. Teacher outputs are
   noisy pseudo-labels, not ground truth. Balance legitimate and prohibited cases.

## Distillation and runtime use

Train/fine-tune a smaller text classifier using the teacher-labeled, reviewed
dataset (behavioral distillation). It predicts the policy decision and confidence;
it does not need to generate a full analytical response or teacher explanations.

```text
Question + bounded relevant context
    -> small safety classifier
        -> confident prohibited: refuse or offer a safe aggregate alternative
        -> allowed: analytical LLM -> normal tool enforcement
        -> uncertain: stronger policy check or clarification; never bypass checks
```

The classifier must understand relevant context for follow-ups: "show their IDs"
after an age filter differs from ordinary top-customer spending by pseudonymous ID.
Stored reports and database strings remain untrusted; this input classifier alone
does not prevent indirect prompt injection from retrieved content.

## Evaluation and lifecycle

- Separate training/test sets by source scenario and paraphrase family to avoid
  synthetic-template leakage. Maintain held-out attacks and benign hard negatives.
- Measure prohibited-request recall/false negatives by policy category, benign
  false rejections, confidence calibration, abstention rate, latency and cost.
- Select thresholds from measured tradeoffs, not an arbitrary confidence number.
  Test end-to-end tool behavior as well as request classification.
- Version policy, labels and model; re-evaluate after policy/model changes.
  Collect sanitized reviewed failure cases for subsequent training iterations.

Even a confident `allow` is not execution permission. Keep deterministic PII,
SQL, brand-access, result-boundary and explicit deletion-confirmation checks.
Document residual risks and fallback behavior; distillation does not establish
a formal privacy or prompt-injection guarantee.
