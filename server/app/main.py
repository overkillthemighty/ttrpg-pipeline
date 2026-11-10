"""Application entry point: `uvicorn app.main:app` (run a single worker process)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import api, db, recordings, security, web
from .config import settings
from .worker import worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ttrpg")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    db.init()
    generated = security.ensure_bootstrap_admin(settings.admin_username, settings.admin_password)
    if generated:
        log.warning("Created admin user '%s' with generated password: %s  (change it under Account)",
                    settings.admin_username, generated)
    n = recordings.recover()
    if n:
        log.info("Released %d browser recording(s) that were being finished when the server stopped", n)
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="TTRPG Transcription Server", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="ttrpg_session",
    max_age=settings.session_days * 86400,
    same_site="lax",
    https_only=settings.cookie_secure,
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(api.router)
app.include_router(web.router)


@app.exception_handler(web.LoginRequired)
async def _login_required(request: Request, exc: web.LoginRequired):
    return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/static/"):
        # Versioned URLs (?v=hash, see web.asset) never change; anything else must be revalidated.
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.query_params.get("v") else "no-cache"
    return resp
