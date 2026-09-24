# Stage 1 evaluation

Measured 24 September 2026 against the live staging deployment, on the
discoveries processed to that date. Read-only: every figure below comes from
querying the two production databases, none from a test fixture.

Rollout stage 1 (spec §19) asks for 20–50 records processed without changing
publication state, then this report against the §16 metrics, before review
write-back is enabled.

**Verdict: do not enable write-back yet.** Two findings below, one of which is
a gate blocker. The reasoning is in "The write-back question".

---

## 1. Did shadow mode hold?

Stage 1's promise is that nothing in the product changed. The flag being set
is not evidence of that — the flag can be right and the code wrong — so these
look for the footprint a breach would have left.

| check | result |
|---|---|
| `SHADOW_MODE` on the deployment | `true` |
| `AUTO_APPROVE_ENABLED` on Finder | `false` |
| review tasks moved off `open` | 0 |
| discoveries created by an agent split | 0 |
| scholarships published or withdrawn since the first run | 0 |
| `audit_log` rows since the first run | none |
| `auto_approval_audits` since the first run | 0 |
| `scholarship_revisions` since the first run | 0 |

One result needed chasing. Four review-task rows (two distinct tasks) were
updated *after* an agent run on the same discovery, which is the shape a
breach would have. They are not one: both carry Finder's own auto-review
payload — `verdict / reasoning / draft_version / proposed_facts /
proposed_award_type` — identical in shape to drafts on discoveries the agent
has never seen, and the reasoning itself reads *"No automated
fetch-and-cross-check has run."* The agent's `request_review` writes
`reason="agent_review_required"`, which appears nowhere. 1,162 of the 1,164
drafts in the table predate the first agent run.

**Shadow mode held.**

---

## 2. What was processed

The unit throughout is the **latest run per discovery**. Re-runs during
debugging would otherwise weight a record by how often it was retried, and 62
runs cover only 24 discoveries.

| workflow version | discoveries |
|---|---|
| `sv-gradeA-3`, `-4`, `-5` | 16 each (the same 16 records, three passes) |
| `sv-lists-2` | 5 |
| `sv-roundups-1`, `-2`, `-3` | 3 each (the same 3 records) |

24 distinct discoveries — inside the 20–50 band, at the low end. They yielded
**101 candidates**.

| page type | pages | share |
|---|---|---|
| individual | 19 | 79.2% |
| list | 3 | 12.5% |
| aggregator | 2 | 8.3% |

Three list pages produced 82 of the 101 candidates. Decomposition works: the
50 / 25 / 5 splits were checked by hand against the source pages, and 0 of the
101 identity keys carry a list ordinal, confirming that fix holds on real data.

---

## 3. The §16 metrics

Measurable from the pilot:

| metric | value |
|---|---|
| official-source discovery rate | **25.7%** (26 of 101) |
| official page fetched, of those found | 100% (26 of 26) |
| funding corroborated | 7.9% (8) — 0 conflicts, 93 not established |
| deadline corroborated | 2.0% (2) — 0 conflicts, 99 not established |
| eligibility rules extracted | 91.1% of candidates got ≥1 rule |
| duplicate rate | 1 of 101 collapsed by identity key |
| workflow failure rate | 4.1% (5 of 123 jobs) |
| average processing time | 67.9s (min 0.6s, max 892.9s) |
| retry distribution | 114 jobs first attempt, 7 second, 2 third |
| Jina usage | 109 calls in 2026-09, of 500/month |

All 5 failed jobs are `ValueError: Source URL domain is not approved` — the
SSRF gate refusing an unapproved host, and the runtime parking the job as
permanent rather than retrying it. That is the design working, not a fault.

Fetch reliability: `fetch_page` 117 ok / 9 error / 5 refused (6.9% error),
`fetch_official_page` 90 ok / 0 error. Official-page fetches average 7.3s,
source fetches 5.6s, with a 38s worst case.

**Not measurable, and why.** Extraction, deadline, funding and CGPA accuracy,
list-decomposition precision and recall, and human override rate all need the
human-labelled fixture set §16 asks for, plus human decisions the shadow run
deliberately never requested. They stay open. The 25 German awards and the
five-heading page were verified by hand, which is a spot check, not a metric.

**Cost per usable candidate** is the number that should worry us: 109 Jina
calls and 118 model workflows produced **8 fully corroborated candidates** —
roughly 15 retrieval calls each.

---

## 4. The write-back question

The pilot exists to answer one thing: if shadow mode came off, would the
candidates reaching a reviewer save time or cost it?

| what a reviewer would receive | candidates | share |
|---|---|---|
| **corroborated** — official page read, ≥1 claim agreeing | 8 | 7.9% |
| **partial** — official page read, no claim settled | 18 | 17.8% |
| **bare** — no official page; the reviewer starts cold | 75 | 74.3% |

Queue impact:

```
current open review tasks                 1014
new discoveries from write-back            100   (101 less 1 duplicate)
queue after write-back                    1114   (+9.9%)
of which arrive corroborated                 8
```

Twenty-four discoveries would grow the review queue by 9.9%, and three
quarters of what lands gives the reviewer no more than they had. **The agent
would be adding to the backlog it was built to drain.**

The cause is known, and the build plan states it as a Phase 1 limitation:
`find_official_source` had no search tool when these records were processed. It works from links on the source
page, the candidate's own URL, and the provider's `approved_domains` — so on a
roundup that names awards in prose without linking them, it finds nothing. The
25.7% official-source rate is that limitation, measured.

This is not an argument that the agent is wrong. Of 101 candidates it produced
**zero false conflicts** — no funding figure and no deadline was called a
contradiction. Everything it could not establish, it declared. The pipeline is
honest; it is starved of official sources.

---

## 5. Gate blocker: the model is never recorded

Spec §16 requires model used, prompt version and workflow version for every
job. The build plan's gate requires the same three per run.

| field | `agent_runs` | `discovery_evidence` | `agent_outputs` |
|---|---|---|---|
| `workflow_version` | 62 / 62 | 39 / 39 | — |
| `prompt_version` | **0 / 62** | 39 / 39 | **NULL** |
| `model` | **0 / 62** | **0 / 39** | **NULL** |

`model` is recorded nowhere, in either database.

The router does return it. `ExecuteResponse.model` exists in `edufurtherai-be`
and its own comment says why:

> Which model actually answered, after fallback. Callers persist this with the
> facts they derive: "model used" is not answerable from
> `model_policy_version`, which names the routing policy rather than the model
> it selected, and a per-model accuracy regression is invisible without it.

The agent drops it at the client boundary: `AIRouterResponse`
(`src/app/integrations/ai_router.py`) has no `model` field, so nothing reads it
off the response. `Candidate.model` is declared at `schemas.py:157` and never
assigned. Everything downstream then writes `None` faithfully.

`agent_runs.prompt_version` is a second, smaller gap: `record_run` takes no
`prompt_version` argument, which is why evidence rows carry one and run rows do
not.

Nothing is broken today. The cost is future: if quality moves after the router
swaps a model, we cannot attribute it — which is the entire reason §16 asks for
the field.

---

## 6. Recommendation

1. **Do not enable write-back.** It would grow the queue by 9.9% and hand
   reviewers 75 cold records. Revisit when the official-source rate is the
   thing that has changed, not the record count.
2. **Fix the provenance gap** before any further evaluation, so the next one is
   not run on unattributable data. Small and well understood.
3. **Then give `find_official_source` a search tool.** It attacks the 25.7%
   directly and is the only change that moves "bare" into "corroborated".

   *Done, after this report: see `official_source.py`.* The plan named Tavily;
   Jina turned out to sell search on the key we already hold, so it needed no
   new vendor. Replayed against these same 101 candidates the rate goes
   **25.7% → 65.3%**, 40 of the 75 bare candidates given a page. That is reach,
   not corroboration: whether those pages agree with the claims is what the
   next run measures, and this report's other numbers stand until it does.
4. **Build the §16 fixture set** with human labels. Without it, accuracy,
   precision and recall stay unmeasured however many records are processed.

Stage 2 (spec §19, 100–300 records with review write-back) should wait on 2
and 3.
