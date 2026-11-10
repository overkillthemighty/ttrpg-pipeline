"""Runs one job end to end. Invoked by the worker as: python -m app.pipeline.runner <job_id>

Stages (each caches its output under work/, so Retry resumes after the last good stage):
  validate -> sync (raw uploads only) -> preprocess -> mix -> transcribe -> assign -> publish -> cleanup

A raw upload can pause after the clap search (status "review", see jobs.resync); the job page then
asks how to sync and queues the job again.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

from .. import db, jobs
from ..config import settings
from . import assign, audio, nextcloud, single, transcribe

# (start %, end %) of overall progress per stage
STAGES = {
    "validate": (0, 2), "sync": (2, 6), "preprocess": (6, 10), "mix": (10, 11),
    "transcribe": (11, 93), "assign": (93, 96), "publish": (96, 100),
}


class Runner:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.job = jobs.require_job(job_id)
        self.opts = self.job["options"]
        self.work = jobs.work_dir(job_id)
        self.final = jobs.final_dir(job_id)
        self.work.mkdir(parents=True, exist_ok=True)
        self.final.mkdir(parents=True, exist_ok=True)
        self.log_fh = open(jobs.log_path(job_id), "a", encoding="utf-8")
        self._last_db = 0.0
        self._stage = "validate"
        self.sync_report: dict | None = None
        self.sync_sig: str | None = None   # changes whenever the synced audio changes

    # ------------------------------------------------------------ logging / progress
    def log(self, msg: str) -> None:
        for line in str(msg).splitlines() or [""]:
            self.log_fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
        self.log_fh.flush()

    def progress(self, stage: str, frac: float, message: str, force: bool = False) -> None:
        a, b = STAGES[stage]
        pct = round(a + (b - a) * max(0.0, min(1.0, frac)), 1)
        now = time.time()
        if force or now - self._last_db >= 1.0:
            db.update_job(self.job_id, stage=stage, percent=pct, message=message)
            self._last_db = now

    def begin(self, stage: str, message: str) -> None:
        self._stage = stage
        self.log(f"== {stage}: {message}")
        self.progress(stage, 0.0, message, force=True)

    # ------------------------------------------------------------ stage cache
    def _marker(self, stage: str) -> Path:
        return self.work / f".done_{stage}.json"

    def cached(self, stage: str, params: dict) -> dict | None:
        m = self._marker(stage)
        if m.exists():
            try:
                data = json.loads(m.read_text())
                if data.get("params") == params:
                    self.log(f"== {stage}: reusing result from an earlier attempt")
                    return data.get("result", {})
            except json.JSONDecodeError:
                pass
        return None

    def mark(self, stage: str, params: dict, result: dict | None = None) -> None:
        self._marker(stage).write_text(json.dumps({"params": params, "result": result or {}}))

    # ------------------------------------------------------------ pipeline
    def inputs(self) -> dict[str, Path]:
        out = {}
        for f in self.job["files"]:
            p, _ = jobs.upload_paths(self.job_id, f)
            if not p.exists():
                raise RuntimeError(f"Uploaded file for {f['name']} is missing; submit the session again")
            out[f["name"]] = p
        return out

    def run(self) -> bool:
        """Process the job. Returns False if it paused to ask about the clap."""
        job = self.job
        self.log(f"Job {self.job_id}: '{job['title']}' session {job['session_date']}, "
                 f"{len(job['files'])} tracks, {'synced' if job['synced'] else 'raw'} upload, options {json.dumps(self.opts)}")

        # validate
        self.begin("validate", "Checking uploaded audio")
        src = self.inputs()
        durations = {n: audio.duration(p) for n, p in src.items()}
        for n, d in durations.items():
            self.log(f"{n}: {d / 60:.1f} min")
        if max(durations.values()) - min(durations.values()) > 120:
            self.log("WARNING: track lengths differ by more than 2 minutes")

        is_single = self.opts.get("mode") == "single"
        if is_single:
            self.log("Single-mic job: no lav sync or lav-based speaker assignment"
                     + ("; speakers will be guessed by diarization" if self.opts.get("diarize") else ""))

        # sync (raw multi-track uploads only)
        if job["synced"] or is_single:
            synced = src
        else:
            synced = self._sync(src)
            if synced is None:
                return False

        # preprocess
        pre_dir = self.work / "pre"
        pre_dir.mkdir(exist_ok=True)
        pre = {n: pre_dir / f"lav_{n}.wav" for n in synced}
        params = self._with_sync({"files": sorted(synced)})
        if self.cached("preprocess", params) is None:
            self.begin("preprocess", "Cleaning up and normalising each mic")
            for i, (n, p) in enumerate(synced.items()):
                self.progress("preprocess", i / len(synced), f"Preprocessing {n}")
                audio.preprocess(p, pre[n], self.log)
            self.mark("preprocess", params)

        # mix
        mix_path = self.work / "lav_mix.wav"
        if is_single:
            mix_path = next(iter(pre.values()))  # nothing to mix
        elif self.cached("mix", self._with_sync({"files": sorted(pre)})) is None:
            self.begin("mix", "Mixing all mics for transcription")
            audio.mix(list(pre.values()), mix_path, self.log)
            self.mark("mix", self._with_sync({"files": sorted(pre)}))

        # transcribe
        device, compute_type = transcribe.pick_device(settings.device, settings.compute_type)
        model = self.opts.get("whisper_model") or (settings.whisper_model if device == "cuda" else settings.whisper_model_cpu)
        diarize = bool(is_single and self.opts.get("diarize"))
        params = self._with_sync({"model": model, "language": self.opts.get("language", settings.language)})
        if diarize:
            params.update(diarize=True, min_speakers=self.opts.get("min_speakers"),
                          max_speakers=self.opts.get("max_speakers"), diarize_model=settings.diarize_model)
        trn_dir = self.work / "transcript"
        cached = self.cached("transcribe", params)
        if cached is None:
            self.begin("transcribe", f"Transcribing with WhisperX {model} on {device}")
            t0 = time.time()
            path = transcribe.transcribe(
                mix_path, trn_dir, model=model, language=params["language"], device=device,
                compute_type=compute_type, batch_size=int(self.opts.get("batch_size", settings.batch_size)),
                threads=settings.threads, log=self.log,
                progress=lambda f, m: self.progress("transcribe", f, m),
                diarize=diarize, min_speakers=self.opts.get("min_speakers"), max_speakers=self.opts.get("max_speakers"),
                diarize_model=settings.diarize_model, hf_token=settings.hf_token)
            self.log(f"Transcription took {(time.time() - t0) / 60:.1f} min")
            self.mark("transcribe", params, {"path": str(path)})
        else:
            path = Path(cached["path"])

        # assign speakers (always re-run: it's fast and options may have changed)
        self.begin("assign", "Assigning speakers")
        if is_single:
            summary = single.run(
                str(path), str(self.final / "transcript_lav_assigned.json"), str(self.final / "transcript_lav_assigned.csv"),
                str(self.final / "transcript_lav_assigned.md"), diarized=diarize,
                title=f"TTRPG Transcript ({job['session_date']})", log=self.log)
        else:
            summary = self._assign_lavs(path, synced, job)
        audio.encode_flac(mix_path, self.final / "lav_mix.flac", self.log)
        self.log(f"Speaker turns: {summary['turns']}; labels: {json.dumps(summary['labels'])}")
        results = {"labels": summary["labels"], "turns": summary["turns"], "model": model, "device": device,
                   "minutes": round(max(durations.values()) / 60, 1), "mode": "single" if is_single else "lavs",
                   "diarized": diarize}
        if self.sync_report:
            results["sync"] = self.sync_report
        self._publish(job, results)
        return True

    # ------------------------------------------------------------ clap sync
    def _with_sync(self, params: dict) -> dict:
        """Cache params of the stages after sync: a different sync must redo them."""
        return {**params, "sync": self.sync_sig} if self.sync_sig else params

    def _sync(self, src: dict[str, Path]) -> dict[str, Path] | None:
        """Line the raw tracks up. Returns the synced files, or None if the job paused to ask."""
        o = self.opts
        mode = o.get("sync_clap", "auto")
        max_spread = float(o.get("sync_max_spread_seconds", 1.0))
        params = {"start": o.get("sync_search_start_seconds", 0.0), "window": o.get("sync_window_seconds", 10.0),
                  "files": sorted(src), "clap": mode, "max_spread": max_spread}
        if mode == "manual":
            params["clap_times"] = o["clap_times"]
        sync_dir = self.work / "synced"
        report = self.cached("sync", params)
        if report is None:
            self.begin("sync", "Finding the clap and aligning tracks")
            report = {"mode": mode, "max_spread_seconds": max_spread}
            clap: dict[str, float] | None = None
            if len(src) < 2:
                report["used"] = "single track"
            elif mode == "manual":
                clap = {n: float(o["clap_times"][n]) for n in src}
                report.update(used="manual", clap_seconds=clap)
                self.log("Using the clap times given: " + ", ".join(f"{n} {t:.3f}s" for n, t in clap.items()))
            elif mode == "none":
                report["used"] = "none"
                self.log("Ignoring the clap: keeping the tracks as recorded")
            else:
                found = audio.find_claps(src, params["start"], params["window"], self.log)
                lines_up = found["spread_seconds"] <= max_spread
                report.update(found, lines_up=lines_up)
                if not lines_up:
                    self.log(f"WARNING: clap times differ by {found['spread_seconds']:.2f}s (more than {max_spread}s), "
                             "so the clap probably wasn't found on every track")
                if mode == "ask" or (mode == "ask_if_unsure" and not lines_up):
                    report["used"] = "waiting"
                    msg = ("Check the clap" if lines_up else "The clap didn't line up") + ": choose how to sync on the job page"
                    self.log(f"== waiting: {msg}")
                    db.update_job(self.job_id, status="review", stage="sync", message=msg, results={"sync": report})
                    return None
                if mode == "clap" or lines_up:
                    clap = found["clap_seconds"]
                    report["used"] = "clap"
                else:
                    report["used"] = "none"
                    self.log("Keeping the tracks as recorded (they start together, usually within ~0.1 s)")
            report["trim_seconds"] = audio.write_synced(src, sync_dir, clap, self.log)
            self.mark("sync", params, report)
        self.sync_report = report
        self.sync_sig = json.dumps(report.get("trim_seconds", {}), sort_keys=True)
        db.update_job(self.job_id, results={"sync": report})
        return {n: sync_dir / f"synced_{n}.flac" for n in src}

    def _assign_lavs(self, path: Path, synced: dict, job: dict) -> dict:
        a_opts = assign.AssignOptions()
        for k in ("overlap_db", "label_overlaps", "switch_cost_db", "switch_cost_boundary_db", "silence_db"):
            if k in self.opts:
                setattr(a_opts, k, self.opts[k])
        for item in self.opts.get("enrollment", []):
            spk, rng = item.split("=", 1)
            t0s, t1s = rng.split("-", 1)
            a_opts.enrollment[spk.strip()] = (_parse_time(t0s), _parse_time(t1s))
        return assign.run(
            str(path), {n: str(p) for n, p in synced.items()},
            str(self.final / "transcript_lav_assigned.json"), str(self.final / "transcript_lav_assigned.csv"),
            str(self.final / "transcript_lav_assigned.md"), opts=a_opts,
            title=f"TTRPG Transcript ({job['session_date']})", log=self.log)

    def _publish(self, job: dict, results: dict) -> None:
        db.update_job(self.job_id, results=results)

        # publish
        if self.opts.get("publish", True) and settings.nextcloud_enabled:
            self.begin("publish", "Uploading results to Nextcloud")
            results["nextcloud"] = nextcloud.publish(self.final, jobs.FINAL_FILES, job["session_date"], self.log)
            db.update_job(self.job_id, results=results)
        elif not settings.nextcloud_enabled:
            self.log("Nextcloud is not configured; results are only available from the dashboard")
        else:
            self.log("Not published to Nextcloud: the account that submitted this job isn't allowed to publish. "
                     "Download the results from the job page." if self.opts.get("publish_blocked")
                     else "Publishing disabled for this job")

        db.update_job(self.job_id, status="done", stage="done", percent=100.0, message="Complete",
                      finished_at=db.now(), error=None)
        self.log("Job complete")
        nextcloud.notify(f"Transcript ready: {job['title']}",
                         f"Session {job['session_date']} finished ({results['turns']} speaker turns).", self.log)

    def finish(self) -> None:
        tail = jobs.read_log_tail(self.job)
        db.update_job(self.job_id, log_tail=tail)
        self.log_fh.close()


def _parse_time(v: str) -> float:
    parts = [float(x) for x in v.strip().split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def main(job_id: str) -> int:
    # Only one runner per job, ever (guards against an orphaned runner from a previous server process).
    jobs.job_dir(job_id).mkdir(parents=True, exist_ok=True)
    lock = open(jobs.job_dir(job_id) / "runner.lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"Job {job_id} is already being processed by another runner", file=sys.stderr)
        return 3
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    r = Runner(job_id)
    try:
        finished = r.run()
        r.finish()
        if finished:
            jobs.cleanup_success(job_id)
        else:  # waiting for a clap decision: keep the upload and work files
            (jobs.job_dir(job_id) / "runner.lock").unlink(missing_ok=True)
        return 0
    except Exception as e:
        r.log("ERROR: " + "".join(traceback.format_exception(e)))
        cur = db.get_job(job_id)
        if cur and not cur["cancel_requested"]:
            db.update_job(job_id, status="failed", message=f"Failed during {r._stage}", error=str(e),
                          finished_at=db.now())
            nextcloud.notify(f"Transcription failed: {r.job['title']}", f"{r._stage}: {e}", r.log)
        r.finish()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
