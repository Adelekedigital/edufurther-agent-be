# Testing the Agent against Scholarship Finder and the AI Router

An end-to-end run across all three services. Each step has a verification
gate — do not move on until it passes, because a failure three steps later
is much harder to attribute.

Ports used throughout: **AI Router 8096**, **Scholarship Finder 8097**,
**Agent 8095**.

---

## Read this first — four ways to waste an afternoon

**Check the port is actually yours before trusting any result.** This is
the one that cost the most time. On Windows a second bind to a port that an
orphaned server still holds *silently succeeds*: your new server logs
`Uvicorn running on http://127.0.0.1:8097` and looks healthy, while every
request is answered by the old process running older code. A run of this
guide hit exactly that — Scholarship Finder returned `200` where the current
code returns `422`, and the endpoint looked broken when the real cause was a
server from a previous session.

`fastapi dev` reloads, so it runs a worker as a child. Kill the parent
without its process tree and that child survives, holding the socket. Its
command line is a bare `python.exe -c "from multiprocessing.spawn import
spawn_main; ..."` with no repo path in it, so **grepping process command
lines for the project name will not find it.** Match on the port instead,
and compare the owning PID against the process you started:

```powershell
foreach ($port in 8095,8096,8097) {
  $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  Write-Output ("port {0}: {1}" -f $port,
    $(if ($c) { "PID " + (($c.OwningProcess | Sort-Object -Unique) -join ',') } else { "clear" }))
}
```

Every port you intend to use must read `clear` *before* you start anything.
Windows may attribute the socket to an already-dead parent PID, so if a port
is held and `Stop-Process` reports the PID does not exist, list the orphans
by their spawn signature and kill those:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*multiprocessing.spawn*' } |
  Select-Object ProcessId, ParentProcessId, CreationDate
```

Anything whose `ParentProcessId` no longer exists is an orphan. Check the
`CreationDate` against when you started your servers before killing it.

**Only ever run one Agent instance against a database.** `scripts/batch.py`
writes job rows *directly*, so any Agent process pointed at that database
claims them through its sweeper — including one left over from an earlier
run with older code. The symptom is an error naming a capability the
current build has, e.g. `UnknownUseCase: ... known: ('probe',)`.

**Only ever run one test suite against a database.** The suites TRUNCATE
shared tables in an autouse fixture. Two concurrent runs deadlock on
relation locks and sit there indefinitely. A stuck run looks exactly like a
slow one — the tell is CPU time that is non-zero but *not increasing*.
Different databases are fine: the Agent's suite and Finder's can run at the
same time, because they use separate databases on separate containers.

**On Windows use `fastapi dev`, never `fastapi run`.** The LangGraph
checkpointer runs on psycopg, which cannot use Windows' `ProactorEventLoop`;
uvicorn picks that loop unless it is running subprocesses, and `dev`
reloads so it does. Also set `PYTHONUTF8=1` or the CLI banner crashes on
cp1252. Neither affects Linux deployment.

---

## Step 0 — only if you just renamed or moved the checkout

Two things break on a rename, both silently, and both look like something
else went wrong.

**The virtualenv holds absolute paths.** `uv run` fails with
`Failed to canonicalize script path`. Rebuild it:

```powershell
Remove-Item -Recurse -Force .venv
uv sync
```

**Compose derives its project name from the directory**, so a renamed
folder points at a brand-new, empty volume and the database looks wiped.
`docker-compose.yml` now pins `name: edufurther-agent-be` to stop that, but
data written under the old name still lives in the old volume. List them
with `docker volume ls`; the Agent's database is disposable runtime state,
so the simplest fix is to let the new volume be created and re-run the
migrations below. Delete the stale volume once you are sure:

```powershell
docker volume rm edufurtherag-be_agent_postgres_data
```

Scholarship Finder's checkout was not renamed, so its volume and its data
are unaffected.

---

## Step 1 — databases up and migrated

```powershell
cd C:\pythonwork\edufurther-agent-be
docker compose up -d postgres
uv sync
$env:PYTHONUTF8=1
$env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:55434/edufurther_agent"
uv run alembic upgrade head

cd C:\pythonwork\edufurtherSF-BE
docker compose up -d postgres
$env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:55433/scholarship_finder"
uv run alembic upgrade head
```

The AI Router needs no database: `DATABASE_URL=` empty selects its
in-memory store.

**Gate:** both `alembic upgrade head` commands exit 0. Agent head is
`0001_agent_runtime_state`, Finder head is `0030_agent_integration`.

---

## Step 2 — register the Agent with the Router (one-time)

Key material was generated into `.local-secrets/` (gitignored) by
`edufurtherai-be/scripts/generate_service_keys.py`. The Agent keeps the
private key; the Router gets only the public half.

Edit `C:\pythonwork\edufurtherai-be\.env`:

```env
# Paste the whole line from .local-secrets/router-SERVICE_CALLERS.env.
# It is pre-merged and keeps scholarship_finder registered - SERVICE_CALLERS
# is one blob covering every caller, so replacing it rather than merging
# silently de-registers the live product's AI extraction.
SERVICE_CALLERS=<paste>

# Your provider key. Without this no model call can succeed.
PROVIDER_KEYS={"openai":"sk-..."}

# The five agent tasks have no ROUTING_POLICY entry, so they fall back to
# PRIMARY_MODEL. It ships unset, and an unset primary with an unset
# fallback means no model is tried at all - you get provider_unavailable
# with nothing in the logs pointing at configuration.
PRIMARY_MODEL=openai/gpt-4o-mini
FALLBACK_MODEL=openai/gpt-4o-mini

# Bound the spend while testing.
PRODUCT_TASK_BUDGETS={"edufurther_agent:split_list_candidates":2.0,"edufurther_agent:extract_scholarship_facts":2.0,"edufurther_agent:compare_official_evidence":2.0,"edufurther_agent:extract_eligibility_requirements":2.0,"edufurther_agent:classify_source_page":1.0}
```

**Gate:**

```powershell
cd C:\pythonwork\edufurtherai-be
uv run python -c "from app.core.config import settings; print(sorted(settings.service_callers))"
```

Must print `['edufurther_agent', 'scholarship_finder']`. If
`scholarship_finder` is missing you have replaced rather than merged — fix
it before going further.

---

## Step 3 — the three `.env` files

**`edufurtherSF-BE\.env`**

```env
ENVIRONMENT=development
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55433/scholarship_finder
INTERNAL_SERVICE_TOKEN=local-admin-token
AGENT_SERVICE_TOKEN=local-agent-token
CURSOR_SECRET=development-only-change-me
```

The two tokens **must differ** — both arrive in the same `X-Service-Token`
header, so sharing one value silently collapses the privilege separation.
Settings refuses to construct if they match.

**`edufurther-agent-be\.env`**

```env
ENVIRONMENT=development
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55434/edufurther_agent
AGENT_SERVICE_TOKEN=local-dev-token

SCHOLARSHIP_FINDER_BASE_URL=http://127.0.0.1:8097
SCHOLARSHIP_FINDER_AGENT_TOKEN=local-agent-token

AI_ROUTER_BASE_URL=http://127.0.0.1:8096
AI_ROUTER_KEY_ID=edufurther-agent-2026
AI_ROUTER_PRODUCT_ID=edufurther_agent
AI_ROUTER_PRIVATE_KEY_PEM=<one line, from .local-secrets/agent-AI_ROUTER_PRIVATE_KEY_PEM.env>

SHADOW_MODE=true
WORKFLOW_VERSION=scholarship-verification-v1
```

`AGENT_SERVICE_TOKEN` means different things on each side. On Finder it is
the credential it **accepts** from the Agent. On the Agent it is what the
Agent accepts from whoever submits jobs. The Agent's *outbound* credential
is `SCHOLARSHIP_FINDER_AGENT_TOKEN`, and that is the one that must equal
Finder's `AGENT_SERVICE_TOKEN`.

---

## Step 4 — start all three

**First confirm all three ports read `clear`** with the loop at the top of
this page. A port still held by an orphan will accept your new server's bind
and then answer every request from the old one.

Three terminals, each with `$env:PYTHONUTF8=1`.

```powershell
# 1  AI Router
cd C:\pythonwork\edufurtherai-be
uv run fastapi dev main.py --host 127.0.0.1 --port 8096

# 2  Scholarship Finder
cd C:\pythonwork\edufurtherSF-BE
uv run fastapi dev --host 127.0.0.1 --port 8097

# 3  Agent
cd C:\pythonwork\edufurther-agent-be
uv run fastapi dev --host 127.0.0.1 --port 8095
```

**Gate:**

```powershell
curl http://127.0.0.1:8096/health
curl http://127.0.0.1:8097/ready
curl http://127.0.0.1:8095/ready
```

Both `/ready` responses must show `"up_to_date": true`. The Agent's log
should carry `checkpointer_ready` and `sweeper_started` — if instead you
see repeated `ProactorEventLoop` warnings, you started it with
`fastapi run`.

Then confirm each port is held by the process you just started:

```powershell
foreach ($port in 8095,8096,8097) {
  $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  Write-Output ("port {0}: PID {1}" -f $port, (($c.OwningProcess | Sort-Object -Unique) -join ','))
}
```

A PID older than this session means you are talking to an orphan, and every
result below it is about code you are not running.

---

## Step 5 — prove the wiring before spending anything

**5a. Finder's agent API answers the Agent's credential:**

```powershell
curl "http://127.0.0.1:8097/api/v1/internal/agent/discoveries?limit=3&workflow_version=scholarship-verification-v1" `
  -H "X-Service-Token: local-agent-token"
```

Expect a JSON list. A 401 means the tokens do not match; a 422 means you
omitted `workflow_version` while `unprocessed_only` defaulted true.

**5b. Privilege separation holds** — this must be **401**:

```powershell
curl -i "http://127.0.0.1:8097/api/v1/internal/admin/reviews" `
  -H "X-Service-Token: local-agent-token"
```

**5c. The Agent can reach the Router.** One cheap real model call:

```powershell
cd C:\pythonwork\edufurther-agent-be
uv run python -c @'
import asyncio, sys; sys.path.insert(0, "src")
from app.core.eventloop import install_selector_event_loop_policy
install_selector_event_loop_policy()
from app.integrations.ai_router import client_from_settings, AIRouterRequest, AITask
async def main():
    r = await client_from_settings().execute(AIRouterRequest(
        task=AITask.classify_source_page, feature_id="smoke",
        correlation_id="agent_smoke_1", idempotency_key="smoke-1",
        source_data={"title": "Test", "excerpt": "", "page_text": "One scholarship, GBP 10,000."}))
    print(r.outcome.value, "| model:", r.model_policy_version, "| prompt:", r.prompt_version)
asyncio.run(main())
'@
```

Expect `completed`. A generic 401 means the caller registration is wrong —
check `kid`, `issuer`, `subject`, `audience`; the Router deliberately gives
one indistinguishable error for all of them.

**5d. Dry run — lists work, writes nothing:**

```powershell
uv run python scripts/batch.py --limit 5 --dry-run
```

---

## Step 6 — run a real batch

Check no stray Agent process is running (see the warning at the top), then:

```powershell
uv run python scripts/batch.py --limit 3
```

Start small. Three of the five tasks run **per candidate**, so a ten-item
list page is roughly 1 classify + 1 split + 10 extract + 10 compare + 10
eligibility ≈ 32 model calls. Prefer individual pages for the first run.

Watch:

```powershell
curl "http://127.0.0.1:8095/api/v1/internal/agent/metrics?workflow_version=scholarship-verification-v1" `
  -H "X-Service-Token: local-dev-token"
```

---

## Step 7 — verify what actually happened

**Agent side:**

```powershell
cd C:\pythonwork\edufurther-agent-be
docker compose exec postgres psql -U postgres -d edufurther_agent -c `
  "SELECT state, count(*) FROM agent_jobs GROUP BY state"
docker compose exec postgres psql -U postgres -d edufurther_agent -c `
  "SELECT tool, status, duration_ms, response_meta->>'fetch_method' AS method FROM agent_tool_calls ORDER BY created_at DESC LIMIT 10"
docker compose exec postgres psql -U postgres -d edufurther_agent -c `
  "SELECT node, payload->>'outcome' AS outcome, payload->'notes' FROM agent_outputs ORDER BY created_at DESC LIMIT 5"
```

**Finder side — the run and its evidence:**

```powershell
cd C:\pythonwork\edufurtherSF-BE
docker compose exec postgres psql -U postgres -d scholarship_finder -c `
  "SELECT workflow_version, agent_outcome, created_at FROM agent_runs ORDER BY created_at DESC LIMIT 5"
docker compose exec postgres psql -U postgres -d scholarship_finder -c `
  "SELECT claim_path, source_type, prompt_version, model FROM discovery_evidence ORDER BY created_at DESC LIMIT 5"
```

**Shadow mode held — all four must be zero:**

```powershell
docker compose exec postgres psql -U postgres -d scholarship_finder -c `
  "SELECT
     (SELECT count(*) FROM discoveries WHERE split_from_discovery_id IS NOT NULL) AS split_created,
     (SELECT count(*) FROM scholarships WHERE created_at > now() - interval '1 hour') AS scholarships,
     (SELECT count(*) FROM audit_log WHERE action LIKE 'scholarship.%' AND created_at > now() - interval '1 hour') AS publications,
     (SELECT count(*) FROM review_tasks WHERE reason LIKE 'agent_%' AND created_at > now() - interval '1 hour') AS agent_tasks"
```

---

## Reading the result

`MORE_EVIDENCE_REQUIRED` will dominate, and that is **correct behaviour,
not a quality problem**. `find_official_source` has no search tool in this
build: it accepts a candidate link that leaves the source's domain, or one
that stays on it when the source is graded A or B. An aggregator linking to
its own page is not corroboration. Candidates on grade C/D sources without
an outbound link therefore come out `MORE_EVIDENCE_REQUIRED`, which caps
the official-source discovery rate until Tavily arrives.

In `/metrics`, `attempts_per_job` above 1.0 means work is being redone —
worth investigating before widening the batch, whatever the outcome counts
say.

---

## Going live (turning shadow mode off)

Only after a shadow batch looks right. Set `SHADOW_MODE=false` and restart
the Agent. It will then create real `Discovery` rows for split candidates
and open review tasks. Re-run the shadow-mode query above and expect
non-zero `split_created`; `scholarships` and `publications` must **still**
be zero — the Agent has no route to either, in any mode.

Rollback is `SHADOW_MODE=true` and restart; see `operations-runbook.md`.

---

## Teardown

```powershell
# Stop the three servers (Ctrl-C each), then confirm the ports are free.
# Match on the port, not the command line: a reload worker orphaned by
# Ctrl-C shows up as a bare multiprocessing.spawn process with no repo
# path in it, so a name filter misses exactly the thing you are checking for.
foreach ($port in 8095,8096,8097) {
  $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  Write-Output ("port {0}: {1}" -f $port,
    $(if ($c) { "STILL HELD by PID " + (($c.OwningProcess | Sort-Object -Unique) -join ',') } else { "clear" }))
}

# The databases can stay up, and the .env files can stay put: the Agent's
# conftest now neutralises the env file for the suite, so a local .env no
# longer leaks into test runs.
```
