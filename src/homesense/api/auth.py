"""Login, logout and first-run account setup (HTML forms, CSRF-protected).

Wrong credentials re-render the login form with HTTP 401 and one generic message, whether or not the
username exists (a dummy hash is verified for unknown users so timing does not tell either).

First run: while there is no user, startup generates a one-time setup token and writes it to
<data_dir>/auth/setup-token.txt (only the file path is logged, never the token). /setup only creates the
first account when that token is typed into the form (POST field only, never a URL parameter, so it
cannot end up in logs, browser history or Referer headers), so nobody else on the LAN can claim the
admin account. The token is valid for one hour (app
Clock); the file is owner-only (0600 where the OS supports modes) and deleted as soon as a user
exists. Once a user exists /setup is 404.
"""

# No `from __future__ import annotations`: FastAPI inspects these signatures at runtime.

import functools
import hmac
import logging
import os
import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from homesense.api.deps import check_csrf, csrf_token, current_user, get_core, start_session
from homesense.app import HomeSenseApp
from homesense.security import hash_password, verify_password

log = logging.getLogger(__name__)

router = APIRouter()

MIN_PASSWORD = 8
MAX_USERNAME = 64
LOGIN_ERROR = "Nieprawidłowa nazwa użytkownika lub hasło."
SETUP_TOKEN_ERROR = (
    "Nieprawidłowy token konfiguracji. Znajdziesz go w pliku auth\\setup-token.txt w katalogu danych "
    "(ścieżka jest w logu aplikacji)."
)
SETUP_TOKEN_EXPIRED = (
    "Token konfiguracji wygasł. Uruchom ponownie usługę HomeSense, aby wygenerować nowy token."
)
SETUP_TOKEN_FILE = "setup-token.txt"
SETUP_TOKEN_TTL = timedelta(hours=1)

Core = Annotated[HomeSenseApp, Depends(get_core)]
FormStr = Annotated[str, Form()]


@functools.cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_hex(16))


def init_setup_token(app: FastAPI) -> None:
    """At startup: a fresh setup token when no user exists yet; otherwise remove any stale token file."""
    core: HomeSenseApp = app.state.core
    path = core.settings.setup_token_path
    app.state.setup_token = None
    app.state.setup_token_issued_at = None
    path.unlink(missing_ok=True)
    (core.settings.data_dir / SETUP_TOKEN_FILE).unlink(missing_ok=True)  # pre-auth-folder location
    if core.users.count() > 0:
        return
    token = secrets.token_urlsafe(24)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700)
    # Always a fresh file (the old one was unlinked above), so the 0600 mode applies.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    app.state.setup_token = token
    app.state.setup_token_issued_at = core.clock.now()
    log.warning("Brak kont — token konfiguracji zapisano w %s; otwórz /setup i wpisz go w formularzu", path)


def _finish_setup(request: Request) -> None:
    request.app.state.setup_token = None
    core: HomeSenseApp = request.app.state.core
    core.settings.setup_token_path.unlink(missing_ok=True)


def _setup_token_error(request: Request, sent: str) -> str | None:
    """None when `sent` is the current, unexpired setup token; otherwise the Polish error."""
    state = request.app.state
    expected = getattr(state, "setup_token", None)
    if not (expected and sent and hmac.compare_digest(sent.encode("utf-8"), expected.encode("utf-8"))):
        return SETUP_TOKEN_ERROR
    if state.core.clock.now() - state.setup_token_issued_at >= SETUP_TOKEN_TTL:
        return SETUP_TOKEN_EXPIRED
    return None


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _render(request: Request, name: str, status_code: int = 200, **context) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, name, {"csrf_token": csrf_token(request), **context}, status_code=status_code
    )


def _setup_closed() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND)


@router.get("/login")
def login_page(request: Request, core: Core) -> Response:
    if core.users.count() == 0:
        return _redirect("/setup")
    if current_user(request) is not None:
        return _redirect("/")
    return _render(request, "login.html")


@router.post("/login", dependencies=[Depends(check_csrf)])
async def login(request: Request, core: Core, username: FormStr = "", password: FormStr = "") -> Response:
    username = username.strip()
    user = await run_in_threadpool(core.users.get_by_username, username) if username else None
    stored = user[2] if user else _dummy_hash()
    valid = await run_in_threadpool(verify_password, password, stored)
    if user is None or not valid:
        return _render(
            request, "login.html", status.HTTP_401_UNAUTHORIZED, error=LOGIN_ERROR, username=username
        )
    start_session(request, user[1], user[2])
    return _redirect("/")


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request) -> Response:
    request.session.clear()
    return _redirect("/login")


@router.get("/setup")
def setup_page(request: Request, core: Core) -> Response:
    if core.users.count() > 0:
        raise _setup_closed()
    return _render(request, "setup.html", min_password=MIN_PASSWORD)


def _setup_error(username: str, password: str, password_confirm: str) -> str | None:
    if not username:
        return "Podaj nazwę użytkownika."
    if len(username) > MAX_USERNAME:
        return f"Nazwa użytkownika może mieć najwyżej {MAX_USERNAME} znaków."
    if len(password) < MIN_PASSWORD:
        return f"Hasło musi mieć co najmniej {MIN_PASSWORD} znaków."
    if password != password_confirm:
        return "Hasła nie są takie same."
    return None


@router.post("/setup")
async def setup(
    request: Request,
    core: Core,
    username: FormStr = "",
    password: FormStr = "",
    password_confirm: FormStr = "",
    setup_token: FormStr = "",
) -> Response:
    if await run_in_threadpool(core.users.count) > 0:
        raise _setup_closed()
    await check_csrf(request)
    username = username.strip()
    # The setup token is never rendered back into the page; the user types it again on errors.
    form = {"username": username, "min_password": MIN_PASSWORD}
    token_error = _setup_token_error(request, setup_token)
    if token_error:
        return _render(request, "setup.html", status.HTTP_403_FORBIDDEN, error=token_error, **form)
    error = _setup_error(username, password, password_confirm)
    if error:
        return _render(request, "setup.html", status.HTTP_400_BAD_REQUEST, error=error, **form)
    password_hash = await run_in_threadpool(hash_password, password)
    # Atomic: only the very first account may be created here, even with concurrent requests.
    if not await run_in_threadpool(core.users.create_first, username, password_hash):
        raise _setup_closed()
    _finish_setup(request)
    start_session(request, username, password_hash)
    return _redirect("/")
