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


def test_sslmode_is_translated_for_asyncpg():
    """asyncpg rejects libpq's `sslmode` outright."""
    result = normalize_database_url("postgresql://u:p@host/db?sslmode=require")

    assert "sslmode" not in result
    assert "ssl=true" in result


def test_a_non_tls_sslmode_is_not_silently_upgraded():
    result = normalize_database_url("postgresql://u:p@host/db?sslmode=disable")

    assert "ssl=false" in result


def test_channel_binding_is_dropped_for_asyncpg():
    """Neon includes it; asyncpg does not know it and refuses to connect."""
    result = normalize_database_url(
        "postgresql://u:p@host/db?sslmode=require&channel_binding=require"
    )

    assert "channel_binding" not in result


def test_psycopg_dsn_strips_the_sqlalchemy_driver():
    assert psycopg_dsn("postgresql+asyncpg://u:p@host:5432/db") == ("postgresql://u:p@host:5432/db")


def test_psycopg_dsn_translates_ssl_back_to_sslmode():
    """The reverse of the asyncpg rewrite: psycopg speaks libpq, so the
    same configured URL has to arrive spelled its way."""
    result = psycopg_dsn("postgresql+asyncpg://u:p@host/db?ssl=true")

    assert "sslmode=require" in result
    assert "ssl=true" not in result


def test_psycopg_dsn_round_trips_a_provider_url():
    """The real path: one value in the environment, through both
    translations, still pointing at the same database with TLS intact."""
    original = "postgresql://u:p@host:5432/db?sslmode=require&channel_binding=require"

    dsn = psycopg_dsn(normalize_database_url(original))

    assert dsn.startswith("postgresql://u:p@host:5432/db")
    assert "sslmode=require" in dsn
    assert "+asyncpg" not in dsn


@pytest.mark.parametrize("converter", [normalize_database_url, psycopg_dsn])
def test_an_empty_url_is_passed_through_rather_than_mangled(converter):
    assert converter("") == ""


@pytest.mark.parametrize("converter", [normalize_database_url, psycopg_dsn])
def test_a_url_without_a_query_is_left_alone(converter):
    assert "?" not in converter("postgresql+asyncpg://u:p@host:5432/db")
