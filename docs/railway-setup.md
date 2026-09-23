# Deploying the Agent to Railway

Follow this top to bottom. Each step ends with a check — do not move on
until it passes, because a wrong value here surfaces later as a single
opaque `401` that says nothing about which of five fields was wrong.

---

## The shape of it

Three services, and each one holds a different thing. Most of the confusion
in setup comes from mixing these up.

| Service | Railway name | Holds |
|---|---|---|
| Agent | `edufurther-agent-be` | The **private** key. Its own database. |
| AI Router | `edufurtherai-be` | **Public** keys only, in `SERVICE_CALLERS`. Provider keys. |
| Scholarship Finder | `edufurthersf-be` | The product's data. |

The agent signs a short-lived JWT with its private key; the router verifies
it against the registered public half. **The router never holds a private
key** — that asymmetry is the point, and it means there is no signing
secret on the router to leak.

### The three shared secrets

Only three values cross a service boundary. Everything else is local to one
service.

| Secret | Where it goes | Variable name there |
|---|---|---|
| **A** | Agent | `INTERNAL_SERVICE_TOKEN` |
| **B** | Agent | `SCHOLARSHIP_FINDER_AGENT_TOKEN` |
| **B** | Finder | `AGENT_SERVICE_TOKEN` |
| **C** | Finder | `INTERNAL_SERVICE_TOKEN` (already set) |

**B is the only value that appears twice.** A and B are different secrets,
and both live on the agent.

The agent's inbound token is called `INTERNAL_SERVICE_TOKEN` rather than
`AGENT_SERVICE_TOKEN` precisely so that no name means two different things.
`AGENT_SERVICE_TOKEN` now exists only on Finder, where it means what Finder
**accepts from** the agent.

---

## Step 1 — generate the two tokens

```powershell
python -c "import secrets; print('A =', secrets.token_urlsafe(48))"
python -c "import secrets; print('B =', secrets.token_urlsafe(48))"
```

Keep both somewhere safe for the next three steps.

**Check:** A and B are different strings, and B is different from Finder's
existing `INTERNAL_SERVICE_TOKEN`. Finder refuses to boot if B equals its
admin token, because both arrive in the same header and one shared value
would silently erase the privilege separation.

---

## Step 2 — the keypair

Already generated and verified at:

```
C:\pythonwork\edufurther-agent-be\.local-secrets\staging\
  caller-private.pem    -> the agent
  caller-public.pem     -> the router
```

The directory is gitignored. To make a different pair:

```powershell
cd C:\pythonwork\edufurtherai-be
uv run python scripts/generate_service_keys.py --output-dir C:\pythonwork\edufurther-agent-be\.local-secrets\staging
```

**Check** the two halves belong together — a mismatched pair is the most
tedious failure to diagnose:

```powershell
cd C:\pythonwork\edufurther-agent-be
uv run python -c "import jwt; from pathlib import Path; d=Path('.local-secrets/staging'); t=jwt.encode({'aud':'x'}, (d/'caller-private.pem').read_text(), algorithm='RS256'); jwt.decode(t, (d/'caller-public.pem').read_text(), algorithms=['RS256'], audience='x'); print('pair matches')"
```

---

## Step 3 — the agent's variables

Eight values on `edufurther-agent-be`. The other 28 in `.env.example` have
working defaults and can be left alone.

```
ENVIRONMENT                     staging
DATABASE_URL                    <your Neon URL, pasted verbatim>
INTERNAL_SERVICE_TOKEN           <secret A>
SCHOLARSHIP_FINDER_BASE_URL     https://<finder-host>
SCHOLARSHIP_FINDER_AGENT_TOKEN  <secret B>
AI_ROUTER_BASE_URL              https://<router-host>
AI_ROUTER_KEY_ID                edufurther-agent-staging-2026
AI_ROUTER_PRIVATE_KEY_PEM       <contents of caller-private.pem>
```

Paste the Neon URL exactly as the dashboard gives it, `sslmode` and
`channel_binding` included. The service rewrites it for both drivers.

Set the key from the file so its value is never retyped:

```powershell
railway variables --set "AI_ROUTER_PRIVATE_KEY_PEM=$(Get-Content C:\pythonwork\edufurther-agent-be\.local-secrets\staging\caller-private.pem -Raw)" -s edufurther-agent-be -e staging
```

Real newlines and `\n` escapes both work; the dashboard editor accepts
multi-line values.

Leave `SHADOW_MODE` alone. It defaults to `true`, which is what Stage 1 of
the rollout wants: evidence and run records are written, the product's
review queue is never touched.

**Check:** `ENVIRONMENT=staging` makes `INTERNAL_SERVICE_TOKEN` mandatory — the
service refuses to boot without it rather than 401ing every route and
looking like a routing bug.

---

## Step 4 — the router's `SERVICE_CALLERS`

One variable on `edufurtherai-be`. It is a single JSON object holding
**every** caller, so this is a merge, not a replace.

```json
{"scholarship_finder":{ ...unchanged... },"edufurther_agent":{ ...added... }}
```

Pasting only the agent's entry de-registers `scholarship_finder` and takes
down live AI extraction — silently, until the next call fails.

**Check**, and do not skip this one:

```powershell
railway run -s edufurtherai-be -- python -c "from app.core.config import settings; print(sorted(settings.service_callers))"
```

Must print `['edufurther_agent', 'scholarship_finder']`. If
`scholarship_finder` is missing, restore it immediately.

While you are here, confirm the router can actually reach a model:

* `PROVIDER_KEYS` has a real provider key.
* `PRIMARY_MODEL` is set. The five agent tasks have no `ROUTING_POLICY`
  entry and fall back to it; an unset primary with an unset fallback means
  no model is tried at all, and the error names nothing useful.

---

## Step 5 — Finder

Two variables on `edufurthersf-be`:

```
AGENT_SERVICE_TOKEN   <secret B - the same string as the agent's SCHOLARSHIP_FINDER_AGENT_TOKEN>
JINA_API_KEY          <unset it>
```

Unsetting Jina hands the whole monthly allowance to the agent. While both
services hold a key they count separately against one real quota, so the
effective limit is the sum of two budgets that each think they are alone.

---

## Step 6 — migrate the agent's database

The agent never migrates on boot, so nothing else will create its schema.

```powershell
cd C:\pythonwork\edufurther-agent-be
railway run -s edufurther-agent-be -e staging -- uv run alembic upgrade head
```

**Check:** six tables exist — `agent_jobs`, `agent_job_attempts`,
`agent_tool_calls`, `agent_outputs`, `agent_errors`,
`agent_research_usage`.

Four more will appear on first boot: `checkpoints`, `checkpoint_blobs`,
`checkpoint_writes`, `checkpoint_migrations`. Those are LangGraph's, created
by its own idempotent migrator, which is why they are not in the Alembic
chain.

---

## Step 7 — verify the whole chain

**The service is up and sees its schema:**

```powershell
curl https://<agent-host>/ready
```

Expect `"up_to_date": true`. A `503` means the database is unreachable;
`up_to_date: false` means step 6 did not run.

**Its own auth works:**

```powershell
curl https://<agent-host>/api/v1/internal/agent/metrics -H "X-Service-Token: <secret A>"
```

**It can reach Finder** — submit one `probe` job. It exercises the runtime
end to end without calling a model or touching real discoveries:

```powershell
curl -X POST https://<agent-host>/api/v1/internal/agent/jobs `
  -H "X-Service-Token: <secret A>" -H "Content-Type: application/json" `
  -d '{"product_id":"scholarship_finder","use_case_id":"probe","input_reference":"smoke-1","payload":{}}'
```

Expect `202` with `"state":"queued"`. Poll `GET /jobs/{job_id}` until it
reaches `completed`.

**It can reach the router** — only after that passes, submit one real
`scholarship_verification` job. If it fails at `classify_page`:

* `AIRouterNotConfigured` — one of the three `AI_ROUTER_*` values is missing.
* a generic `401` — the registration is wrong. The router returns one
  indistinguishable error for a bad `kid`, `issuer`, `subject`, `audience`
  or key, so check all five rather than assuming it is the key.

---

## Rollback

`SHADOW_MODE=true` and restart. The agent then writes evidence and run
records but never touches a review task. It has no route to publishing in
any mode.
