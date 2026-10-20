"""FastAPI application factory: sessions, static files, templates and all routers."""

from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from homesense.api import auth, grafana_proxy, routes_api, routes_pages, sse
from homesense.app import HomeSenseApp
from homesense.core.config import Settings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SESSION_COOKIE = "homesense_session"
SESSION_MAX_AGE_S = 14 * 24 * 3600

# Pages load scripts, styles and frames only from this origin (the JS libraries are vendored and
# Grafana is proxied under /grafana/). 'unsafe-eval' is required by the standard Alpine.js build,
# which evaluates x-* attribute expressions with new Function(); there is still no 'unsafe-inline',
# so injected inline <script>/<style> cannot run, and htmx's own eval features are switched off
# (htmx-config meta in base.html). Inline style attributes are blocked too: dynamic styling goes
# through classes or CSSOM in app.js.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-eval'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "frame-src 'self'",
        "worker-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'self'",
    ]
)

mimetypes.add_type("application/manifest+json", ".webmanifest")


class SecurityHeadersMiddleware:
    """Adds CSP / anti-framing / nosniff headers to HomeSense's own HTML responses.

    Pure ASGI (no response buffering, so SSE streams are unaffected). /grafana/* is left alone:
    those are Grafana's pages with Grafana's needs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(grafana_proxy.PROXY_PREFIX):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin")]
                content_type = dict(headers).get(b"content-type", b"")
                if content_type.startswith(b"text/html"):
                    extra += [
                        (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")),
                        (b"x-frame-options", b"SAMEORIGIN"),
                    ]
                headers += [(k, v) for k, v in extra if k not in names]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(
    settings: Settings, core: HomeSenseApp | None = None, start_background: bool = True
) -> FastAPI:
    """The web app around `core` (built from `settings` when None).

    The lifespan initializes the core, starts its background jobs when `start_background`, and stops
    it on shutdown if the app created or started it. With no user yet it also issues the one-time
    setup token (see api/auth.py).
    """
    owns_core = core is None
    app_core = core if core is not None else HomeSenseApp(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started = False
        try:
            app_core.initialize()
            auth.init_setup_token(app)
            if start_background:
                await app_core.start()
                started = True
            yield
        finally:
            if owns_core or started:
                await app_core.stop()

    # No public OpenAPI/docs: the schema would be readable without logging in.
    app = FastAPI(title="HomeSense", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.core = app_core
    app.state.templates = Jinja2Templates(directory=WEB_DIR / "templates")
    routes_pages.configure_templates(app.state.templates, settings.timezone, app_core.clock)
    app.state.sse_heartbeat_s = sse.HEARTBEAT_S
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE_S,
        same_site="lax",
        https_only=False,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(routes_api.public)
    app.include_router(routes_api.router)
    app.include_router(sse.router)
    app.include_router(auth.router)
    app.include_router(grafana_proxy.router)
    app.include_router(routes_pages.public)
    app.include_router(routes_pages.router)
    return app
