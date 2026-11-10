"""Browser recordings (the dashboard's Record page), uploaded in small chunks while recording.

Two modes:
  single  one microphone (the room mic): a room-mic-only job.
  lavs    one or more lav receivers, each delivering 1-2 channels (one player per channel),
          plus nothing else: a normal lav job with the player names chosen on the page.

The browser keeps every chunk in IndexedDB until the server has it, so a dropped connection only
delays the upload and a crashed or closed tab can be resumed as a new "part". All mics of a part
start together; if one fails, the browser restarts all of them as the next part.
The recording id is made by the browser, so recording can start while the server is unreachable.

On-disk layout (DATA_DIR/recordings/<id>/):
    meta.json                     title, date, mode, layout, options, parts, status, job id
    p01/<stream>/000001.chunk     chunks of one mic (stream) in part 1, in order

Finishing: per part and mic, join the chunks, decode, split the channels into one mono track per
player, line the tracks up (on the clap at the start of each part when it lines up, otherwise on
the browser's start times), place the parts on one timeline by their start times (gaps become
silence) and create the job with the tracks already synced.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

from fastapi import HTTPException

from . import db, jobs
from .config import settings
from .pipeline import audio

log = logging.getLogger("ttrpg.recordings")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
STREAM_RE = re.compile(r"^[a-z0-9]{1,16}$")
MAX_PARTS = 500
MAX_CHUNKS = 100_000
MAX_STREAMS = 8
MIME_EXT = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".mp4", "audio/x-m4a": ".mp4", "video/webm": ".webm"}
UNFINISHED = {"recording", "failed"}
STALE_FINISHING_S = 30 * 60      # "finishing" left behind by a server restart counts as failed after this
CLAP_WINDOW_S = 10.0             # look for the clap in the first seconds of each part
CLAP_MAX_SPREAD_S = 1.0          # claps further apart than this are not trusted
CLAP_MODES = {"auto", "none"}    # auto: use the clap when it lines up; none: browser start times only
JOB_OPTIONS = {"whisper_model", "language", "publish", "diarize", "min_speakers", "max_speakers"}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(rid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(rid, threading.Lock())


def root() -> Path:
    return settings.data_dir / "recordings"


def rec_dir(rid: str) -> Path:
    if not ID_RE.match(rid):
        raise HTTPException(status_code=400, detail="Bad recording id")
    return root() / rid


def _meta_path(rid: str) -> Path:
    return rec_dir(rid) / "meta.json"


def read(rid: str) -> dict | None:
    p = _meta_path(rid)
    if not p.exists():
        return None
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if meta.get("status") == "finishing" and time.time() - meta.get("updated_at", 0) > STALE_FINISHING_S:
        meta["status"] = "failed"
        meta["error"] = "Finishing was interrupted (server restart?). Finish it again."
    return meta


def require(rid: str) -> dict:
    m = read(rid)
    if not m:
        raise HTTPException(status_code=404, detail="Recording not found")
    return m


def _write(meta: dict) -> None:
    meta["updated_at"] = time.time()
    p = _meta_path(meta["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _base_mime(mime: str) -> str:
    return (mime or "").split(";")[0].strip().lower()


def _bad(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail=msg)


# ----------------------------------------------------------------- layout

def validate_layout(mode: str, layout: list[dict]) -> list[dict]:
    """[{stream, device, channels, names}] -> cleaned. names[i] = player on channel i, None = unused."""
    if mode not in {"single", "lavs"}:
        raise _bad("mode must be 'single' or 'lavs'")
    if not layout or len(layout) > MAX_STREAMS:
        raise _bad(f"Choose 1-{MAX_STREAMS} microphones")
    out, keys, names = [], set(), set()
    for s in layout:
        key = str(s.get("stream", ""))
        if not STREAM_RE.match(key) or key in keys:
            raise _bad(f"Bad or duplicate stream id '{key}'")
        keys.add(key)
        try:
            ch = int(s.get("channels", 1))
        except (TypeError, ValueError):
            ch = 0
        if ch not in (1, 2):
            raise _bad("Each microphone has 1 or 2 channels")
        raw = list(s.get("names") or [])[:ch]
        raw += [None] * (ch - len(raw))
        clean = []
        for n in raw:
            if n in (None, ""):
                clean.append(None)
                continue
            n = str(n).strip()
            if not jobs.NAME_RE.match(n):
                raise _bad(f"Player name '{n}': use letters, digits, _ . - (no spaces), up to 64 characters")
            if n.lower() in names:
                raise _bad(f"Player name '{n}' is used twice")
            names.add(n.lower())
            clean.append(n)
        out.append({"stream": key, "device": str(s.get("device") or "")[:200], "channels": ch, "names": clean})
    if not names:
        raise _bad("At least one microphone channel must have a player")
    if mode == "single" and (len(out) != 1 or sum(1 for n in out[0]["names"] if n) != 1):
        raise _bad("Room-mic-only recordings use exactly one microphone")
    return out


def track_names(meta: dict) -> list[str]:
    return [n for s in meta["layout"] for n in s["names"] if n]


# ----------------------------------------------------------------- create / chunks

def upsert(rid: str, *, title: str, session_date: str, mode: str, layout: list[dict], clap: str,
           options: dict, created_by: str, owner: str | None = None) -> dict:
    """Create the recording, or update its title/date/options (idempotent: the browser may retry).

    The layout (which mic carries which players) is fixed once audio has arrived."""
    try:
        date.fromisoformat(session_date)
    except ValueError:
        raise _bad("session_date must be YYYY-MM-DD")
    layout = validate_layout(mode, layout)
    if clap not in CLAP_MODES:
        raise _bad("clap must be 'auto' or 'none'")
    opts = {k: v for k, v in (options or {}).items() if k in JOB_OPTIONS}
    if mode == "single":
        opts["mode"] = "single"
    else:
        for k in ("diarize", "min_speakers", "max_speakers"):
            opts.pop(k, None)
    opts = jobs.validate_options(opts)
    with _lock(rid):
        meta = read(rid)
        if meta and meta["status"] == "done":
            return meta
        if not meta:
            meta = {"id": rid, "created_at": time.time(), "created_by": created_by, "owner": owner,
                    "status": "recording", "parts": {}, "job_id": None, "error": None}
        if meta.get("parts") and meta.get("layout") and (meta.get("mode") != mode or meta["layout"] != layout):
            # Player names may still change; which mics and channels are recorded may not.
            same_shape = meta.get("mode") == mode and [(s["stream"], s["channels"]) for s in meta["layout"]] == \
                [(s["stream"], s["channels"]) for s in layout]
            if not same_shape:
                raise HTTPException(status_code=409, detail="This recording already has audio for a different set of microphones")
        meta.update(title=(title or f"Session {session_date}").strip()[:200], session_date=session_date,
                    mode=mode, layout=layout, clap=clap, options=opts)
        if meta["status"] == "failed":
            meta["status"] = "recording"
        _write(meta)
        return meta


def _stream_dir(rid: str, part: int, stream: str) -> Path:
    return rec_dir(rid) / f"p{part:02d}" / stream


def put_chunk(rid: str, part: int, stream: str, seq: int, data: bytes, *, started_at_ms: int, mime: str) -> dict:
    """Store chunk <seq> of one mic in <part>. Re-sending a chunk the server already has is harmless."""
    if not (1 <= part <= MAX_PARTS and 1 <= seq <= MAX_CHUNKS) or not STREAM_RE.match(stream):
        raise _bad("Bad part, stream or chunk number")
    if _base_mime(mime) not in MIME_EXT:
        raise _bad(f"Unsupported recording format: {mime}")
    if not data:
        raise _bad("Empty chunk")
    meta = require(rid)
    if meta["status"] in {"finishing", "done"}:
        raise HTTPException(status_code=409, detail=f"Recording is {meta['status']}; no more chunks accepted")
    if stream not in {s["stream"] for s in meta.get("layout", [])}:
        raise _bad(f"Stream '{stream}' isn't part of this recording")
    info = meta["parts"].get(str(part), {}).get("streams", {}).get(stream)
    if info is None:
        with _lock(rid):
            meta = require(rid)
            p = meta["parts"].setdefault(str(part), {"streams": {}})
            p["streams"].setdefault(stream, {"started_at_ms": int(started_at_ms), "mime": mime})
            _write(meta)
    elif _base_mime(info["mime"]) != _base_mime(mime):
        raise HTTPException(status_code=409, detail="Chunk format differs from the rest of this stream")
    d = _stream_dir(rid, part, stream)
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{seq:06d}.chunk"
    if dst.exists() and dst.stat().st_size == len(data):
        return {"stored": False, "size": len(data)}
    tmp = d / f"{seq:06d}.tmp{threading.get_ident()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, dst)
    with _lock(rid):
        meta = require(rid)
        meta["last_chunk_at"] = time.time()
        _write(meta)
    return {"stored": True, "size": len(data)}


def _chunks(rid: str, part: int, stream: str) -> list[Path]:
    d = _stream_dir(rid, part, stream)
    return sorted(d.glob("*.chunk")) if d.exists() else []


def public(meta: dict) -> dict:
    parts, total = [], 0
    for key in sorted(meta.get("parts", {}), key=int):
        streams = {}
        for s, info in meta["parts"][key]["streams"].items():
            files = _chunks(meta["id"], int(key), s) if meta["status"] != "done" else []
            size = sum(f.stat().st_size for f in files)
            total += size
            streams[s] = {"started_at_ms": info["started_at_ms"], "mime": info["mime"], "chunks": len(files), "bytes": size}
        parts.append({"part": int(key), "streams": streams})
    out = {k: meta.get(k) for k in ("id", "title", "session_date", "mode", "layout", "clap", "options", "status",
                                    "job_id", "error", "created_at", "updated_at", "created_by", "owner", "last_chunk_at")}
    out.update(parts=parts, bytes=total, players=track_names(meta) if meta.get("layout") else [])
    return out


def list_unfinished() -> list[dict]:
    if not root().exists():
        return []
    out = []
    for d in root().iterdir():
        if d.is_dir() and ID_RE.match(d.name):
            m = read(d.name)
            if m and m.get("layout") and m["status"] in UNFINISHED | {"finishing"}:
                out.append(public(m))
    return sorted(out, key=lambda m: m["created_at"], reverse=True)


def recover() -> int:
    """At startup: a recording left "finishing" by the previous server process can be finished again
    right away (the browser retries on its own), instead of looking busy for STALE_FINISHING_S."""
    n = 0
    if not root().exists():
        return 0
    for d in root().iterdir():
        if d.is_dir() and ID_RE.match(d.name):
            with _lock(d.name):
                m = read(d.name)
                if m and m.get("status") == "finishing":
                    m["status"] = "failed"
                    m["error"] = "Finishing was interrupted by a server restart. Finish it again."
                    _write(m)
                    n += 1
    return n


def active(window_s: float = 180.0) -> list[str]:
    """Recordings still in progress: chunks arrived in the last window_s seconds, or being finished."""
    out = []
    if not root().exists():
        return out
    now = time.time()
    for d in root().iterdir():
        if d.is_dir() and ID_RE.match(d.name):
            m = read(d.name)
            if not m:
                continue
            if m.get("status") == "finishing" or (m.get("status") == "recording"
                                                    and now - (m.get("last_chunk_at") or 0) < window_s):
                out.append(d.name)
    return out


def delete(rid: str) -> None:
    meta = require(rid)
    if meta["status"] == "finishing":
        raise HTTPException(status_code=409, detail="Recording is being finished; try again in a moment")
    shutil.rmtree(rec_dir(rid), ignore_errors=True)


# ----------------------------------------------------------------- finish

def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {p.stdout.strip()[-600:]}")


FF = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error"]
FLAC16 = ["-sample_fmt", "s16", "-c:a", "flac"]   # every piece in the same format, so they join cleanly


def _channels(path: Path) -> int:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return int(p.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 1


def _decode_tracks(meta: dict, part: int, stream: dict, work: Path) -> dict[str, Path]:
    """Join one mic's chunks for a part and split it into a 16 kHz mono FLAC per named channel."""
    files = _chunks(meta["id"], part, stream["stream"])
    if not files:
        return {}
    info = meta["parts"][str(part)]["streams"][stream["stream"]]
    joined = work / f"p{part:02d}_{stream['stream']}{MIME_EXT[_base_mime(info['mime'])]}"
    with open(joined, "wb") as out:
        for f in files:
            with open(f, "rb") as fh:
                shutil.copyfileobj(fh, out)
    tracks = {}
    have = _channels(joined)
    for ch, name in enumerate(stream["names"]):
        if not name:
            continue
        dst = work / f"p{part:02d}_{name}.flac"
        if stream["channels"] == 2 and have >= 2:
            af = ["-af", f"pan=mono|c0=c{ch}"]
        else:   # a mono device, or a receiver that fell back to mono: every player gets the same audio
            af = ["-ac", "1"]
        _run(FF + ["-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err", "-i", str(joined), "-vn",
                   *af, "-ar", "16000", *FLAC16, str(dst)])
        tracks[name] = dst
    joined.unlink(missing_ok=True)
    return tracks


def _place(pieces: list[tuple[float, Path]], out: Path, work: Path, tag: str) -> None:
    """Put audio pieces on one timeline: silence fills gaps, overlaps trim the later piece's start."""
    listed, cursor = [], 0.0
    for i, (off, path) in enumerate(sorted(pieces, key=lambda x: x[0])):
        dur = audio.duration(path)
        src = path
        if off > cursor + 0.0005:
            sil = work / f"{tag}_sil{i:03d}.flac"
            _run(FF + ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", f"{off - cursor:.6f}", *FLAC16, str(sil)])
            listed.append(sil)
            cursor = off
        elif off < cursor - 0.0005:
            cut = cursor - off
            if cut >= dur:
                continue
            src = work / f"{tag}_cut{i:03d}.flac"
            _run(FF + ["-i", str(path), "-af", f"atrim=start={cut:.6f},asetpts=PTS-STARTPTS", *FLAC16, str(src)])
            dur -= cut
        listed.append(src)
        cursor += dur
    if len(listed) == 1:
        shutil.copyfile(listed[0], out)
        return
    # Decode and join with the concat filter (not the concat demuxer, which needs identical streams).
    args = []
    for p in listed:
        args += ["-i", str(p)]
    graph = "".join(f"[{i}:a]" for i in range(len(listed))) + f"concat=n={len(listed)}:v=0:a=1[a]"
    _run(FF + args + ["-filter_complex", graph, "-map", "[a]", "-ar", "16000", "-ac", "1", *FLAC16, str(out)])


def _assemble(meta: dict, work: Path) -> tuple[dict[str, Path], list[str]]:
    """Build one synced 16 kHz mono FLAC per player. Returns (tracks, report lines for the job log)."""
    report: list[str] = []
    pieces: dict[str, list[tuple[float, Path]]] = {n: [] for n in track_names(meta)}
    first_ms = None
    prev_end = 0.0
    for key in sorted(meta["parts"], key=int):
        part = int(key)
        streams = meta["parts"][key]["streams"]
        tracks: dict[str, Path] = {}
        local: dict[str, float] = {}        # track -> offset (s) of its start within the part, from browser times
        ref_ms = min(v["started_at_ms"] for v in streams.values())
        for s in meta["layout"]:
            if s["stream"] not in streams:
                continue
            got = _decode_tracks(meta, part, s, work)
            for name, path in got.items():
                tracks[name] = path
                local[name] = (streams[s["stream"]]["started_at_ms"] - ref_ms) / 1000.0
        if not tracks:
            continue
        line = f"Part {part}: {len(tracks)} track(s)"
        if len(tracks) > 1 and meta.get("clap", "auto") == "auto":
            claps = {}
            for name, path in tracks.items():
                try:
                    t, _ = audio.detect_clap(path, 0.0, CLAP_WINDOW_S)
                    claps[name] = local[name] + t       # clap on the part's shared clock
                except RuntimeError:
                    pass
            if len(claps) == len(tracks) and max(claps.values()) - min(claps.values()) <= CLAP_MAX_SPREAD_S:
                target = min(claps.values())
                spread_ms = (max(claps.values()) - target) * 1000
                for name in tracks:
                    local[name] -= claps[name] - target
                line += f", lined up on the clap (claps were {spread_ms:.0f} ms apart by browser start times)"
            else:
                line += ", clap not found on every track or more than 1 s apart: using the browser's start times"
        elif len(tracks) > 1:
            line += ", using the browser's start times (clap ignored)"
        base = min(local.values())
        for name in local:
            local[name] -= base
        if first_ms is None:
            first_ms = ref_ms
        start = max((ref_ms - first_ms) / 1000.0, prev_end)
        if part > 1:
            line += f", placed at {start:.1f} s"
        end = start
        for name, path in tracks.items():
            pieces[name].append((start + local[name], path))
            end = max(end, start + local[name] + audio.duration(path))
        prev_end = end
        report.append(line)
    if not any(pieces.values()):
        raise HTTPException(status_code=409, detail="This recording has no audio on the server yet")
    out: dict[str, Path] = {}
    for name, ps in pieces.items():
        if not ps:
            report.append(f"WARNING: no audio for {name}")
            continue
        dst = work / f"track_{name}.flac"
        _place(ps, dst, work, f"t_{name}")
        out[name] = dst
    return out, report


def finish(rid: str, *, expected: dict[str, dict[str, int]] | None, submitted_by: str, can_publish: bool = True,
           owner: str | None = None) -> dict:
    """Check every chunk arrived, build the tracks and queue a job. Returns the job.

    expected: {part: {stream: chunks recorded}}; None = take whatever the server has
    (used when finishing from a browser that doesn't have the recording's records)."""
    with _lock(rid):
        meta = require(rid)
        if meta["status"] == "done" and meta.get("job_id") and db.get_job(meta["job_id"]):
            return db.get_job(meta["job_id"])  # already finished (e.g. the browser retried)
        if meta["status"] == "finishing":
            raise HTTPException(status_code=409, detail="Already being finished")
        if not meta.get("layout"):
            raise HTTPException(status_code=409, detail="Recording has no microphone layout yet")
        if expected:
            missing = []
            for part, streams in expected.items():
                for stream, n in (streams or {}).items():
                    have = {int(f.stem) for f in _chunks(rid, int(part), stream)} if STREAM_RE.match(stream) else set()
                    gone = sum(1 for s in range(1, int(n) + 1) if s not in have)
                    if gone:
                        missing.append(f"part {part} {stream}: {gone} chunk(s)")
            if missing:
                raise HTTPException(status_code=409, detail="Still uploading: " + "; ".join(missing))
        meta["status"] = "finishing"
        meta["error"] = None
        _write(meta)

    work = rec_dir(rid) / "work"
    try:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        tracks, report = _assemble(meta, work)
        is_single = meta["mode"] == "single"
        files = [{"name": n, "filename": f"{n}.flac", "size": p.stat().st_size} for n, p in tracks.items()]
        job = jobs.create_job(
            title=meta["title"], session_date=meta["session_date"], files=files, synced=True,
            options=meta.get("options") or ({"mode": "single"} if is_single else {}), source="browser",
            submitted_by=submitted_by, client_ref=f"browser recording {rid}", can_publish=can_publish, owner=owner)
        for f in job["files"]:
            dst, _ = jobs.upload_paths(job["id"], f)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tracks[f["name"]]), dst)
        with open(jobs.log_path(job["id"]), "a", encoding="utf-8") as lf:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            devices = "; ".join(f"{s['stream']}={s['device'] or '?'} ({', '.join(n or '-' for n in s['names'])})" for s in meta["layout"])
            lf.write(f"[{stamp}] Browser recording {rid}: {devices}\n")
            for line in report:
                lf.write(f"[{stamp}] {line}\n")
        db.update_job(job["id"], status="queued", stage="queued", message="Queued", percent=0.0)
    except Exception as e:
        with _lock(rid):
            meta = require(rid)
            meta["status"] = "failed"
            meta["error"] = getattr(e, "detail", None) or str(e)
            _write(meta)
        shutil.rmtree(work, ignore_errors=True)
        log.exception("Finishing recording %s failed", rid)
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=f"Could not build the recording: {e}")

    # The audio now lives in the job; keep only meta.json so a retried finish returns the same job.
    with _lock(rid):
        meta = require(rid)
        meta.update(status="done", job_id=job["id"], finished_at=time.time())
        _write(meta)
    for d in rec_dir(rid).iterdir():
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    return db.get_job(job["id"])


def purge(max_idle_days: float = 14.0) -> int:
    """Remove finished recordings' leftovers after a day and abandoned ones after max_idle_days."""
    if not root().exists():
        return 0
    n = 0
    now = time.time()
    for d in root().iterdir():
        if not (d.is_dir() and ID_RE.match(d.name)):
            continue
        m = read(d.name)
        age = (now - (m or {}).get("updated_at", d.stat().st_mtime)) / 86400
        if (m is None and age > 1) or (m and m["status"] == "done" and age > 1) or \
                (m and m["status"] != "finishing" and age > max_idle_days):
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n
