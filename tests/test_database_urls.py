"""URL translation for the two drivers that share one DATABASE_URL.

Our tables use SQLAlchemy with asyncpg; LangGraph's checkpointer uses
psycopg3. They disagree about how to spell TLS, and a managed provider hands
out a URL that suits neither exactly. Getting either translation wrong fails
at connect time in a deployed environment with an error that points at the
driver rather than at the URL, so both directions are pinned here.
"""

import pytest

from app.infra.database import normalize_database_url
from app.runtime.checkpoints import psycopg_dsn


@pytest.mark.parametrize(
    "given",
    [
        "postgres://u:p@host:5432/db",
        "postgresql://u:p@host:5432/db",
        "postgresql+psycopg2://u:p@host:5432/db",
        "postgresql+asyncpg://u:p@host:5432/db",
    ],
)
def test_any_provider_spelling_becomes_the_async_driver(given):
    """Railway and Neon hand out `postgres://` and `postgresql://`. Pasting
    the dashboard value straight into the environment has to work."""
    assert normalize_database_url(given).startswith("postgresql+asyncpg://")


@pytest.mark.parametrize(
    "mode", ["require", "disable", "prefer", "allow", "verify-ca", "verify-full"]
)
def test_sslmode_is_renamed_for_asyncpg_but_the_mode_is_preserved(mode):
    """asyncpg rejects the *name* `sslmode`, not its vocabulary.

    Its `ssl` argument parses the string through asyncpg's own SSLMode
    enum, so the value has to stay a libpq mode. This previously became
    `ssl=true`, which failed at connect time against every managed
    provider with "`sslmode` parameter must be one of: ..." - an error
    naming a parameter the rewritten URL no longer contained.
    """
    result = normalize_database_url(f"postgresql://u:p@host/db?sslmode={mode}")

    assert "sslmode" not in result
    assert f"ssl={mode}" in result


def test_every_asyncpg_ssl_value_we_emit_is_one_asyncpg_accepts():
    """The regression guard. The bug was not that the value looked wrong -
    it was that nothing checked it against asyncpg's own parser."""
    from asyncpg.connect_utils import SSLMode

    for mode in ("require", "disable", "prefer", "allow", "verify-ca", "verify-full"):
        emitted = normalize_database_url(f"postgresql://u:p@host/db?sslmode={mode}")
        value = emitted.split("ssl=")[1].split("&")[0]
        # Raises for anything asyncpg would reject at connect time.
        SSLMode.parse(value)


def test_a_non_tls_sslmode_is_not_silently_upgraded():
    result = normalize_database_url("postgresql://u:p@host/db?sslmode=disable")

    assert "ssl=disable" in result


def test_channel_binding_is_dropped_for_asyncpg():
    """Neon includes it; asyncpg does not know it and refuses to connect."""
    result = normalize_database_url(
        "postgresql://u:p@host/db?sslmode=require&channel_binding=require"
    )

    assert "channel_binding" not in result


def test_psycopg_dsn_strips_the_sqlalchemy_driver():
    assert psycopg_dsn("postgresql+asyncpg://u:p@host:5432/db") == ("postgresql://u:p@host:5432/db")


@pytest.mark.parametrize("mode", ["require", "disable", "verify-ca", "verify-full"])
def test_psycopg_dsn_translates_ssl_back_to_sslmode(mode):
    """The reverse of the asyncpg rewrite: psycopg speaks libpq, so the
    same configured URL has to arrive spelled its way - with the mode
    intact. Collapsing unrecognised modes to `disable`, as this once did,
    turns verify-full into no TLS at all."""
    result = psycopg_dsn(f"postgresql+asyncpg://u:p@host/db?ssl={mode}")

    assert f"sslmode={mode}" in result
    assert "ssl=" not in result.replace("sslmode=", "")


def test_psycopg_dsn_round_trips_a_provider_url():
    """The real path: one value in the environment, through both
    translations, still pointing at the same database with TLS intact."""
    original = "postgresql://u:p@host:5432/db?sslmode=verify-full&channel_binding=require"

    dsn = psycopg_dsn(normalize_database_url(original))

    assert dsn.startswith("postgresql://u:p@host:5432/db")
    # verify-full, not downgraded to require along the way.
    assert "sslmode=verify-full" in dsn
    assert "+asyncpg" not in dsn


@pytest.mark.parametrize("converter", [normalize_database_url, psycopg_dsn])
def test_an_empty_url_is_passed_through_rather_than_mangled(converter):
    assert converter("") == ""


@pytest.mark.parametrize("converter", [normalize_database_url, psycopg_dsn])
def test_a_url_without_a_query_is_left_alone(converter):
    assert "?" not in converter("postgresql+asyncpg://u:p@host:5432/db")
