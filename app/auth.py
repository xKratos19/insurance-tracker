"""
Google OAuth / OpenID Connect layer.

Public surface:
    install_auth(app)          -> register middleware, /login, /auth/callback, /logout
    require_user(request)      -> FastAPI dependency, returns user dict, redirects otherwise
    is_internal_request(req)   -> bypass check for cron/webhook callers (shared-secret)

Configuration is read from environment variables (see .env.example):
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SESSION_SECRET, APP_BASE_URL,
    ALLOWED_EMAILS, ALLOWED_DOMAIN, INTERNAL_API_TOKEN, AUTH_DISABLED
"""

import os
import secrets
from typing import Optional
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware


GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "0") == "1"

ALLOWED_EMAILS = {
    e.strip().lower()
    for e in os.getenv("ALLOWED_EMAILS", "").split(",")
    if e.strip()
}
ALLOWED_DOMAINS = {
    d.strip().lower().lstrip("@")
    for d in os.getenv("ALLOWED_DOMAIN", "").split(",")
    if d.strip()
}

# Paths that must remain reachable without a login (used for OAuth handshake, health checks,
# cron/webhook callers). Everything else hits the redirect-to-/login middleware.
PUBLIC_PATHS = {
    "/login",
    "/logout",
    "/auth/callback",
    "/auth/error",
    "/healthz",
    "/favicon.ico",
    "/robots.txt",
}
PUBLIC_PREFIXES = (
    "/static/",
)

# Routes that may be hit by the internal scheduler / external webhooks. These are exempt
# only when the caller presents the INTERNAL_API_TOKEN.
INTERNAL_PATHS = {
    "/admin/run-expiration-check",
}


oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def _email_allowed(email: str) -> bool:
    if not email:
        return False
    email = email.lower()
    if ALLOWED_EMAILS and email in ALLOWED_EMAILS:
        return True
    if ALLOWED_DOMAINS:
        domain = email.split("@", 1)[-1]
        if domain in ALLOWED_DOMAINS:
            return True
    # If neither whitelist is configured, refuse by default — fail closed.
    return False


def is_internal_request(request: Request) -> bool:
    if not INTERNAL_API_TOKEN:
        return False
    token = (
        request.headers.get("x-internal-token")
        or request.query_params.get("internal_token")
        or ""
    )
    return secrets.compare_digest(token, INTERNAL_API_TOKEN)


def current_user(request: Request) -> Optional[dict]:
    if AUTH_DISABLED:
        return {"email": "auth-disabled@local", "name": "Local", "picture": ""}
    user = request.session.get("user")
    return user if isinstance(user, dict) else None


def require_user(request: Request) -> dict:
    """FastAPI dependency. Returns the user dict, otherwise raises 401 — caller-friendly
    handling is done by the middleware (which redirects to /login)."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


async def _auth_gate(request: Request, call_next):
    """Starlette middleware: gate every request unless it's public, internal, or already
    authenticated."""
    if AUTH_DISABLED:
        return await call_next(request)

    path = request.url.path

    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)

    if path in INTERNAL_PATHS and is_internal_request(request):
        return await call_next(request)

    if current_user(request):
        return await call_next(request)

    # Unauthenticated — redirect browser navigations to /login, return 401 for API/XHR.
    accept = request.headers.get("accept", "")
    wants_html = "text/html" in accept
    if wants_html and request.method == "GET":
        next_url = request.url.path
        if request.url.query:
            next_url = f"{next_url}?{request.url.query}"
        params = urlencode({"next": next_url})
        return RedirectResponse(url=f"/login?{params}", status_code=303)
    return HTMLResponse(
        '{"detail":"Authentication required"}',
        status_code=401,
        media_type="application/json",
    )


router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request, next: str = "/"):
    if current_user(request):
        return RedirectResponse(next or "/", status_code=303)
    # Stash post-login destination, then kick off the OAuth flow.
    request.session["post_login_next"] = next or "/"
    redirect_uri = (
        f"{APP_BASE_URL}/auth/callback"
        if APP_BASE_URL
        else str(request.url_for("auth_callback"))
    )
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        # Transparent recovery: a stale/missing state token usually means the user
        # retried the flow (e.g. after a 500). Clearing the session and bouncing back
        # to /login restarts authorise cleanly instead of dead-ending on an error page.
        if exc.error in {"mismatching_state", "csrf_warning", "missing_state"}:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        return RedirectResponse(url=f"/auth/error?reason={exc.error}", status_code=303)

    userinfo = token.get("userinfo") or {}
    email = (userinfo.get("email") or "").lower()
    if not userinfo.get("email_verified", False):
        return RedirectResponse(url="/auth/error?reason=email_unverified", status_code=303)
    if not _email_allowed(email):
        return RedirectResponse(url="/auth/error?reason=forbidden", status_code=303)

    request.session["user"] = {
        "email": email,
        "name": userinfo.get("name", ""),
        "picture": userinfo.get("picture", ""),
    }
    next_url = request.session.pop("post_login_next", "/")
    return RedirectResponse(url=next_url or "/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@router.get("/auth/error", response_class=HTMLResponse)
async def auth_error(request: Request, reason: str = "unknown"):
    messages = {
        "forbidden": "Your Google account is not authorised for this application.",
        "email_unverified": "Your Google email address is not verified.",
        "access_denied": "You declined the authorisation request.",
    }
    msg = messages.get(reason, f"Authentication failed ({reason}).")
    return HTMLResponse(
        f"""<!DOCTYPE html><html><head><title>Access denied</title>
<script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-50 min-h-screen flex items-center justify-center">
  <div class="bg-white shadow rounded p-8 max-w-md text-center">
    <h1 class="text-xl font-bold text-red-600 mb-2">Access denied</h1>
    <p class="text-gray-700 mb-4">{msg}</p>
    <a href="/login" class="text-blue-600 hover:underline">Try again</a>
  </div>
</body></html>""",
        status_code=403,
    )


def install_auth(app) -> None:
    """Attach session middleware, auth gate, and the OAuth routes to the FastAPI app.

    Middleware order matters: `add_middleware` prepends, so the LAST added wraps first.
    We need SessionMiddleware to run before the gate (so `request.session` is populated)
    — therefore the gate is registered first, SessionMiddleware second."""
    app.middleware("http")(_auth_gate)
    app.add_middleware(
        SessionMiddleware,
        secret_key=SESSION_SECRET,
        same_site="lax",
        https_only=APP_BASE_URL.startswith("https://"),
        max_age=60 * 60 * 24 * 7,
    )
    app.include_router(router)
