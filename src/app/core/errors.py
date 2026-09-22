from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

PROBLEM_MEDIA_TYPE = "application/problem+json"


def problem(
    request: Request,
    status: int,
    title: str,
    code: str,
    detail: str,
    *,
    retryable: bool = False,
) -> JSONResponse:
    """RFC 7807 problem response, matching both sibling services.

    `code` is the stable, machine-readable discriminator; `title` and
    `detail` are for a human reading a log. `retryable` tells a caller
    whether backing off is worth anything, so it does not have to infer
    that from the status code.
    """
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        "retryable": retryable,
    }
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_MEDIA_TYPE)
