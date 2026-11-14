"""GitHub login for the board (owner: C), ticket C11.

    GET  /auth/github/login      -> GitHub's "Authorize" page
    GET  /auth/github/callback   <- GitHub sends the user back with a code; we swap it for a token
    GET  /auth/me                {enabled, logged_in, login, avatar_url}
    GET  /github/repos           the user's repos, most recently updated first
    POST /auth/logout

Setup: create an OAuth app at https://github.com/settings/developers ("New OAuth App"), with
Authorization callback URL = the address you open the board on + ``/auth/github/callback``
(e.g. ``http://localhost:8000/auth/github/callback`` or ``http://<laptop-ip>:8000/...`` for a phone).
Put its keys in ``.env``: ``GITHUB_CLIENT_ID``, ``GITHUB_CLIENT_SECRET``. Optional
``GITHUB_OAUTH_SCOPES`` (default ``read:user``: public repos only; add ``repo`` for private ones).
No keys = login is hidden and the board keeps the paste-a-URL field.

The token stays in this server's memory (one login session per browser cookie). It is never
written to run.json, events, logs or disk; a run started while logged in gets it only as
GITHUB_TOKEN in its own process environment, which ingest uses to clone private repos.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

COOKIE = "democut_session"
STATE_COOKIE = "democut_oauth_state"
SESSION_TTL_S = 12 * 3600
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API = "https://api.github.com"

# session id -> {"token", "login", "avatar_url", "created"}
_sessions: dict[str, dict] = {}


def _http() -> httpx.AsyncClient:
    """One place to make GitHub calls (tests swap this for a fake)."""
    return httpx.AsyncClient(timeout=15)


def enabled() -> bool:
    return bool(os.environ.get("GITHUB_CLIENT_ID") and os.environ.get("GITHUB_CLIENT_SECRET"))


def _callback_url(request: Request) -> str:
    return str(request.url_for("github_callback"))


def session(request: Request) -> Optional[dict]:
    sid = request.cookies.get(COOKIE)
    s = _sessions.get(sid or "")
    if s and time.time() - s["created"] > SESSION_TTL_S:
        _sessions.pop(sid, None)
        return None
    return s


def token_for(request: Request) -> Optional[str]:
    """The logged-in user's token, for starting a run. Never log or store it."""
    s = session(request)
    return s["token"] if s else None


@router.get("/auth/github/login", include_in_schema=False)
def github_login(request: Request) -> RedirectResponse:
    if not enabled():
        raise HTTPException(404, "GitHub login is not set up (GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET)")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": os.environ["GITHUB_CLIENT_ID"],
        "redirect_uri": _callback_url(request),
        "scope": os.environ.get("GITHUB_OAUTH_SCOPES", "read:user"),
        "state": state,
        "allow_signup": "false",
    }
    r = RedirectResponse(f"{AUTHORIZE_URL}?{urlencode(params)}", status_code=302)
    r.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax")
    return r


@router.get("/auth/github/callback", name="github_callback", include_in_schema=False)
async def github_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    if error:
        return RedirectResponse(f"/?login_error={error}", status_code=302)
    expected = request.cookies.get(STATE_COOKIE)
    if not code or not expected or not secrets.compare_digest(state, expected):
        raise HTTPException(400, "login expired or was started elsewhere; try again")
    async with _http() as client:
        r = await client.post(
            TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": os.environ["GITHUB_CLIENT_ID"],
                "client_secret": os.environ["GITHUB_CLIENT_SECRET"],
                "code": code,
                "redirect_uri": _callback_url(request),
            },
        )
        token = r.json().get("access_token") if r.status_code == 200 else None
        if not token:
            return RedirectResponse("/?login_error=token", status_code=302)
        u = await client.get(f"{API}/user", headers=_gh_headers(token))
        if u.status_code != 200:
            return RedirectResponse("/?login_error=user", status_code=302)
        user = u.json()
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = {"token": token, "login": user.get("login"), "avatar_url": user.get("avatar_url"), "created": time.time()}
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(COOKIE, sid, max_age=SESSION_TTL_S, httponly=True, samesite="lax")
    resp.delete_cookie(STATE_COOKIE)
    return resp


def _gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


@router.get("/auth/me")
def me(request: Request) -> dict:
    s = session(request)
    return {"enabled": enabled(), "logged_in": bool(s), "login": s and s["login"], "avatar_url": s and s["avatar_url"]}


@router.post("/auth/logout")
def logout(request: Request) -> dict:
    _sessions.pop(request.cookies.get(COOKIE) or "", None)
    return {"logged_in": False}


@router.get("/github/repos")
async def repos(request: Request) -> list[dict]:
    """The user's repos (owner, collaborator, org member), most recently updated first."""
    s = session(request)
    if not s:
        raise HTTPException(401, "log in with GitHub first")
    async with _http() as client:
        r = await client.get(
            f"{API}/user/repos",
            params={"sort": "updated", "per_page": 100, "affiliation": "owner,collaborator,organization_member"},
            headers=_gh_headers(s["token"]),
        )
    if r.status_code == 401:
        _sessions.pop(request.cookies.get(COOKIE) or "", None)
        raise HTTPException(401, "GitHub login expired; log in again")
    if r.status_code != 200:
        raise HTTPException(502, f"GitHub answered {r.status_code}")
    return [
        {
            "full_name": x["full_name"],
            "url": x["html_url"],
            "private": x["private"],
            "description": x.get("description") or "",
            "updated_at": x.get("pushed_at") or x.get("updated_at"),
        }
        for x in r.json()
    ]
