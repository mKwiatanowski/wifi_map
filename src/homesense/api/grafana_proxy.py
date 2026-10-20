"""Reverse proxy /grafana/* -> Grafana (listening on 127.0.0.1 only), behind the HomeSense login.

CSRF: Grafana's own frontend cannot send the HomeSense CSRF header, so state-changing requests
(anything but GET/HEAD/OPTIONS) must come from the HomeSense origin (Origin, or Referer when Origin
is absent) and may only hit the few endpoints a read-only anonymous Viewer dashboard needs:
  api/ds/query            panel data queries (POST)
  api/frontend-metrics    frontend performance metrics (POST)
  api/frontend/log        frontend error logging (POST)
  api/live/...            Grafana Live (publish / streaming endpoints)
Everything else (saving dashboards, admin API, ...) is 403.

Paths: matching and forwarding use one canonical path built from the raw request path (decoded once),
so Grafana can never see a different path than the one checked here. Encoded separators (%2F, %5C,
%2E), anything still containing '%' after decoding, backslashes, '.'/'..' or empty segments,
whitespace and control characters are rejected with 400, for every method.
"""

# No `from __future__ import annotations`: FastAPI inspects these signatures at runtime.

import logging
import re
from typing import Annotated
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from homesense.api.deps import CSRF_HEADER, get_core, require_user
from homesense.app import HomeSenseApp

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_user)])

METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MUTATING_PATHS = frozenset({"api/ds/query", "api/frontend-metrics", "api/frontend/log"})
LIVE_PREFIX = "api/live/"
PROXY_PREFIX = "/grafana/"
_LIVE_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_ENCODED_SEPARATOR = re.compile(rb"%(2[fFeE]|5[cC])")
_FORBIDDEN_CHAR = re.compile(r"[%\\\x00-\x20\x7f]")
# Characters left unescaped when re-encoding the canonical path for the upstream URL.
_PATH_SAFE = "/:@!$&'()*+,;="
UPSTREAM_TIMEOUT = httpx.Timeout(60.0)
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
# Never forwarded to Grafana: the HomeSense session cookie and CSRF token stay with HomeSense;
# Origin/Referer are checked here and then dropped (Grafana's own origin check would see a mismatch).
DROP_REQUEST = HOP_BY_HOP | {"host", "cookie", "content-length", "origin", "referer", CSRF_HEADER.lower()}
# Grafana must not set cookies on the HomeSense origin (it could clobber the session cookie).
DROP_RESPONSE = HOP_BY_HOP | {"set-cookie"}

UNAVAILABLE_PAGE = """<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><title>Grafana niedostępna</title></head>
<body><h1>Grafana niedostępna</h1>
<p>Nie udało się połączyć z Grafaną. Sprawdź, czy usługa Grafana działa, i odśwież stronę.</p>
</body></html>"""


def _filter_headers(headers: list[tuple[str, str]], drop: frozenset[str]) -> list[tuple[str, str]]:
    """Drops `drop` plus any header named in the Connection header (RFC 9110 7.6.1)."""
    named = {
        token.strip().lower()
        for name, value in headers
        if name.lower() == "connection"
        for token in value.split(",")
    }
    return [(k, v) for k, v in headers if k.lower() not in drop and k.lower() not in named]


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def canonical_path(raw_path: bytes) -> str | None:
    """The path below /grafana/ ("" for the root), or None when it is not in canonical form."""
    if _ENCODED_SEPARATOR.search(raw_path):
        return None
    try:
        path = unquote(raw_path.decode("ascii"), errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    if not path.startswith(PROXY_PREFIX):
        return None
    rest = path[len(PROXY_PREFIX) :]
    if rest == "":
        return rest
    if _FORBIDDEN_CHAR.search(rest) or any(s in ("", ".", "..") for s in rest.split("/")):
        return None
    return rest


def _mutation_allowed(path: str) -> bool:
    """Exact, case-sensitive match; only api/live/ is a prefix, with strictly named segments."""
    if path in MUTATING_PATHS:
        return True
    if path.startswith(LIVE_PREFIX):
        return all(_LIVE_SEGMENT.fullmatch(s) for s in path[len(LIVE_PREFIX) :].split("/"))
    return False


def _check_mutation(request: Request, path: str) -> None:
    """403 unless a state-changing request is same-origin and targets an allowlisted endpoint."""
    if request.method in SAFE_METHODS:
        return
    source = request.headers.get("origin") or request.headers.get("referer")
    own = f"{request.url.scheme}://{request.url.netloc}".lower()
    if source is None or _origin_of(source) != own:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Żądanie spoza panelu HomeSense")
    if not _mutation_allowed(path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Ta operacja w Grafanie jest niedozwolona")


@router.api_route("/grafana/{path:path}", methods=METHODS)
async def grafana(request: Request, core: Annotated[HomeSenseApp, Depends(get_core)]) -> Response:
    # The decoded route parameter is ignored on purpose: only the canonical raw path is trusted.
    raw_path = request.scope.get("raw_path") or quote(request.scope["path"]).encode("ascii")
    path = canonical_path(raw_path)
    if path is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nieprawidłowa ścieżka")
    _check_mutation(request, path)
    url = f"{core.settings.grafana_url.rstrip('/')}/grafana/{quote(path, safe=_PATH_SAFE)}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = _filter_headers(
        [(k.decode("latin-1"), v.decode("latin-1")) for k, v in request.headers.raw], DROP_REQUEST
    )
    http = core.http
    upstream_request = http.build_request(
        request.method, url, headers=headers, content=await request.body(), timeout=UPSTREAM_TIMEOUT
    )
    try:
        upstream = await http.send(upstream_request, stream=True)
    except httpx.TransportError as exc:
        log.warning("grafana proxy %s %s failed: %s", request.method, path, type(exc).__name__)
        return HTMLResponse(UNAVAILABLE_PAGE, status_code=502)
    response = StreamingResponse(
        upstream.aiter_raw(), status_code=upstream.status_code, background=BackgroundTask(upstream.aclose)
    )
    # Raw bytes are passed through unchanged (incl. Content-Encoding/Length); repeated headers are kept.
    response.raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in _filter_headers(upstream.headers.multi_items(), DROP_RESPONSE)
    ]
    return response
