"""Dashboard pages (server-rendered) and login: local accounts + optional Nextcloud OIDC."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import client, db, jobs, security
from .config import settings
from .worker import worker

log = logging.getLogger("ttrpg.web")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_STATIC_DIR = Path(__file__).parent / "static"
_asset_versions: dict[str, tuple[float, str]] = {}


def asset(name: str) -> str:
    """/static URL with a content hash, so browsers (and proxy caches) pick up new files after a redeploy."""
    import hashlib
    p = _STATIC_DIR / name
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return f"/static/{name}"
    cached = _asset_versions.get(name)
    if not cached or cached[0] != mtime:
        cached = (mtime, hashlib.sha256(p.read_bytes()).hexdigest()[:10])
        _asset_versions[name] = cached
    return f"/static/{name}?v={cached[1]}"


templates.env.globals["asset"] = asset


def _fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


templates.env.filters["dt"] = _fmt_time

# ----------------------------------------------------------------- OIDC

oauth = None
if settings.oidc_enabled:
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        "nextcloud",
        server_metadata_url=settings.oidc_discovery_url,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        client_kwargs={"scope": settings.oidc_scopes},
    )


class LoginRequired(Exception):
    pass


def _user(request: Request) -> dict:
    user = security.session_user(request)
    if not user:
        raise LoginRequired()
    return user


def _admin(request: Request) -> dict:
    user = _user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admins only")
    return user


def render(request: Request, name: str, user: dict | None, **ctx) -> HTMLResponse:
    ctx.update(user=user, csrf=security.csrf_token(request), oidc_enabled=settings.oidc_enabled,
               oidc_label=settings.oidc_button_label, nextcloud_enabled=settings.nextcloud_enabled,
               flash=request.session.pop("flash", None))
    return templates.TemplateResponse(request, name, ctx)


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session["flash"] = {"msg": msg, "kind": kind}


def back(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# ----------------------------------------------------------------- login / logout

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if security.session_user(request):
        return back("/")
    return render(request, "login.html", None, next=next)


def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"


def _start_session(request: Request, user: dict) -> None:
    request.session.clear()
    request.session["uid"] = user["id"]
    security.csrf_token(request)
    security.touch_login(user["id"])


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          csrf: str = Form(""), next: str = Form("/")):
    security.check_csrf(request, csrf)
    ip = security.client_ip(request)
    if security.throttle.blocked(ip, username):
        flash(request, "Too many failed attempts. Wait 15 minutes and try again.", "err")
        return back("/login")
    user = security.get_user_by_name(username.strip())
    if not user or not security.verify_password(password, user["password_hash"]):
        security.throttle.fail(ip, username)
        log.warning("Failed login for %r from %s", username, ip)
        flash(request, "Wrong username or password.", "err")
        return back(f"/login?next={quote(_safe_next(next))}")
    security.throttle.success(ip, username)
    _start_session(request, user)
    return back(_safe_next(next))


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    security.check_csrf(request, csrf)
    request.session.clear()
    return back("/login")


@router.post("/auth/oidc/link")
async def oidc_link(request: Request, csrf: str = Form("")):
    """Logged-in user links their Nextcloud account (so it can be used to sign in later)."""
    user = _user(request)
    security.check_csrf(request, csrf)
    if not oauth:
        raise HTTPException(status_code=404)
    request.session["link_uid"] = user["id"]
    return await oidc_login(request)


@router.get("/auth/oidc/login")
async def oidc_login(request: Request):
    if not oauth:
        raise HTTPException(status_code=404)
    redirect_uri = (settings.base_url + "/auth/oidc/callback") if settings.base_url else str(request.url_for("oidc_callback"))
    return await oauth.nextcloud.authorize_redirect(request, redirect_uri)


@router.get("/auth/oidc/callback", name="oidc_callback")
async def oidc_callback(request: Request):
    if not oauth:
        raise HTTPException(status_code=404)
    try:
        token = await oauth.nextcloud.authorize_access_token(request)
    except Exception as e:  # authlib raises several types
        log.warning("OIDC callback failed: %s", e)
        flash(request, "Nextcloud sign-in failed. Try again.", "err")
        return back("/login")
    claims = dict(token.get("userinfo") or {})
    if not claims.get(settings.oidc_username_claim):
        try:
            claims.update(await oauth.nextcloud.userinfo(token=token))
        except Exception:
            pass
    sub = str(claims.get("sub") or "").strip()
    iss = str(claims.get("iss") or "").strip()
    username = str(claims.get(settings.oidc_username_claim) or "").strip()
    if not sub:
        flash(request, "Nextcloud sign-in returned no subject.", "err")
        return back("/login")
    ident = f"{iss}|{sub}"
    groups = claims.get(settings.oidc_groups_claim) or []
    if isinstance(groups, str):
        groups = [groups]

    # Explicit linking from the Account page
    link_uid = request.session.pop("link_uid", None)
    if link_uid:
        me = security.session_user(request)
        if me and me["id"] == link_uid:
            security.set_oidc_sub(me["id"], ident)
            flash(request, f"Linked Nextcloud account '{username or sub}'. You can now sign in with it.")
            return back("/account")

    user = security.get_user_by_oidc(ident)
    if not user and username:
        existing = security.get_user_by_name(username)
        if existing:
            # Only auto-link accounts an admin created for Nextcloud sign-in (no password, not admin).
            if not existing["password_hash"] and not existing["is_admin"] and not existing.get("oidc_sub"):
                security.set_oidc_sub(existing["id"], ident)
                user = security.get_user_by_name(username)
            else:
                log.warning("Nextcloud user %r matches a local account that hasn't linked Nextcloud", username)
                flash(request, f"The local account '{username}' hasn't linked Nextcloud yet. Sign in with its password "
                               "and use 'Link Nextcloud account' on the Account page.", "err")
                return back("/login")
        else:
            allowed_auto = (username.lower() in {u.lower() for u in settings.oidc_allowed_users}
                            or bool(set(groups) & set(settings.oidc_allowed_groups)))
            if allowed_auto:
                security.create_user(username, None, is_admin=False)
                user = security.get_user_by_name(username)
                security.set_oidc_sub(user["id"], ident)
                log.info("Created user %s from Nextcloud sign-in", username)
    if not user:
        log.warning("Nextcloud user %r is not allowed", username or sub)
        flash(request, f"Nextcloud user '{username or sub}' is not allowed here. Ask an admin to add the account.", "err")
        return back("/login")
    _start_session(request, user)
    return back("/")


# ----------------------------------------------------------------- jobs

@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = _user(request)
    return render(request, "jobs.html", user, jobs=[jobs.public_job(j, include_log=False, viewer=security.principal_for_user(user)) for j in db.list_jobs(200)],
                  running=worker.current)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    user = _user(request)
    raw = jobs.require_job(job_id)
    viewer = security.principal_for_user(user)
    job = jobs.public_job(raw, viewer=viewer)
    return render(request, "job.html", user, job=job, final_names=jobs.FINAL_FILES,
                  can_resync=jobs.can_resync(raw) and viewer.can_manage(raw))


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    user = _user(request)
    return render(request, "upload.html", user, today=datetime.now().strftime("%Y-%m-%d"),
                  models=sorted(jobs.WHISPER_MODELS), default_model=settings.whisper_model,
                  max_chunk_mb=min(settings.max_chunk_mb, 16), diarize_available=bool(settings.hf_token))


@router.get("/record", response_class=HTMLResponse)
def record_page(request: Request):
    user = _user(request)
    return render(request, "record.html", user, today=datetime.now().strftime("%Y-%m-%d"),
                  diarize_available=bool(settings.hf_token))


# ----------------------------------------------------------------- API keys

@router.get("/settings/keys", response_class=HTMLResponse)
def keys_page(request: Request):
    user = _user(request)
    new_key = request.session.pop("new_key", None)
    return render(request, "keys.html", user, keys=security.list_api_keys(None if user["is_admin"] else user["username"]),
                  new_key=new_key)


@router.post("/settings/keys")
def keys_create(request: Request, name: str = Form(...), csrf: str = Form("")):
    user = _user(request)
    security.check_csrf(request, csrf)
    name = name.strip()[:80] or "unnamed"
    request.session["new_key"] = security.create_api_key(name, user["username"])
    return back("/settings/keys")


@router.post("/settings/keys/{key_id}/revoke")
def keys_revoke(request: Request, key_id: int, csrf: str = Form("")):
    user = _user(request)
    security.check_csrf(request, csrf)
    if security.revoke_api_key(key_id, user):
        flash(request, "API key revoked.")
    else:
        flash(request, "Only an admin or the key's creator can revoke it.", "err")
    return back("/settings/keys")


# ----------------------------------------------------------------- laptop scripts

def _server_url(request: Request) -> str:
    return settings.base_url or str(request.base_url).rstrip("/")


def _zip_response(data: bytes, suffix: str = "") -> Response:
    name = f"TTRPG-Recorder-{client.version()}{suffix}.zip"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.get("/client", response_class=HTMLResponse)
def client_page(request: Request):
    user = _user(request)
    return render(request, "client.html", user, version=client.version(), available=client.available(),
                  server_url=_server_url(request))


@router.get("/client/download")
def client_download(request: Request):
    _user(request)
    return _zip_response(client.build_zip(_server_url(request)))


@router.post("/client/download")
def client_download_with_key(request: Request, name: str = Form(...), csrf: str = Form("")):
    user = _user(request)
    security.check_csrf(request, csrf)
    name = name.strip()[:80] or "Recorder laptop"
    key = security.create_api_key(name, user["username"])
    log.info("API key '%s' created by %s for a scripts download", name, user["username"])
    return _zip_response(client.build_zip(_server_url(request), api_key=key), "-with-key")


# ----------------------------------------------------------------- users

@router.get("/settings/users", response_class=HTMLResponse)
def users_page(request: Request):
    user = _admin(request)
    return render(request, "users.html", user, users=security.list_users())


@router.post("/settings/users")
def users_create(request: Request, username: str = Form(...), password: str = Form(""),
                 is_admin: str = Form(""), can_view_all: str = Form(""), can_publish: str = Form(""),
                 csrf: str = Form("")):
    _admin(request)
    security.check_csrf(request, csrf)
    username = username.strip()
    if not jobs.NAME_RE.match(username.replace("@", "_")):
        flash(request, "Usernames may contain letters, digits, @ _ . -", "err")
        return back("/settings/users")
    if password and len(password) < 10:
        flash(request, "Passwords must be at least 10 characters.", "err")
        return back("/settings/users")
    if security.get_user_by_name(username):
        flash(request, "That user already exists.", "err")
        return back("/settings/users")
    security.create_user(username, password or None, bool(is_admin), can_publish=bool(can_publish),
                         can_view_all=bool(can_view_all))
    flash(request, f"Created {username}." + ("" if password else " (Nextcloud sign-in only until a password is set.)"))
    return back("/settings/users")


@router.post("/settings/users/{user_id}/delete")
def users_delete(request: Request, user_id: int, csrf: str = Form("")):
    me = _admin(request)
    security.check_csrf(request, csrf)
    if user_id == me["id"]:
        flash(request, "You can't delete yourself.", "err")
        return back("/settings/users")
    security.delete_user(user_id)
    flash(request, "User deleted.")
    return back("/settings/users")


FLAG_MESSAGES = {
    "is_admin": ("is now an admin.", "is no longer an admin."),
    "can_view_all": ("can now see every job's output.", "now sees only their own jobs' output."),
    "can_publish": ("results are published to Nextcloud.", "results stay on this server (download from each job)."),
}


@router.post("/settings/users/{user_id}/flag")
def users_flag(request: Request, user_id: int, flag: str = Form(...), value: str = Form(""), csrf: str = Form("")):
    me = _admin(request)
    security.check_csrf(request, csrf)
    target = security.get_user(user_id)
    if not target or flag not in security.USER_FLAGS:
        flash(request, "No such user or permission.", "err")
        return back("/settings/users")
    on = value == "1"
    if flag == "is_admin" and not on:
        if user_id == me["id"]:
            flash(request, "You can't remove your own admin permission.", "err")
            return back("/settings/users")
        if target["is_admin"] and security.count_admins() <= 1:
            flash(request, "There must be at least one admin.", "err")
            return back("/settings/users")
    security.set_user_flag(user_id, flag, on)
    flash(request, f"{target['username']}: " + FLAG_MESSAGES[flag][0 if on else 1])
    return back("/settings/users")


@router.post("/settings/users/{user_id}/password")
def users_password(request: Request, user_id: int, password: str = Form(...), csrf: str = Form("")):
    _admin(request)
    security.check_csrf(request, csrf)
    if len(password) < 10:
        flash(request, "Passwords must be at least 10 characters.", "err")
        return back("/settings/users")
    security.set_password(user_id, password)
    flash(request, "Password updated.")
    return back("/settings/users")


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    return render(request, "account.html", _user(request))


@router.post("/account/password")
def account_password(request: Request, current: str = Form(""), new: str = Form(...), csrf: str = Form("")):
    user = _user(request)
    security.check_csrf(request, csrf)
    if user["password_hash"] and not security.verify_password(current, user["password_hash"]):
        flash(request, "Current password is wrong.", "err")
        return back("/account")
    if len(new) < 10:
        flash(request, "Passwords must be at least 10 characters.", "err")
        return back("/account")
    security.set_password(user["id"], new)
    flash(request, "Password changed.")
    return back("/account")


_STATIC = Path(__file__).parent / "static"
_ICON_CACHE = {"Cache-Control": "public, max-age=604800"}


@router.get("/favicon.ico")
def favicon():
    return FileResponse(_STATIC / "favicon.ico", media_type="image/x-icon", headers=_ICON_CACHE)


@router.get("/apple-touch-icon.png")
@router.get("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    return FileResponse(_STATIC / "apple-touch-icon.png", media_type="image/png", headers=_ICON_CACHE)
