"""JSON API used by Submit-Session.ps1 (API key) and by the dashboard's JavaScript (session + CSRF).

Upload protocol (resumable):
  POST  /api/v1/jobs                      create job, declare files -> {id, files[]}
  GET   /api/v1/jobs/{id}/files/{name}    -> {received, size, complete}
  PATCH /api/v1/jobs/{id}/files/{name}    body = next chunk, header Upload-Offset = byte offset
  POST  /api/v1/jobs/{id}/submit          verify sizes/checksums, queue for processing
  POST  /api/v1/jobs/{id}/sync            raw uploads: choose how to use the clap, process again

Browser recordings (Record page; see recordings.py):
  PUT   /api/v1/recordings/{rid}                                 create/update (id made by the browser)
  PUT   /api/v1/recordings/{rid}/parts/{part}/streams/{stream}/chunks/{seq}
                                                                 one chunk of one mic; headers X-Stream-Started-At, X-Stream-Mime
  GET   /api/v1/recordings/{rid}  |  GET /api/v1/recordings      state / unfinished recordings
  POST  /api/v1/recordings/{rid}/finish                          build the tracks and queue the job
  DELETE /api/v1/recordings/{rid}
"""
from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import client, db, jobs, recordings
from .config import settings
from . import security
from .security import Principal, api_principal, human_only
from .worker import worker

router = APIRouter(prefix="/api/v1")


class FileDecl(BaseModel):
    name: str
    filename: str
    size: int
    sha256: str | None = None


class JobCreate(BaseModel):
    session_date: str
    title: str | None = None
    client_ref: str | None = None
    synced: bool = True
    files: list[FileDecl]
    options: dict = Field(default_factory=dict)


class RetryBody(BaseModel):
    clean: bool = False


class RecordingBody(BaseModel):
    session_date: str
    title: str | None = None
    mode: str = "single"                       # single (room mic) | lavs
    layout: list[dict]                         # [{stream, device, channels, names: [player per channel or null]}]
    clap: str = "auto"                         # auto | none
    options: dict = Field(default_factory=dict)


class FinishBody(BaseModel):
    chunks: dict[str, dict[str, int]] | None = None   # {part: {stream: chunks}}; None = use what the server has


class SyncBody(BaseModel):
    clap: str                                   # clap | none | manual | auto
    clap_times: dict | str | None = None        # manual: {"Player1": 4.04, ...} or "Player1=4.04, ..."


@router.get("/health")
def health() -> dict:
    return {"ok": True, "running": worker.current, "client_version": client.version()}


@router.get("/health/idle")
def health_idle(response: Response) -> dict:
    """For deploy scripts: 200 when an update can restart the server without interrupting anything,
    503 while a job runs or waits in the queue, a laptop upload is in progress, or a browser
    recording is still sending audio. Wait for 200, then redeploy."""
    reasons = []
    if worker.current:
        reasons.append("a job is running")
    with db.connect() as c:
        queued = c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
        uploading = c.execute("SELECT COUNT(*) FROM jobs WHERE status='uploading' AND updated_at > ?",
                              (db.now() - 300,)).fetchone()[0]
    if queued:
        reasons.append(f"{queued} job(s) queued")
    if uploading:
        reasons.append(f"{uploading} upload(s) in progress")
    rec = len(recordings.active())
    if rec:
        reasons.append(f"{rec} browser recording(s) in progress")
    if reasons:
        response.status_code = 503
    return {"idle": not reasons, "reasons": reasons}


@router.get("/whoami")
def whoami(p: Principal = Depends(api_principal)) -> dict:
    return {"kind": p.kind, "name": p.name}


@router.get("/jobs")
def list_jobs(limit: int = 50, p: Principal = Depends(api_principal)) -> list[dict]:
    return [jobs.public_job(j, include_log=False, viewer=p) for j in db.list_jobs(limit=max(1, min(limit, 500)))]


@router.post("/jobs", status_code=201)
def create_job(body: JobCreate, p: Principal = Depends(api_principal)) -> dict:
    job = jobs.create_job(
        title=body.title or "", session_date=body.session_date,
        files=[f.model_dump() for f in body.files], synced=body.synced, options=body.options,
        source="dashboard" if p.is_human else "api", submitted_by=p.name, client_ref=body.client_ref,
        can_publish=p.can_publish, owner=p.owner)
    return jobs.public_job(job, viewer=p)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, p: Principal = Depends(api_principal)) -> dict:
    return jobs.public_job(jobs.require_job(job_id), viewer=p)


def _job(job_id: str, p: Principal, *, manage: bool = False, output: bool = False) -> dict:
    """The job, if this caller may see its output (output=True) or change it (manage=True)."""
    job = jobs.require_job(job_id)
    if manage and not p.can_manage(job):
        raise HTTPException(status_code=403, detail="Only the job's owner or an admin can do that")
    if output and not p.can_see_output(job):
        raise HTTPException(status_code=403, detail="Only the job's owner and users with full queue visibility can see its output")
    return job


_upload_locks: dict[str, asyncio.Lock] = {}


def _lock_for(job_id: str, name: str) -> asyncio.Lock:
    return _upload_locks.setdefault(f"{job_id}/{name}", asyncio.Lock())


@router.get("/jobs/{job_id}/files/{name}")
async def file_state(job_id: str, name: str, p: Principal = Depends(api_principal)) -> dict:
    job = _job(job_id, p, manage=True)
    f = jobs.file_entry(job, name)
    lock = _lock_for(job_id, name)
    if not lock.locked() and job["status"] == "uploading":
        async with lock:
            try:  # a fully received .part (e.g. server restarted mid-verify) gets finalised here
                await run_in_threadpool(jobs.finalize_part, job_id, f)
            except ValueError:
                pass
    got = jobs.received_bytes(job_id, f)
    return {"name": name, "size": f["size"], "received": got, "complete": jobs.is_complete(job_id, f)}


def _append(path, offset: int, data: bytes) -> None:
    with open(path, "ab") as fh:
        fh.truncate(offset)  # drop anything past the agreed offset
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


@router.patch("/jobs/{job_id}/files/{name}")
async def upload_chunk(job_id: str, name: str, request: Request, p: Principal = Depends(api_principal)) -> JSONResponse:
    job = _job(job_id, p, manage=True)
    if job["status"] != "uploading":
        raise HTTPException(status_code=409, detail=f"Job is {job['status']}; uploads are closed")
    f = jobs.file_entry(job, name)
    final, part = jobs.upload_paths(job_id, f)
    lock = _lock_for(job_id, name)
    if lock.locked():
        cur = part.stat().st_size if part.exists() else 0
        return JSONResponse(status_code=409, content={"detail": "Another upload of this file is in progress", "received": cur})
    async with lock:
        if final.exists():
            return JSONResponse({"received": final.stat().st_size, "complete": True})
        try:
            offset = int(request.headers.get("upload-offset", "-1"))
        except ValueError:
            offset = -1
        current = part.stat().st_size if part.exists() else 0
        if offset != current:
            return JSONResponse(status_code=409, content={"detail": "Offset mismatch", "received": current},
                                headers={"Upload-Offset": str(current)})
        limit = settings.max_chunk_mb * 1024 * 1024
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > limit or current + len(buf) > f["size"]:
                raise HTTPException(status_code=413, detail="Chunk too large or exceeds declared file size")
        part.parent.mkdir(parents=True, exist_ok=True)
        await run_in_threadpool(_append, part, current, bytes(buf))
        received = current + len(buf)
        complete = False
        if received >= f["size"]:
            try:
                complete = await run_in_threadpool(jobs.finalize_part, job_id, f)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
        db.update_job(job_id, message=f"Received {name}" if complete else f"Uploading {name}")
        return JSONResponse({"received": received, "complete": complete}, headers={"Upload-Offset": str(received)})


@router.post("/jobs/{job_id}/submit")
def submit(job_id: str, p: Principal = Depends(api_principal)) -> dict:
    job = _job(job_id, p, manage=True)
    if job["status"] == "queued":
        return jobs.public_job(job, viewer=p)
    if job["status"] != "uploading":
        raise HTTPException(status_code=409, detail=f"Job is {job['status']}")
    missing = [f["name"] for f in jobs.file_status(job) if not f["complete"]]
    if missing:
        raise HTTPException(status_code=409, detail=f"Files not fully uploaded: {', '.join(missing)}")
    db.update_job(job_id, status="queued", stage="queued", message="Queued", percent=0.0)
    return jobs.public_job(jobs.require_job(job_id), viewer=p)


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, p: Principal = Depends(api_principal)) -> dict:
    jobs.cancel(_job(job_id, p, manage=True))
    return jobs.public_job(jobs.require_job(job_id), viewer=p)


@router.post("/jobs/{job_id}/retry")
def retry(job_id: str, body: RetryBody | None = None, p: Principal = Depends(api_principal)) -> dict:
    human_only(p)
    jobs.retry(_job(job_id, p, manage=True), clean=bool(body and body.clean))
    return jobs.public_job(jobs.require_job(job_id), viewer=p)


@router.post("/jobs/{job_id}/sync")
def resync(job_id: str, body: SyncBody, p: Principal = Depends(api_principal)) -> dict:
    """Raw uploads: choose how to use the clap and process the job again (reuses the upload)."""
    human_only(p)
    jobs.resync(_job(job_id, p, manage=True), body.clap, body.clap_times)
    return jobs.public_job(jobs.require_job(job_id), viewer=p)


@router.delete("/jobs/{job_id}")
def delete(job_id: str, p: Principal = Depends(api_principal)) -> dict:
    human_only(p)
    jobs.delete(_job(job_id, p, manage=True))
    return {"deleted": job_id}


@router.get("/jobs/{job_id}/final/{filename}")
def download(job_id: str, filename: str, p: Principal = Depends(api_principal)) -> FileResponse:
    _job(job_id, p, output=True)
    if filename not in jobs.FINAL_FILES and filename not in jobs.LEGACY_FINAL_FILES:
        raise HTTPException(status_code=404, detail="Not found")
    path = jobs.final_dir(job_id) / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, filename=filename)


@router.get("/jobs/{job_id}/log")
def full_log(job_id: str, p: Principal = Depends(api_principal)):
    human_only(p)
    _job(job_id, p, output=True)
    path = jobs.log_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Log not kept for this job")
    return FileResponse(path, media_type="text/plain", filename=f"{job_id}.log")


# ----------------------------------------------------------------- browser recordings

MAX_REC_CHUNK = 32 * 1024 * 1024


@router.get("/csrf")
def csrf(request: Request, p: Principal = Depends(api_principal)) -> dict:
    """Current CSRF token for a signed-in browser (the Record page refreshes it after a re-login)."""
    human_only(p)
    from .security import csrf_token
    return {"csrf": csrf_token(request)}


def _rec_owner(meta: dict) -> str | None:
    if meta.get("owner"):
        return meta["owner"]
    by = meta.get("created_by") or ""
    return None if by.startswith("key:") else by or None


def _rec(rid: str, p: Principal) -> dict:
    """An existing recording the caller owns (or any, for an admin)."""
    meta = recordings.require(rid)
    if not (p.is_admin or p.owns(_rec_owner(meta))):
        raise HTTPException(status_code=403, detail="This recording belongs to another user")
    return meta


@router.get("/recordings")
def list_recordings(p: Principal = Depends(api_principal)) -> list[dict]:
    return [r for r in recordings.list_unfinished() if p.is_admin or p.owns(r.get("owner") or r.get("created_by"))]


@router.put("/recordings/{rid}")
def upsert_recording(rid: str, body: RecordingBody, p: Principal = Depends(api_principal)) -> dict:
    if recordings.read(rid):
        _rec(rid, p)
    return recordings.public(recordings.upsert(rid, title=body.title or "", session_date=body.session_date,
                                               mode=body.mode, layout=body.layout, clap=body.clap,
                                               options=body.options, created_by=p.name, owner=p.owner))


@router.get("/recordings/{rid}")
def get_recording(rid: str, p: Principal = Depends(api_principal)) -> dict:
    return recordings.public(_rec(rid, p))


@router.put("/recordings/{rid}/parts/{part}/streams/{stream}/chunks/{seq}")
async def put_recording_chunk(rid: str, part: int, stream: str, seq: int, request: Request,
                              p: Principal = Depends(api_principal)) -> dict:
    _rec(rid, p)
    try:
        started = int(request.headers.get("x-stream-started-at", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Stream-Started-At header (ms since 1970) is required")
    mime = request.headers.get("x-stream-mime", "")
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > MAX_REC_CHUNK:
            raise HTTPException(status_code=413, detail="Chunk too large")
    return await run_in_threadpool(recordings.put_chunk, rid, part, stream, seq, bytes(buf), started_at_ms=started, mime=mime)


@router.post("/recordings/{rid}/finish")
def finish_recording(rid: str, body: FinishBody | None = None, p: Principal = Depends(api_principal)) -> dict:
    meta = _rec(rid, p)
    # The job belongs to whoever recorded it, and publishes only if that account may (even if an admin finishes it).
    owner = _rec_owner(meta) or p.owner
    can_pub = security.user_can_publish(security.get_user_by_name(owner)) if owner else p.can_publish
    job = recordings.finish(rid, expected=body.chunks if body else None, submitted_by=p.name,
                            can_publish=can_pub, owner=owner)
    return jobs.public_job(job, viewer=p)


@router.delete("/recordings/{rid}")
def delete_recording(rid: str, p: Principal = Depends(api_principal)) -> dict:
    _rec(rid, p)
    recordings.delete(rid)
    return {"deleted": rid}
