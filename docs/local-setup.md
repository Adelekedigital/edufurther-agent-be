# Running the build locally

Three services, three levels of testing. Each level needs strictly more
than the last, so start at the top and stop when you have what you need.

For a single sequenced walkthrough with verification gates at each step,
see [`integration-testing.md`](integration-testing.md). This page is the
reference for what each level requires; that one is the runbook.

Generated key material is in `.local-secrets/` (gitignored). It was created
by `edufurtherai-be/scripts/generate_service_keys.py`.

---

## Level 1 — no credentials at all

Everything except real model calls. This is the whole test suite across all
three services. Each repo's `scripts/check.py` is the gate CI runs, so a
clean run here is the same bar.

```powershell
# Agent
cd C:\pythonwork\edufurther-agent-be
docker compose up -d postgres
docker compose exec postgres psql -U postgres -c "CREATE DATABASE edufurther_agent_test"
$env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:55434/edufurther_agent_test"
uv sync; uv run alembic upgrade head
uv run python scripts/check.py

# Scholarship Finder
cd C:\pythonwork\edufurtherSF-BE
docker compose up -d postgres
$env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:55433/scholarship_finder_test"
uv run alembic upgrade head
uv run python scripts/check.py

# AI Router
cd C:\pythonwork\edufurtherai-be
uv run pytest
```

**On Windows, use `fastapi dev`, not `fastapi run`** — see the README. Also
set `PYTHONUTF8=1`, or the CLI's banner crashes on cp1252.

The Agent and Finder suites can run at the same time — separate databases on
separate containers. Two runs against *one* database deadlock on the autouse
TRUNCATE fixture.

**Do not read the exit code through a pipe.** `check.py 2>&1 | tail -40`
reports `tail`'s status, not the gate's, so a failing gate looks like a pass.
Redirect to a file and check the code directly.

**Finder's `check.py` lints `scripts/`, including untracked files.** On a
working machine that directory holds ad-hoc operational scripts that were
never meant to meet the repo's lint bar, and ruff fails the gate on them
while `src`, `tests` and `migrations` are clean. CI only sees tracked files,
so this is a local-only failure. To confirm the repo itself is clean:

```powershell
uv run ruff check src tests migrations
```

---

## Level 2 — the two services talking, still no model keys

Proves the integration surface: the agent reading discoveries, attaching
evidence, recording runs, and shadow mode holding. This is what was used to
verify PR5 against PR7.

**Scholarship Finder** `.env`:

```env
ENVIRONMENT=development
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55433/scholarship_finder
INTERNAL_SERVICE_TOKEN=local-admin-token
AGENT_SERVICE_TOKEN=local-agent-token
CURSOR_SECRET=development-only-change-me
```

**Agent** `.env`:

```env
ENVIRONMENT=development
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55434/edufurther_agent
INTERNAL_SERVICE_TOKEN=local-dev-token
SCHOLARSHIP_FINDER_BASE_URL=http://127.0.0.1:8097
SCHOLARSHIP_FINDER_AGENT_TOKEN=local-agent-token
SHADOW_MODE=true
WORKFLOW_VERSION=scholarship-verification-v1
```

Three secrets, not two. Scholarship Finder's `AGENT_SERVICE_TOKEN` is what
it *accepts* from the agent, and must equal the agent's outbound
`SCHOLARSHIP_FINDER_AGENT_TOKEN`. The agent's own `INTERNAL_SERVICE_TOKEN`
is a separate value: what the agent accepts from whoever submits jobs to
it. The inbound one is named for its direction rather than for the service
so that no single name means two different things across the two
deployments.

Then:

```powershell
cd C:\pythonwork\edufurtherSF-BE
$env:PYTHONUTF8=1; uv run alembic upgrade head
uv run fastapi dev --host 127.0.0.1 --port 8097

# separate terminal
cd C:\pythonwork\edufurther-agent-be
$env:PYTHONUTF8=1; uv run alembic upgrade head
uv run fastapi dev --host 127.0.0.1 --port 8095

curl http://127.0.0.1:8095/ready          # migration.up_to_date must be true
uv run python scripts/batch.py --limit 5 --dry-run
```

A real batch at this level will fail each job at `classify_page` with
`AIRouterNotConfigured`, classified **permanent**, so jobs land in
`failed_review` with a clear error rather than retrying. That is correct —
but it is why Level 3 exists.

---

## Level 3 — the full workflow, model calls included

**This is the only level that needs something I cannot generate: an LLM
provider API key.** The router's `PROVIDER_KEYS` is `{}` and its
`ROUTING_POLICY` names placeholder models (`openai/model-a`), so no model
call can currently succeed.

**AI Router** `.env` — three changes:

```env
# 1. Register the agent. Use the merged value from
#    .local-secrets/router-SERVICE_CALLERS.env - it keeps scholarship_finder
#    registered. SERVICE_CALLERS is one blob covering every caller, so
#    replacing it rather than merging silently de-registers the product.
SERVICE_CALLERS=<paste from .local-secrets/router-SERVICE_CALLERS.env>

# 2. A real provider key.
PROVIDER_KEYS={"openai":"sk-..."}

# 3. Real models. The five agent tasks have no ROUTING_POLICY entry, so
#    they fall back to PRIMARY_MODEL - which is currently unset, and an
#    unset primary with an unset fallback means no model is tried at all.
PRIMARY_MODEL=openai/gpt-4o-mini
FALLBACK_MODEL=openai/gpt-4o-mini
```

**Agent** `.env` — add:

```env
AI_ROUTER_BASE_URL=http://127.0.0.1:8096
AI_ROUTER_KEY_ID=edufurther-agent-2026
AI_ROUTER_PRODUCT_ID=edufurther_agent
# Single line, newlines escaped. Ready-made in
# .local-secrets/agent-AI_ROUTER_PRIVATE_KEY_PEM.env
AI_ROUTER_PRIVATE_KEY_PEM=-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n

# Optional. Without it there is no Jina fallback, so pages that block a
# direct fetch simply are not retrieved.
JINA_API_KEY=
```

Start the router on 8096, then:

```powershell
uv run python scripts/batch.py --limit 5
curl "http://127.0.0.1:8095/api/v1/internal/agent/metrics" -H "X-Service-Token: local-dev-token"
```

### Cost

Five agent tasks, called per candidate for three of them. A ten-item list
page is roughly 1 classify + 1 split + 10 extract + 10 compare + 10
eligibility ≈ 32 calls. Start with `--limit 5` on individual pages, and set
`PRODUCT_TASK_BUDGETS` in the router to bound it:

```env
PRODUCT_TASK_BUDGETS={"edufurther_agent:split_list_candidates":2.0,"edufurther_agent:extract_scholarship_facts":2.0}
```

---

## Before any deployed pilot

* Register the agent in the router's `SERVICE_CALLERS` **per environment** —
  staging and production are separate registrations.
* Set `AGENT_SERVICE_TOKEN` on Scholarship Finder and the matching
  `SCHOLARSHIP_FINDER_AGENT_TOKEN` on the agent. Not the admin token.
* Run Scholarship Finder's migration `0030_agent_integration` through its
  manual GitHub Actions workflow. Neither service migrates on boot.
* Unset `JINA_API_KEY` on Scholarship Finder so the agent owns the whole
  monthly allowance — while both hold a key, the real quota is the sum.
* Confirm `SHADOW_MODE=true` on the agent and `AUTO_APPROVE_ENABLED=false`
  on Scholarship Finder.
