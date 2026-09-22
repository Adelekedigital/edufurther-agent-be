"""Schema drift reporting for /ready.

Ported in spirit from Scholarship Finder. Two deliberate choices carried
over:

* the expected head is resolved from the *working directory*, not from
  `__file__` - the deployment image copies `migrations/` next to the app
  rather than inside the package, so a `__file__`-relative lookup finds
  nothing in exactly the environment that matters;
* every lookup degrades to `None` rather than raising. A packaging problem
  should surface as "unknown" on a readiness probe, never as a crash loop
  that takes the service down over a diagnostic.
"""

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("app.infra.migration_status")


def code_migration_head() -> str | None:
    """The newest revision present in `migrations/versions`.

    Found by elimination: the head is the revision no other revision names
    as its `down_revision`. Cheap, and it needs no Alembic import.
    """
    try:
        versions = Path.cwd() / "migrations" / "versions"
        if not versions.is_dir():
            return None
        revisions: set[str] = set()
        parents: set[str] = set()
        for path in versions.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("revision =") or stripped.startswith("revision:"):
                    value = _literal(stripped)
                    if value:
                        revisions.add(value)
                elif stripped.startswith("down_revision =") or stripped.startswith(
                    "down_revision:"
                ):
                    value = _literal(stripped)
                    if value:
                        parents.add(value)
        heads = revisions - parents
        if len(heads) == 1:
            return heads.pop()
        return None
    except Exception:  # pragma: no cover - diagnostics must never raise
        logger.warning("migration_head_lookup_failed", exc_info=True)
        return None


def _literal(line: str) -> str | None:
    _, _, raw = line.partition("=")
    raw = raw.strip().rstrip(",")
    if raw.startswith(("'", '"')) and raw.endswith(("'", '"')) and len(raw) >= 2:
        return raw[1:-1]
    return None


async def applied_migration_revision(db: AsyncSession) -> str | None:
    try:
        return await db.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
    except Exception:  # pragma: no cover - an unmigrated database is a valid state
        logger.info("alembic_version_unavailable", exc_info=True)
        return None


@dataclass(frozen=True)
class MigrationState:
    applied: str | None
    expected: str | None
    up_to_date: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


async def migration_status(db: AsyncSession) -> MigrationState:
    applied = await applied_migration_revision(db)
    expected = code_migration_head()
    return MigrationState(
        applied=applied,
        expected=expected,
        # Unknown on either side is not "up to date". Reporting drift when
        # we cannot tell is the safe direction: it prompts a look.
        up_to_date=bool(applied and expected and applied == expected),
    )
