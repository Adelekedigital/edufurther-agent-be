# Operations runbook

For whoever is running the pilot, and for whoever is on the other end of it
going wrong.

## The one thing to know first

`SHADOW_MODE` decides whether this service can affect Scholarship Finder's
review queue. It defaults to `true`, and Stage 1 of the rollout runs that
way.

| Setting | Writes | Effect on the product |
|---|---|---|
| `SHADOW_MODE=true` | `agent_runs`, `discovery_evidence` | None. Nothing in review, approval or publication reads either table. |
| `SHADOW_MODE=false` | also candidates and review requests | Creates discoveries and opens review tasks. |

Nothing in either mode can publish. The credential this service holds is
scoped to `/internal/agent/*` and has no route to publish, withdraw, or
decide a review — Scholarship Finder enforces that with a separate token,
so it does not depend on this service behaving.

`AUTO_APPROVE_ENABLED` is a Scholarship Finder variable and stays `false`
for the duration of this work.

## Before a batch

```powershell
# 1. Both services healthy, and the agent's schema current.
curl https://<agent>/health
curl https://<agent>/ready          # migration.up_to_date must be true

# 2. Confirm the mode you think you are in.
#    This is the check people skip and regret.
curl https://<agent>/api/v1/internal/agent/metrics -H "X-Service-Token: $TOKEN"

# 3. See what would be submitted, without submitting it.
uv run python scripts/batch.py --limit 20 --dry-run
```

## Running a batch

```powershell
uv run python scripts/batch.py --limit 20
```

Submits jobs; it does not run them. The service's worker starts each one
immediately and the sweeper picks up anything the worker missed. A batch
interrupted halfway leaves durable, resumable jobs rather than losing work,
and re-running the same command submits nothing twice — the idempotency key
is `{use_case}:{workflow_version}:{discovery_id}`.

Watch progress:

```powershell
curl https://<agent>/api/v1/internal/agent/jobs/<job_id> -H "X-Service-Token: $TOKEN"
curl "https://<agent>/api/v1/internal/agent/metrics?workflow_version=<version>" -H "X-Service-Token: $TOKEN"
```

## Reading the metrics

Counts, not rates, on purpose. A job that completed with
`MORE_EVIDENCE_REQUIRED` did its work correctly; a success rate would
invite reading that as a failure.

| Field | What it tells you |
|---|---|
| `jobs.by_state` | `failed_review` is the one to look at — durable and inspectable, never retried into silence. |
| `outcomes` | Expect `MORE_EVIDENCE_REQUIRED` to dominate early. See the limitation below before reading that as a quality problem. |
| `tool_calls.by_status` | `refused` means the fetch guard rejected a target. That is the guard working, not a fault. |
| `errors_by_class` | `permanent` errors will not resolve on their own. |
| `attempts_per_job` | Above 1.0 means work is being redone. Worth looking at before widening the batch, whatever the outcomes say. |

### A limitation that will show in the numbers

`find_official_source` has no search tool in this build. It accepts a
candidate link that leaves the source's domain, or one that stays on it when
the source is graded A or B. An aggregator linking to its own page is not
corroboration, so candidates on grade C/D sources without an outbound link
come out `MORE_EVIDENCE_REQUIRED`.

That is correct behaviour and it caps the official-source discovery rate
until Tavily arrives with the harvest migration. Do not read it as
extraction quality.

## When something goes wrong

### Jobs are queued but nothing runs

The in-process sweeper is wedged, or a deploy stranded a batch.

```powershell
curl -X POST "https://<agent>/api/v1/internal/agent/jobs/run-due?limit=50" -H "X-Service-Token: $TOKEN"
```

Drains synchronously and reports what it did. Scholarship Finder needed an
equivalent even with scheduled delivery; this service has no external queue
at all, so this is the escape hatch.

### A stale instance is running jobs

`scripts/batch.py` writes job rows straight to the database rather than
going through the API, so **any** agent process pointed at that database
will claim them through its sweeper - including one left over from a
previous deploy running older code.

Concurrent instances are safe by design (`FOR UPDATE SKIP LOCKED`), but an
instance running *old* code is not: it will happily run a job with a
workflow version it does not implement. The symptom is an error that names
a capability the current build has, such as
`UnknownUseCase: no workflow registered for 'scholarship_verification'`.

Confirm only the intended process is running before reading any batch
result:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*edufurther*agent*' } |
  Select-Object ProcessId, CommandLine
```

On Railway this is handled by the deploy replacing the old container. It
bites locally, where `fastapi dev` leaves an orphaned reloader child if the
parent is killed without its process tree.

### A job is stuck in `running`

Its worker died. The sweeper reclaims it once the lease expires
(`JOB_LEASE_SECONDS`, default 900) and it resumes from its last checkpoint
rather than starting over. If it has not moved after twice the lease, check
`/ready` — the checkpointer needs the database.

### `PoolTimeout`, or "the database is slow"

Check `DB_POOL_SIZE` and `DB_POOL_CONNECT_TIMEOUT_SECONDS` before assuming
an outage. Concurrent workflows each open several short sessions, and an
undersized pool presents as callers timing out. `DB_CONNECT_TIMEOUT_SECONDS`
bounds the readiness probe only — deliberately separate, because one value
for both makes a burst look like an outage.

### A tool is misbehaving

Per-tool kill switches, no deploy needed, effective on the next call:

```
DISABLED_TOOLS=["jina"]
```

Known tools: `direct_fetch`, `jina`, `tavily`, `parsebot`. A name that
matches none of them is reported by `registry.unknown_disabled_tools()` —
worth checking, because a misspelled switch reads as "off" to whoever set it
while the tool carries on running.

Disabling `jina` and `direct_fetch` together stops retrieval entirely and
jobs will fail rather than silently return nothing.

### Provider budget exhausted

`agent_research_usage` counts per provider per calendar month, reserved
before each call. Exhaustion is not an error: retrieval falls back or the
candidate comes out `MORE_EVIDENCE_REQUIRED`.

Note both services count separately while both hold a key for the same
provider — the real quota is the sum. Scholarship Finder's `JINA_API_KEY`
is unset at cutover for exactly this reason.

## Rollback

In order, least disruptive first. Each step is independently sufficient for
the problem above it.

**1. Stop new work.** Stop running `scripts/batch.py`. In-flight jobs finish;
nothing new is submitted.

**2. Return to shadow mode.** Set `SHADOW_MODE=true` and redeploy. The
service keeps processing and recording, and stops creating candidates and
review tasks. Records already written stay — they are additive and nothing
in the product's pipeline reads them.

**3. Disconnect from the product.** Unset `SCHOLARSHIP_FINDER_BASE_URL`.
Workflows still run and record locally; submission is skipped with a reason,
not an error.

**4. Stop the service.** Scale the Railway service to zero. Jobs stay
`queued` or `running` with expired leases and resume when it returns.

**Do not** delete `agent_runs` or `discovery_evidence` rows to "clean up".
They are the audit trail for what was processed, and the pilot report is
computed from them.

If publication risk is ever suspected — it should not be reachable, since
this service cannot publish — the product-side control is
`AUTO_APPROVE_ENABLED=false` in Scholarship Finder, which is already its
default.

## What to preserve when reporting a failure

Everything needed is already durable. Include:

* the `job_id` and its `correlation_id` — the correlation id is sent to the
  AI Router as `X-Request-ID` and seeds its Langfuse trace, so a run's model
  calls are findable from the job;
* the job's state and `last_error`;
* `agent_errors` and `agent_tool_calls` rows for that job;
* the `workflow_version`.

Do not re-run a failed job to reproduce it before capturing these — a retry
resumes from the checkpoint and may not reach the same state.
