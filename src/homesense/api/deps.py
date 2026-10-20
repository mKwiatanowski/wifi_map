"""Request dependencies: core access, session auth and CSRF protection."""

# No `from __future__ import annotations`: FastAPI inspects these signatures at runtime.

import hmac
import secrets

from fastapi import HTTPException, Request, status

from homesense.app import HomeSenseApp
from homesense.security import session_fingerprint

SESSION_USER = "user"
SESSION_FINGERPRINT = "fp"
SESSION_CSRF = "csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


def get_core(request: Request) -> HomeSenseApp:
    return request.app.state.core


def start_session(request: Request, username: str, password_hash: str) -> None:
    """New session for the user: drops everything from before login (no session fixation)."""
    request.session.clear()
    request.session[SESSION_USER] = username
    request.session[SESSION_FINGERPRINT] = session_fingerprint(
        get_core(request).settings.secret_key, password_hash
    )
    rotate_csrf(request)


def _session_valid(request: Request) -> bool:
    """The session's user still exists and its password is unchanged since login."""
    user = request.session.get(SESSION_USER)
    fingerprint = request.session.get(SESSION_FINGERPRINT)
    if not (isinstance(user, str) and user and isinstance(fingerprint, str)):
        return False
    core = get_core(request)
    row = core.users.get_by_username(user)
    if row is None:
        return False
    expected = session_fingerprint(core.settings.secret_key, row[2])
    return hmac.compare_digest(fingerprint.encode("utf-8"), expected.encode("utf-8"))


def current_user(request: Request) -> str | None:
    """The logged-in username, or None. A session whose user was deleted or whose password changed
    since login is revoked (cleared)."""
    if _session_valid(request):
        return request.session[SESSION_USER]
    if SESSION_USER in request.session:
        request.session.clear()
    return None


def require_user(request: Request) -> str:
    """The logged-in username; 401 JSON for /api paths, 303 redirect to /login for pages.

    htmx requests (HX-Request header) get 401 with HX-Redirect: /login instead, so htmx navigates
    to the login page rather than swapping it into a fragment.
    """
    user = current_user(request)
    if user is not None:
        return user
    if request.headers.get("HX-Request") == "true":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Wymagane logowanie", headers={"HX-Redirect": "/login"}
        )
    if request.url.path.startswith("/api"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wymagane logowanie")
    raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})


def csrf_token(request: Request) -> str:
    """The session's CSRF token, created on first use."""
    token = request.session.get(SESSION_CSRF)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF] = token
    return token


def rotate_csrf(request: Request) -> str:
    request.session[SESSION_CSRF] = secrets.token_urlsafe(32)
    return request.session[SESSION_CSRF]


async def check_csrf(request: Request) -> None:
    """403 unless the X-CSRF-Token header or the csrf_token form field matches the session token."""
    expected = request.session.get(SESSION_CSRF)
    sent = request.headers.get(CSRF_HEADER)
    if sent is None and request.headers.get("content-type", "").startswith(FORM_TYPES):
        value = (await request.form()).get(CSRF_FIELD)
        sent = value if isinstance(value, str) else None
    valid = (
        isinstance(expected, str)
        and expected
        and sent
        and hmac.compare_digest(sent.encode("utf-8"), expected.encode("utf-8"))
    )
    if not valid:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nieprawidłowy token CSRF")


async def csrf_protect(request: Request) -> None:
    """check_csrf for state-changing methods only (router-level dependency)."""
    if request.method not in SAFE_METHODS:
        await check_csrf(request)
