import hmac

from fastapi import Header, HTTPException

from app.core.config import get_settings


async def require_agent_service(x_service_token: str | None = Header(default=None)) -> None:
    """Authenticate an internal caller by shared secret.

    Constant-time compare: a short-circuiting comparison leaks the token
    byte by byte to anyone close enough to measure, and the platform proxy
    is that close. Same reasoning, and same shape, as Scholarship Finder's
    `require_internal_service`.

    Fails closed when no token is configured - an unset secret must not mean
    an open service. `Settings.require_deployed_secrets` turns that into a
    boot failure in staging and production, where silence would be worse.
    """
    expected = get_settings().internal_service_token
    # Compared as bytes. Starlette decodes headers as latin-1, so a byte
    # >= 0x80 yields a non-ASCII str and `hmac.compare_digest` raises
    # TypeError on it - an unhandled 500, from an unauthenticated request,
    # on every route behind this dependency.
    supplied = (x_service_token or "").encode("utf-8", "surrogateescape")
    if not expected or not hmac.compare_digest(supplied, expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Internal service authentication required")
