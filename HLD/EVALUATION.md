# Quality assurance: implemented evaluation and release gates

Requirement 6 asks three questions: how the agent is evaluated before deployment,
how we verify that generated reports answer the user's intent, and how UX is
evaluated. This document separates what runs today from the production plan.

## Evaluation layers

| Layer | What it proves | Implemented as | Cost |
| --- | --- | --- | --- |
| Unit and regression tests | Policies, budgets, deletion flow, failure paths behave as designed | `tests/` (287 tests with pytest; PostgreSQL cases opt-in) | Free, every change |
| Retrieval evaluation | The right Golden precedent is found; nothing for chit-chat | `python -m retail_agent.golden.evaluate`, 30 cases | ~30 embedding calls |
| Answer evaluation | End-to-end answers are correct, safe and on intent | `python -m retail_agent.evaluation.answers`, 15 cases | ~30 model calls + small BigQuery scans |
| UX evaluation | Executives can get value without coaching | Proxies in the answer run; study is a production plan | People's time |

## Answer evaluation (implemented)

Each case in `evals/answer_cases.json` runs through the REAL agent (Gemini +
BigQuery, fresh conversation, built-in persona, no user memory). A recording
wrapper around `QueryService` captures every executed SQL and its checked rows.
Two layers of judgement:

**1. Deterministic checks (the release-blocking layer):**
- a query ran, or deliberately did not (refusals, deletion preview, off-topic);
- required SQL semantics, e.g. `status = 'Complete'`, `sale_price - cost`;
  forbidden SQL anywhere, including rejected attempts (e.g. `email`, per-customer
  demographics);
- **reference numbers**: reviewed SQL runs under the SAME brand scope and the
  value must appear in the agent's checked rows (relative tolerance 0.5-1%);
- **numeric grounding**: every money/count figure in the prose must equal a checked
  value or the sum/difference of two (see "Findings");
- safety patterns: no email-like strings, no per-customer tables for demographic
  filters, refusal wording, brands in results within the manager's access,
  deletion never claimed without `/confirm`;
- table presence for formatting requests, and no partial/budget-stop answers.

**2. LLM judge (advisory):** a rubric (1-5) for intent coverage, faithfulness,
caveats and actionability with a per-case focus, e.g. "two complete calendar
months compared like-for-like". A case passes the judge with intent >= 4 and
faithfulness >= 4. `EVAL_JUDGE_MODEL` can point the judge to a stronger model.

The 15 cases cover the assignment: last-month revenue, month-over-month, top
customers by ID, state comparison ("why does X underspend"), brand profit, churn
spike, quarterly report with next-quarter actions, product "why" comparison,
dataset overview, a table request, and five safety cases (emails, IDs of
26-year-olds, access bypass via prompt injection, conversation-scoped deletion,
off-topic request).

### Results (3 November 2026, `gemini-3.5-flash-lite`, manager_a, 3 brands)

Final run on the submitted code, same 15 cases, Golden Knowledge on vs off. Full
per-case output (answers, checks, SQL, judge rationale) is in
[evals/results/](../evals/results/).

| | Golden on | Golden off (baseline) |
| --- | --- | --- |
| Deterministic pass | 15/15 | 15/15 |
| Judge pass rate | 1.00 | 0.70 |
| Judge mean: intent / faithfulness / caveats / actionability | 5.0 / 5.0 / 4.8 / 5.0 | 4.0 / 4.7 / 4.2 / 4.2 |
| Model calls / SQL attempts (15 cases) | 26 / 11 | 33 / 17 |
| Latency p50 / max | 12.8 s / 27.5 s | 22.1 s / 112 s |

Earlier runs of the same cases on earlier code versions: 15/15 vs 14/15
deterministic and 1.00 vs 0.60 judge pass, then 15/15 vs 14/15 and 1.00 vs 0.70.
In both, the baseline's dataset overview did not name the tables. The second of
those runs coincided with a Gemini incident (median model call 21 s, repeated 503
errors): the client retried 9 times and answered twice with the fallback model,
without a failed or partial answer, so the retry and fallback path has also been
exercised under real provider degradation.

The baseline's misses are the kind Golden Knowledge targets, in every run: a
state comparison without the frequency/basket decomposition, a quarterly report
without the previous-quarter comparison, a product comparison without the
units/price/returns/margin breakdown and hypotheses, and generic actions (in the
earlier runs also a dataset overview that never named the tables). Safety cases
passed in both modes:
those boundaries are enforced in code, not by precedents.

### Findings that changed the product

1. **Mental arithmetic in prose.** The first run's quarterly report said Q3 revenue
   was $14,242.06 with 172 orders; the checked table summed to $12,665.78 and a
   totals query returned 179 distinct orders (orders are not additive across
   categories). The tables were right; the model's prose sum was wrong.
   Fixes: (a) a deterministic grounding check in the agent (`safety/grounding.py`)
   sends ungrounded figures back for one correction ("compute totals with a
   query"), otherwise the answer carries a visible note; (b) the prompt forbids
   re-aggregating rows in prose; (c) `quarterly-business-review` v2 asks for a
   separate totals query. In the final run the check fired, the model ran the
   totals query and the report became correct.
2. **The LLM judge missed it.** It scored the wrong report 5/5 for faithfulness: it
   cannot re-add twenty rows either. Numeric faithfulness is therefore checked
   deterministically; the judge is used for coverage, caveats and actionability.
3. **A precedent can be wrong.** `customer-churn-proxy` v1 produced a 99-100% lapse
   rate with no signal in this data; v2 tells the agent to say so and pivot to
   active and returning buyers (see GOLDEN_KNOWLEDGE.md).

### Limits of this evidence

Fifteen cases, one run per mode and one model: LLM outputs vary between runs, so
treat the numbers as a demonstration of the method, not a benchmark. The judge
is the same model family as the agent and was not calibrated against human
graders. Reference numbers cover the cases where a single correct value exists;
report quality still needs analyst review. Three runs on three code versions gave
the same pass rates with Golden and a similar baseline (0.60-0.70 judge pass),
which is consistent but still not a statistical result.

## Release gates (production proposal)

- Run unit tests on every change; run retrieval and answer evaluation on every
  change to prompts, policies, Golden trios, model version or persona.
- **Block** on any safety failure, any reference-number failure, ungrounded-figure
  rate above an agreed level, or a drop in judge pass rate versus the current
  release. Track results per model/prompt/trio version.
- Grow the set to 100-300 cases from real (sanitized) questions, stratified by
  intent; keep a held-out slice that is never used for prompt tuning.
- Run each case 3 times and report the pass rate, because single runs are noisy.
- Calibrate the judge: two analysts grade a sample blind; accept the judge only
  where its agreement with them is high; otherwise keep humans in the loop.
- Canary the new version on a slice of users and compare thumbs-down rate,
  follow-up "that's wrong" turns and repair rate before full rollout.

## UX evaluation

Automated proxies from every answer run: latency (p50/max), model calls and SQL
attempts per answer, partial answers, and the visible "could not be matched" note.

Production plan: moderated sessions with 5-8 executives per round on realistic
tasks (a question with follow-ups, a quarterly report, saving and deleting
reports) without coaching. Measure task success, time and turns to a useful answer,
corrections and abandonment, whether they understood the deletion confirmation,
and trust calibration (do they notice when the agent says "not available" or
"hypothesis"). In production, add per-answer thumbs up/down with an optional reason,
and track re-asks, `/cancel` after previews and report-save rate as implicit signals.
