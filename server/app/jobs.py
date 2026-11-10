"""Job bookkeeping: creation, upload state, retry/cancel/delete, on-disk layout, retention.

On-disk layout per job (DATA_DIR/jobs/<id>/):
    input/   uploaded files (<name>.<ext>, or <name>.<ext>.part while uploading)
    work/    intermediate files (kept only while a job is unfinished or failed)
    final/   the 4 result files (lav_mix.flac, transcript_lav_assigned.{csv,md,json})
    job.log  full log (deleted on success unless KEEP_LOG_ON_SUCCESS=true)
"""
from __future__ import annotations

import re
import secrets
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import db
from .config import settings

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
AUDIO_EXT = {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wma", ".mka"}
FINAL_FILES = ["lav_mix.flac", "transcript_lav_assigned.csv", "transcript_lav_assigned.md", "transcript_lav_assigned.json"]
# Names used by jobs finished before the mix was switched to FLAC (still downloadable).
LEGACY_FINAL_FILES = ["lav_mix.wav"]

ACTIVE = {"uploading", "queued", "running"}
TERMINAL = {"done", "failed", "cancelled"}
# "review": paused after the clap search, waiting for someone to choose how to sync (see resync()).
WAITING = {"review"}

# Options a client may set per job, with type and bounds.
OPTION_SPEC: dict[str, tuple[type, Any, Any]] = {
    "whisper_model": (str, None, None),
    "language": (str, None, None),
    "batch_size": (int, 1, 64),
    "overlap_db": (float, 0.0, 40.0),
    "label_overlaps": (bool, None, None),
    "switch_cost_db": (float, 0.0, 60.0),
    "switch_cost_boundary_db": (float, 0.0, 60.0),
    "silence_db": (float, -120.0, 0.0),
    "sync_window_seconds": (float, 1.0, 600.0),
    "sync_search_start_seconds": (float, 0.0, 3600.0),
    "sync_clap": (str, None, None),       # raw uploads: how to use the clap, see SYNC_CLAP
    "clap_times": (dict, None, None),     # sync_clap=manual: {track name: clap time in seconds}
    "sync_max_spread_seconds": (float, 0.01, 60.0),
    "publish": (bool, None, None),
    "enrollment": (list, None, None),
    "mode": (str, None, None),            # "lavs" (default) or "single"
    "diarize": (bool, None, None),        # single-mic only
    "min_speakers": (int, 1, 20),
    "max_speakers": (int, 1, 20),
}
MODES = {"lavs", "single"}
# auto           use the clap if it lines up across tracks, otherwise keep the tracks as recorded
# ask_if_unsure  like auto, but stop and ask (status "review") when it doesn't line up
# ask            always stop and ask after finding the clap
# clap           use the detected clap even if it doesn't line up
# none           ignore the clap: keep the tracks as recorded (they start together)
# manual         use the clap times in clap_times
SYNC_CLAP = {"auto", "ask_if_unsure", "ask", "clap", "none", "manual"}
WHISPER_MODELS = {"tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en",
                  "large-v1", "large-v2", "large-v3", "large-v3-turbo", "turbo", "distil-large-v3"}


def job_dir(job_id: str) -> Path:
    return settings.jobs_dir / job_id


def input_dir(job_id: str) -> Path:
    return job_dir(job_id) / "input"


def work_dir(job_id: str) -> Path:
    return job_dir(job_id) / "work"


def final_dir(job_id: str) -> Path:
    return job_dir(job_id) / "final"


def log_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.log"


def _bad(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail=msg)


def parse_clap_times(v: Any) -> dict[str, float]:
    """{"Player1": 4.04} or "Player1=4.04, Player2=4.05" -> {"Player1": 4.04, ...}."""
    if isinstance(v, str):
        pairs = []
        for item in re.split(r"[,;\n]", v):
            if not item.strip():
                continue
            if "=" not in item:
                raise _bad(f"Bad clap time '{item.strip()}'. Use Player1=4.04")
            k, val = item.split("=", 1)
            pairs.append((k, val))
    elif isinstance(v, dict):
        pairs = list(v.items())
    else:
        raise _bad("clap_times must be like Player1=4.04, Player2=4.05")
    out: dict[str, float] = {}
    for k, val in pairs:
        k = str(k).strip()
        if not NAME_RE.match(k):
            raise _bad(f"Bad track name '{k}' in clap_times")
        try:
            sec = float(str(val).strip())
        except ValueError:
            raise _bad(f"Clap time for {k} must be a number of seconds (use a dot: 4.04)")
        if not (0.0 <= sec <= 24 * 3600):
            raise _bad(f"Clap time for {k} must be 0 or more seconds")
        out[k] = sec
    return out


def check_clap_times(options: dict, names: list[str]) -> None:
    """With sync_clap=manual every track needs a clap time, and no unknown names."""
    if options.get("sync_clap") != "manual":
        return
    times = options.get("clap_times") or {}
    missing = [n for n in names if n not in times]
    extra = [n for n in times if n not in names]
    if missing:
        raise _bad(f"Clap time missing for: {', '.join(missing)}")
    if extra:
        raise _bad(f"Clap times given for tracks that aren't in this job: {', '.join(extra)}")


def validate_options(raw: dict | None) -> dict:
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        if v is None or v == "":
            continue
        if k not in OPTION_SPEC:
            raise _bad(f"Unknown option: {k}")
        typ, lo, hi = OPTION_SPEC[k]
        try:
            if typ is bool:
                v = v if isinstance(v, bool) else str(v).lower() in {"1", "true", "yes", "on"}
            elif typ is dict:
                v = parse_clap_times(v)
            elif typ is list:
                if isinstance(v, str):
                    v = [x.strip() for x in re.split(r"[,\n]", v) if x.strip()]
                if not isinstance(v, list):
                    raise ValueError
                v = [str(x) for x in v]
            else:
                v = typ(v)
        except (TypeError, ValueError):
            raise _bad(f"Option {k} must be {typ.__name__}")
        if lo is not None and not (lo <= v <= hi):
            raise _bad(f"Option {k} must be between {lo} and {hi}")
        out[k] = v
    if "whisper_model" in out and out["whisper_model"] not in WHISPER_MODELS:
        raise _bad(f"Unknown whisper_model. Choose one of: {', '.join(sorted(WHISPER_MODELS))}")
    if "language" in out and not re.fullmatch(r"[a-z]{2,3}", out["language"]):
        raise _bad("language must be a 2-3 letter code such as 'en'")
    if "mode" in out and out["mode"] not in MODES:
        raise _bad("mode must be 'lavs' or 'single'")
    if "sync_clap" in out and out["sync_clap"] not in SYNC_CLAP:
        raise _bad(f"sync_clap must be one of: {', '.join(sorted(SYNC_CLAP))}")
    if out.get("sync_clap") == "manual" and not out.get("clap_times"):
        raise _bad("sync_clap=manual needs clap_times, e.g. Player1=4.04, Player2=4.05")
    if out.get("sync_clap") != "manual":
        out.pop("clap_times", None)
    if out.get("diarize") and out.get("mode") != "single":
        raise _bad("diarize is only available for single-mic jobs (mode=single)")
    if out.get("diarize") and not settings.hf_token:
        raise _bad("Diarization isn't set up on this server (HF_TOKEN is empty in .env)")
    if not out.get("diarize"):
        out.pop("min_speakers", None)
        out.pop("max_speakers", None)
    if out.get("min_speakers") and out.get("max_speakers") and out["min_speakers"] > out["max_speakers"]:
        raise _bad("min_speakers can't be larger than max_speakers")
    for item in out.get("enrollment", []):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+=[0-9:.]+-[0-9:.]+", item):
            raise _bad(f"Bad enrollment item '{item}'. Use Player1=00:00:10-00:00:35")
    return out


def validate_files(files: list[dict]) -> list[dict]:
    if not files:
        raise _bad("At least one file is required")
    if len(files) > 32:
        raise _bad("Too many files (max 32)")
    seen: set[str] = set()
    out = []
    max_bytes = int(settings.max_file_gb * 1024 ** 3)
    for f in files:
        name = str(f.get("name", "")).strip()
        filename = str(f.get("filename", "")).strip()
        try:
            size = int(f.get("size", -1))
        except (TypeError, ValueError):
            size = -1
        if not NAME_RE.match(name):
            raise _bad(f"Bad speaker/file name '{name}' (letters, digits, _ . - only)")
        if name.lower() in seen:
            raise _bad(f"Duplicate name '{name}'")
        seen.add(name.lower())
        ext = Path(filename).suffix.lower()
        if ext not in AUDIO_EXT:
            raise _bad(f"{filename}: unsupported audio type (use {', '.join(sorted(AUDIO_EXT))})")
        if size <= 0 or size > max_bytes:
            raise _bad(f"{filename}: size must be between 1 byte and {settings.max_file_gb} GB")
        sha = str(f.get("sha256") or "").lower().strip()
        if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise _bad(f"{filename}: sha256 must be 64 hex characters")
        out.append({"name": name, "filename": filename, "ext": ext, "size": size, "sha256": sha or None})
    return out


def create_job(*, title: str, session_date: str, files: list[dict], synced: bool, options: dict,
               source: str, submitted_by: str, client_ref: str | None, can_publish: bool = True,
               owner: str | None = None) -> dict:
    try:
        date.fromisoformat(session_date)
    except ValueError:
        raise _bad("session_date must be YYYY-MM-DD")
    files = validate_files(files)
    options = validate_options(options)
    if not can_publish:
        # The submitter's account may not publish: results stay on the server for download.
        options["publish"] = False
        options["publish_blocked"] = True   # internal; clients can't set it (validate_options rejects it)
    if options.get("mode") == "single" and len(files) != 1:
        raise _bad("A single-mic job takes exactly one audio file")
    if not synced:
        check_clap_times(options, [f["name"] for f in files])
    job_id = time.strftime("%Y%m%d") + "-" + secrets.token_hex(3)
    title = (title or f"Session {session_date}").strip()[:200]
    t = db.now()
    db.insert_job({
        "id": job_id, "title": title, "session_date": session_date,
        "client_ref": (client_ref or "")[:200] or None, "source": source, "submitted_by": submitted_by,
        "owner": owner,
        "synced": int(bool(synced)), "options": options, "files": files, "status": "uploading",
        "stage": "upload", "percent": 0.0, "message": "Waiting for files", "created_at": t, "updated_at": t,
    })
    input_dir(job_id).mkdir(parents=True, exist_ok=True)
    return db.get_job(job_id)  # type: ignore[return-value]


def require_job(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9]{8}-[0-9a-f]{6}", job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def file_entry(job: dict, name: str) -> dict:
    for f in job["files"]:
        if f["name"] == name:
            return f
    raise HTTPException(status_code=404, detail=f"No file named '{name}' in this job")


def upload_paths(job_id: str, f: dict) -> tuple[Path, Path]:
    final = input_dir(job_id) / f"{f['name']}{f['ext']}"
    return final, final.with_name(final.name + ".part")


def received_bytes(job_id: str, f: dict) -> int:
    final, part = upload_paths(job_id, f)
    if final.exists():
        return final.stat().st_size
    if part.exists():
        return part.stat().st_size
    return 0


def is_complete(job_id: str, f: dict) -> bool:
    """Only a verified, renamed file counts as complete (never a .part)."""
    final, _ = upload_paths(job_id, f)
    return final.exists()


def finalize_part(job_id: str, f: dict) -> bool:
    """If the .part file has all declared bytes, verify its checksum and rename it.

    Returns True when the final file exists afterwards. On checksum mismatch the .part is
    deleted and ValueError is raised. Blocking: call from a worker thread.
    """
    import hashlib
    final, part = upload_paths(job_id, f)
    if final.exists():
        return True
    if not part.exists() or part.stat().st_size < f["size"]:
        return False
    if f.get("sha256"):
        h = hashlib.sha256()
        with open(part, "rb") as fh:
            for blk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                h.update(blk)
        if h.hexdigest() != f["sha256"]:
            part.unlink(missing_ok=True)
            raise ValueError(f"Checksum mismatch for {f['name']}; upload restarted from 0")
    part.rename(final)
    return True


def file_status(job: dict) -> list[dict]:
    out = []
    for f in job["files"]:
        if job["status"] in ACTIVE | WAITING | {"failed", "cancelled"}:
            got, done = received_bytes(job["id"], f), is_complete(job["id"], f)
        else:
            got, done = f["size"], True
        out.append({"name": f["name"], "filename": f["filename"], "size": f["size"], "received": got,
                    "complete": done})
    return out


def public_job(job: dict, include_log: bool = True, viewer: Any = None) -> dict:
    """Job as the API/pages show it. With a viewer (security.Principal) that may not see this job's
    output, everything but its progress is left out: no result files, results, clap details or log."""
    see = viewer is None or viewer.can_see_output(job)
    manage = viewer is None or viewer.can_manage(job)
    d = {k: job[k] for k in ("id", "title", "session_date", "client_ref", "source", "submitted_by", "status",
                             "stage", "percent", "message", "error", "attempts", "created_at", "updated_at",
                             "started_at", "finished_at")}
    d["owner"] = job.get("owner")
    d["synced"] = bool(job["synced"])
    d["options"] = job["options"]
    d["files"] = file_status(job)
    d["can_see_output"] = see
    d["can_manage"] = manage
    d["can_resync"] = can_resync(job) and manage
    if see:
        d["results"] = job["results"]
        d["final_files"] = [p.name for p in final_dir(job["id"]).glob("*") if p.is_file()] if final_dir(job["id"]).exists() else []
        if include_log:
            d["log_tail"] = read_log_tail(job)
    else:
        d["results"] = {}
        d["final_files"] = []
        d["error"] = "Failed" if job.get("error") else None   # details can include file names; the owner sees them
        if include_log:
            d["log_tail"] = ""
    return d


def read_log_tail(job: dict, lines: int = 60) -> str:
    p = log_path(job["id"])
    if p.exists():
        try:
            with open(p, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 64 * 1024))
                txt = fh.read().decode("utf-8", errors="replace")
            return "\n".join(txt.splitlines()[-lines:])
        except OSError:
            pass
    return job.get("log_tail") or ""


def retry(job: dict, clean: bool) -> None:
    if job["status"] not in {"failed", "cancelled", "done"}:
        raise HTTPException(status_code=409, detail=f"Cannot retry a job that is {job['status']}")
    if job["status"] == "done" or not input_dir(job["id"]).exists() or not any(input_dir(job["id"]).iterdir()):
        raise HTTPException(status_code=409, detail="The uploaded audio for this job has been cleaned up; submit it again from the recorder")
    if clean and work_dir(job["id"]).exists():
        shutil.rmtree(work_dir(job["id"]), ignore_errors=True)
    db.update_job(job["id"], status="queued", stage="queued", percent=0.0, message="Queued (retry)",
                  error=None, cancel_requested=0, finished_at=None)


def can_resync(job: dict) -> bool:
    """A raw multi-track job whose uploaded audio is still on the server and isn't being processed."""
    return (not job["synced"] and job["options"].get("mode") != "single" and len(job["files"]) > 1
            and job["status"] in WAITING | {"failed", "cancelled"}
            and input_dir(job["id"]).exists() and any(input_dir(job["id"]).iterdir()))


def resync(job: dict, clap: str, clap_times: Any = None) -> None:
    """Choose how to use the clap (clap | none | manual | auto) and queue the job again.

    Only the sync stage and everything after it run again; the upload is reused."""
    if not can_resync(job):
        if job["synced"] or job["options"].get("mode") == "single" or len(job["files"]) < 2:
            raise HTTPException(status_code=409, detail="Only raw multi-track uploads are synced on the server")
        if job["status"] not in WAITING | {"failed", "cancelled"}:
            raise HTTPException(status_code=409, detail=f"Cannot re-sync a job that is {job['status']}")
        raise HTTPException(status_code=409, detail="The uploaded audio for this job has been cleaned up; upload it again")
    if clap not in {"clap", "none", "manual", "auto"}:
        raise _bad("clap must be clap, none, manual or auto")
    opts = dict(job["options"])
    opts["sync_clap"] = clap
    opts.pop("clap_times", None)
    if clap == "manual":
        opts["clap_times"] = parse_clap_times(clap_times if clap_times is not None else "")
        check_clap_times(opts, [f["name"] for f in job["files"]])
    db.update_job(job["id"], options=opts, status="queued", stage="queued", percent=0.0,
                  message="Queued (re-sync)", error=None, cancel_requested=0, finished_at=None)


def cancel(job: dict) -> None:
    if job["status"] == "running":
        db.update_job(job["id"], cancel_requested=1, message="Cancelling...")
    elif job["status"] in {"uploading", "queued"} | WAITING:
        db.update_job(job["id"], status="cancelled", stage="cancelled", message="Cancelled", finished_at=db.now())
    else:
        raise HTTPException(status_code=409, detail=f"Job is already {job['status']}")


def delete(job: dict) -> None:
    if job["status"] == "running":
        raise HTTPException(status_code=409, detail="Cancel the job before deleting it")
    shutil.rmtree(job_dir(job["id"]), ignore_errors=True)
    db.delete_job(job["id"])


def cleanup_success(job_id: str) -> None:
    """Keep only final/ (and optionally the log) once a job has succeeded."""
    shutil.rmtree(input_dir(job_id), ignore_errors=True)
    shutil.rmtree(work_dir(job_id), ignore_errors=True)
    (job_dir(job_id) / "runner.lock").unlink(missing_ok=True)
    if not settings.keep_log_on_success:
        try:
            log_path(job_id).unlink()
        except FileNotFoundError:
            pass


def purge_expired() -> int:
    """Delete finished jobs older than RESULT_RETENTION_DAYS (0 = keep forever) and stale uploads (7 days)."""
    n = 0
    t = db.now()
    for job in db.list_jobs(limit=10000):
        age_days = (t - (job["finished_at"] or job["updated_at"])) / 86400
        if job["status"] == "done" and settings.result_retention_days > 0 and age_days > settings.result_retention_days:
            delete(job); n += 1
        elif job["status"] == "uploading" and age_days > 7:
            delete(job); n += 1
    return n
