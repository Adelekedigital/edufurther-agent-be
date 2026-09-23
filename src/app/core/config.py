import logging
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("app.core.config")

DEPLOYED_ENVIRONMENTS = frozenset({"staging", "production"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Application -------------------------------------------------
    app_name: str = "edufurther-agent"
    app_version: str = "0.1.0"
    environment: str = "development"
    #: Stamped on every job, output and evidence record, and sent to the
    #: product with each run. Bumping it is what makes a deliberate
    #: reprocess of an already-processed discovery possible without
    #: creating a duplicate - see the agent_runs uniqueness constraint.
    workflow_version: str = "scholarship-verification-v1"

    # --- Database ----------------------------------------------------
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/edufurther_agent"
    #: Bounds the readiness probe only, so an unreachable database fails
    #: /ready promptly rather than hanging it. Deliberately not the pool's
    #: connect timeout: those are different jobs, and using one value for
    #: both makes a burst of concurrent workflows look like an outage.
    db_connect_timeout_seconds: float = 3.0
    #: Bounds one pool connection attempt. More generous, because opening
    #: several at once under load is normal and slow is not broken.
    db_pool_connect_timeout_seconds: float = 15.0
    #: Explicit rather than SQLAlchemy's defaults. Workflows run
    #: concurrently and each opens several short sessions, so an
    #: under-sized pool shows up as callers timing out waiting for a
    #: connection - which looks like the database being slow rather than
    #: the pool being small.
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # --- Inbound authentication --------------------------------------
    #: What this service accepts on its own internal surface, matching
    #: Scholarship Finder's setting of the same name. The convention across
    #: the ecosystem is that INTERNAL_SERVICE_TOKEN always names the
    #: *acceptor's* own credential, so it means the same thing everywhere
    #: even though the value differs per service - exactly like DATABASE_URL.
    #:
    #: Deliberately not AGENT_SERVICE_TOKEN. That name inverts roles
    #: depending on where it is read: on Finder it names the *caller* (what
    #: Finder accepts from this service), so using it here for the
    #: *acceptor* would give one name two meanings across two deployments.
    #: Swapping the two is invisible when it happens - both values are
    #: valid tokens, so the mistake surfaces as an ordinary 401 rather than
    #: as a typo.
    #:
    #: The outbound counterpart is `scholarship_finder_agent_token`.
    #:
    #: Fails closed when unset: no token configured means no caller can
    #: reach an authenticated route at all.
    internal_service_token: str | None = None

    # --- AI Router (outbound) ----------------------------------------
    ai_router_base_url: str | None = None
    ai_router_private_key_pem: str | None = None
    #: The registered `kid`. Bump on every key rotation.
    ai_router_key_id: str | None = None
    #: Must match the key this service is registered under in the router's
    #: SERVICE_CALLERS, and the product named in each task's allowed_products.
    ai_router_product_id: str = "edufurther_agent"
    ai_router_timeout_seconds: float = 45.0

    # --- Scholarship Finder (outbound) -------------------------------
    scholarship_finder_base_url: str | None = None
    scholarship_finder_agent_token: str | None = None
    scholarship_finder_timeout_seconds: float = 30.0

    #: Shadow mode writes evidence and run records but never creates or
    #: updates a review task. Stage 1 of the rollout runs this way, so the
    #: default is the safe one: an unconfigured environment cannot affect
    #: the product's review queue.
    shadow_mode: bool = True

    # --- Jobs --------------------------------------------------------
    #: 900s matches Scholarship Finder's lease, for the same reason: it is
    #: the ceiling on how long a single run may hold a claim before the
    #: sweeper is entitled to assume the worker died.
    job_lease_seconds: int = 900
    job_max_attempts: int = 5
    job_sweep_interval_seconds: int = 60
    job_sweep_batch_limit: int = 20
    max_workflow_duration_seconds: int = 1800

    # --- Tools -------------------------------------------------------
    #: Per-tool kill switches. A tool returning garbage, or a provider
    #: billing unexpectedly, has to be stoppable by configuration alone -
    #: without a deploy, and without taking the other tools down with it.
    disabled_tools: set[str] = Field(default_factory=set)

    jina_api_key: str | None = None
    jina_monthly_call_limit: int = 500
    jina_timeout_seconds: float = 30.0
    fetch_timeout_seconds: float = 15.0
    fetch_connect_timeout_seconds: float = 5.0
    fetch_max_bytes: int = 2_000_000
    # Registered but unimplemented until harvesting moves off Scholarship
    # Finder; the timeouts exist so the policy is already expressible.
    tavily_timeout_seconds: float = 30.0
    parsebot_timeout_seconds: float = 60.0

    # --- Observability -----------------------------------------------
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.0
    sentry_enabled: bool | None = None

    @model_validator(mode="after")
    def require_deployed_secrets(self) -> "Settings":
        """Refuse to boot a deployed environment with no inbound token.

        Unset means every authenticated route 401s, which looks like a
        routing or client bug rather than missing configuration. Failing at
        boot puts the error where someone will read it.
        """
        if self.environment in DEPLOYED_ENVIRONMENTS and not self.internal_service_token:
            raise ValueError("INTERNAL_SERVICE_TOKEN is required in staging and production")
        return self

    @property
    def is_deployed(self) -> bool:
        return self.environment in DEPLOYED_ENVIRONMENTS

    @property
    def sentry_active(self) -> bool:
        if not self.sentry_dsn:
            return False
        return self.is_deployed if self.sentry_enabled is None else self.sentry_enabled

    @property
    def ai_router_configured(self) -> bool:
        return bool(
            self.ai_router_base_url and self.ai_router_private_key_pem and self.ai_router_key_id
        )

    @property
    def scholarship_finder_configured(self) -> bool:
        return bool(self.scholarship_finder_base_url and self.scholarship_finder_agent_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
