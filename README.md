# Edufurther Agent

Reusable workflow runtime for the Edufurther ecosystem. It discovers, splits, extracts,
verifies and classifies candidates, and hands evidence-backed recommendations to the product
that owns them.

It is not a publisher. Scholarship Finder remains the system of record and the only service
allowed to approve or publish. **The agent proposes; it never decides.**

## Boundaries

These are the constraints the design exists to hold, not aspirations:

| The agent owns | The product owns | The router owns |
|---|---|---|
| Workflow execution state | Discovery lifecycle | Model selection and fallback |
| Tool calls and retrieval | Canonical scholarship identity | Prompts and output validation |
| Model output as *proposals* | Review task state | Budgets and rate limits |
| Evidence assembly | Approval and publication | Provider credentials |

Three rules follow from that table:

* **No product database access.** This service integrates with Scholarship Finder over HTTP
  only. `DATABASE_URL` here is the agent's own database and must never be a Finder
  connection string.
* **No direct provider calls.** Every model call goes through `edufurtherai-be` as a
  registered task. This service holds no provider keys.
* **No publication authority.** The agent emits one of four outcomes as an *input* to the
  product's existing gates. `AUTO_CHECK_ELIGIBLE` is not a publish command.

## Architecture

```text
edufurther-agent-be     workflows, tools, state, retries, evidence preparation
edufurtherai-be         LiteLLM routing, budgets, prompts, Langfuse
edufurtherSF-BE         canonical discoveries, review, approval, publication
```

## Run locally

```powershell
docker compose up -d postgres
uv sync
uv run alembic upgrade head
uv run fastapi dev
```

**On Windows, use `fastapi dev`, not `fastapi run`.** The LangGraph
checkpointer runs on psycopg, which cannot use Windows' `ProactorEventLoop`.
Uvicorn 0.36+ passes a loop factory straight to `asyncio.run`, bypassing the
global event loop policy, and picks that loop whenever it is not running
subprocesses - so `fastapi run` gets it and `fastapi dev`, which reloads,
does not. Linux defaults to a selector loop, so deployments are unaffected
and `railway.toml` is correct as written. Getting this wrong raises a clear
error rather than stalling; see `core/eventloop.py`.

Tests need their own database:

```powershell
docker compose exec postgres psql -U postgres -c "CREATE DATABASE edufurther_agent_test"
$env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:55434/edufurther_agent_test"
uv run alembic upgrade head
uv run pytest
```

Local Postgres is on **55434** - not 5432, and not Scholarship Finder's 55433, because
running both services at once is the normal case for this work.

`SKIP_DB_TESTS=1` skips the database tests. A pass with them skipped is not a full pass.

## Endpoints

| Method | Path | Auth |
|---|---|---|
| GET | `/health` | none - liveness, touches nothing external |
| GET | `/ready` | none - database probe plus schema drift report |
| POST | `/api/v1/internal/agent/jobs` | `X-Service-Token` |
| GET | `/api/v1/internal/agent/jobs/{job_id}` | `X-Service-Token` |
| POST | `/api/v1/internal/agent/jobs/run-due` | `X-Service-Token` |

`run-due` drains the queue now and reports what it did. It is the manual
escape hatch: Scholarship Finder needed one even with scheduled delivery in
place, and this service has no external queue at all.

Submission returns **202**, not 200. A run fetches several pages and makes several model
calls; holding an HTTP request open for that is how Scholarship Finder's inline job execution
ended up bounded by its callback lifetime. Progress is read from `GET /jobs/{job_id}`.

Submitting the same work twice returns the same job with `created: false`. That is enforced
by a unique constraint on the job's idempotency key, not by a check in the handler - a
code-level duplicate check does not survive two workers racing.

## Workflows

A workflow is addressed by `use_case_id`. An unregistered one is rejected at
submission with 422 rather than queued as a job guaranteed to fail later.

| Use case | Purpose |
|---|---|
| `scholarship_verification` | Classify a discovery's page, split a list into candidates, extract facts, locate and check the official source, extract eligibility, and recommend one of four outcomes. |
| `probe` | Exercises the runtime and nothing else - no tools, no model calls, no product writes. How you answer "can this environment claim, run, checkpoint and finish a job?" without pointing it at real discoveries. Its payload can request a delay or a simulated transient or permanent failure, so the retry and lease paths are testable in a deployed environment. |

### The scholarship workflow

```text
load_discovery → fetch_source → classify_page
   ├─ individual / aggregator → single_candidate
   ├─ list                    → split_candidates
   └─ not_a_scholarship       → decide  (short-circuit)
→ dedupe_candidates → extract_facts → find_official_source
→ fetch_official → compare_evidence → extract_eligibility → decide
```

The branch after classification is why this service exists. The product has
no notion of a page holding many awards, so a blog roundup of ten
scholarships arrives as one discovery and leaves as one - and that case
dominates the backlog.

**The model proposes; deterministic code decides.** A model classifies a
page, splits a list and reads facts out of prose. It never decides whether
two values agree (`fact_matching`), whether two candidates are the same
award (`normalization`, using the product's own key derivation), or which
outcome applies (`policies`). Those are pure functions, so a recommendation
is reproducible over the same evidence and every boundary condition is
testable without a database, a network or a model.

**Deterministic and model extraction are both kept, never merged** - the
product's rule for `extracted_facts` versus `ai_extracted_facts`, for the
reason that survives here too: different provenance, and merging leaves a
reviewer unable to tell which part came from where.

**One bad candidate never discards the batch.** Each is handled in
isolation and its failure recorded as an uncertainty reason.

### Outcomes

| Outcome | Meaning |
|---|---|
| `REJECT_RECOMMENDED` | The official page contradicts the claim. Evidence *against*, not evidence missing. |
| `MORE_EVIDENCE_REQUIRED` | No official source, or it could not be fetched, or a required claim has no evidence. |
| `AUTO_CHECK_ELIGIBLE` | Official page corroborates funding and deadline, and eligibility was extracted. **Not a publication command** - an input to the product's existing gates, which remain the only thing that can publish. |
| `REVIEW_REQUIRED` | Formed, but needing judgement. The default, and the right default. |

Every outcome carries its reasons: "we do not know, and here is why" is
usable for a reviewer where an empty field is not.

### A limitation to read into the pilot's numbers

`find_official_source` has no search tool in this build. It accepts a
candidate link that leaves the source's domain, or one that stays on it
when the source is graded A or B - the product grades sources precisely so
this question can be answered, and C or D means an aggregator. An
aggregator linking to its own page is not corroboration, however plausible
it looks; accepting it is how one list page ends up standing as proof of
every award on it.

The consequence is that candidates whose official page cannot be identified
this way come out as `MORE_EVIDENCE_REQUIRED`. That is correct behaviour,
not a failure, and it caps the official-source discovery rate until Tavily
arrives with the harvest migration.

### Academic requirements

Structured rules in the source's own scale and wording - never a single
`minimum_cgpa`, and never converted between grading systems. A 3.0/4.0 is
not a 75% and a 2:1 is not a GPA; inventing an equivalence makes an
applicant's real result unrecoverable. An unrecognised scale is recorded as
`unknown` rather than guessed at, and every rule carries
`equivalency_status: not_converted`.

## Execution and recovery

Submission commits the job row, then hands it to a background task -
committed first, because a worker started before the transaction lands looks
for a row that is not visible yet and finds nothing to claim.

Two things make that safe when a process dies mid-run:

* **Leases.** A running job holds one, renewed on a heartbeat at a third of
  its length. The sweeper reclaims a job whose lease expired; it leaves one
  that is still being renewed alone.
* **Checkpoints.** A retry resumes from the last checkpoint rather than
  replaying completed nodes. This is load-bearing and easy to get subtly
  wrong: passing a state dict to `ainvoke` *restarts* a thread, and only
  `None` resumes it. Written faithfully and never used, checkpoints cost a
  retry every page fetch and every model call the interrupted attempt had
  already paid for.

## Tools

| Tool | Status |
|---|---|
| `direct_fetch` | Page retrieval, and the SSRF boundary |
| `jina` | Reader fallback for pages that block a direct fetch |
| `tavily` | Registered, unimplemented - arrives with the harvest migration |
| `parsebot` | Registered, unimplemented - same |

Each has a kill switch (`DISABLED_TOOLS`) checked at the point of use, so
stopping a misbehaving tool takes effect on the next call rather than
needing a deploy - and stops only that tool. A name in `DISABLED_TOOLS` that
matches no known tool is reported by `registry.unknown_disabled_tools()`,
because the dangerous failure is a misspelled switch that reads as "off"
while the tool carries on running.

### The fetch boundary

`direct_fetch` is ported from Scholarship Finder's `infra/source_fetch.py`,
and its details are deliberate: an **allowlist** rather than a blocklist,
`all()` rather than `any()` across resolved addresses, carrier-grade NAT
excluded explicitly, and `follow_redirects=False` with exactly one manually
re-validated hop. Two changes were made in the move, both covered by tests:
a relative `Location` is resolved rather than rejected, and the size limit
is enforced while streaming rather than after the whole body has been
downloaded.

**A policy refusal must never trigger a fallback.** Jina fetches from its
own infrastructure, so falling back to it after the guard refused a URL
would use it to walk around the guard. `ValueError` from the guard
propagates untouched, and `tests/test_retrieval_integration.py` asserts
Jina is not reached for a refused domain, a private host, or an oversize
response.

### Retrieval precedence

Two paths, in opposite orders, for the same reason Scholarship Finder has
two:

* `fetch_page` - direct first, Jina on failure. Ordinary retrieval wants
  *some* content.
* `fetch_official_page` - Jina first, direct as fallback. Verification
  extracts facts from the text, and raw HTML is markedly worse input than
  rendered markdown.

### Budgets

`agent_research_usage` counts provider calls per calendar month, reserved
before each call in a single atomic upsert so two callers can never both
take the last one. The period is derived from the timestamp rather than
reset by a job, so a missed cron cannot hand out an unlimited month.

While both services hold a key for the same provider they count separately
and the real quota is the sum - which is why Scholarship Finder's
`JINA_API_KEY` is unset at cutover.

## Idempotency and workflow versions

A job's key is `{use_case}:{workflow_version}:{input_reference}`, deterministic so a
redelivery collapses onto the existing job, and versioned so that changing the workflow can
deliberately reprocess a record instead of being suppressed as a duplicate. That second
property is the gap Scholarship Finder's one-time `auto_review_evaluated_at` marker left
open.

## Migrations

Alembic, run explicitly - never on container start. `/ready` reports drift between the
applied revision and the newest revision on disk, but never fails the probe over it:
refusing readiness because a migration is pending takes the service down at exactly the
moment someone is deploying the migration that fixes it.

## Configuration

See [`.env.example`](.env.example). Two defaults are deliberately the cautious ones:

* `SHADOW_MODE=true` - evidence and run records are written, review tasks are never touched.
  An unconfigured environment cannot affect the product's review queue.
* `INTERNAL_SERVICE_TOKEN` unset fails every authenticated request closed, and the service
  refuses to boot without it in staging or production. It is what this service *accepts*;
  the credential it *sends* to the product is `SCHOLARSHIP_FINDER_AGENT_TOKEN`, and the two
  are different secrets. Scholarship Finder's own `AGENT_SERVICE_TOKEN` is a third thing
  again - what Finder accepts from this service - which is precisely why the inbound one
  here is not called that.

`AUTO_APPROVE_ENABLED` is a Scholarship Finder variable. It is not an agent variable, and it
stays `false` for the duration of this build.

## Checks

```powershell
uv run python scripts/check.py
```

Runs the suite with coverage, then ruff and mypy.
