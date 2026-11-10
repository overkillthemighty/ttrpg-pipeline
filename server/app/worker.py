"""Background worker: runs one job at a time in a child process.

Running each job in its own process means a crash or out-of-memory kill inside
WhisperX can't take the web dashboard down, and Cancel can stop it cleanly.
"""
from __future__ import annotations

import fcntl
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from . import db, jobs, recordings

log = logging.getLogger("ttrpg.worker")


class Worker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: str | None = None
        self._proc: subprocess.Popen | None = None
        self._shutting_down = False
        self._last_purge = 0.0

    def start(self) -> None:
        self._recover()
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Server shutdown (e.g. an update): stop the running job and put it back in the queue.

        Finished stages are cached, so when the server is back the job resumes from the last
        completed stage (a transcription that was cut off starts that stage again)."""
        self._shutting_down = True
        self._stop.set()
        proc, job_id = self._proc, self.current
        if proc and proc.poll() is None:
            log.info("Server stopping: interrupting job %s", job_id)
            self._kill(proc)
        if self._thread:
            self._thread.join(timeout=10)
        if job_id:
            self._requeue(job_id, "Interrupted by a server restart or update; it continues automatically")

    @staticmethod
    def _requeue(job_id: str, message: str) -> None:
        with db.connect() as c:
            c.execute("UPDATE jobs SET status='queued', stage='queued', percent=0, message=?, cancel_requested=0, "
                      "updated_at=? WHERE id=? AND status='running'", (message, db.now(), job_id))

    def _recover(self) -> None:
        """Jobs that were running when the server stopped go back to the queue."""
        for job in db.list_jobs(limit=10000):
            if job["status"] == "running":
                self._kill_orphan(job["id"])
                self._requeue(job["id"], "Interrupted by a server restart or update; it continues automatically")
                log.info("Re-queued %s after restart", job["id"])

    @staticmethod
    def _kill_orphan(job_id: str) -> None:
        """If a runner from a previous server process still holds the job lock, stop it."""
        path = jobs.job_dir(job_id) / "runner.lock"
        if not path.exists():
            return
        with open(path, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
                return  # nobody holds it
            except BlockingIOError:
                pass
            fh.seek(0)
            try:
                pid = int(fh.read().strip() or 0)
            except ValueError:
                pid = 0
        if pid > 0:
            log.warning("Stopping orphaned runner %s for job %s", pid, job_id)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except ProcessLookupError:
                    return
                for _ in range(20):
                    time.sleep(0.5)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return

    def _next(self) -> dict | None:
        with db.connect() as c:
            r = c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        return db.get_job(r["id"]) if r else None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if time.time() - self._last_purge > 3600:
                    self._last_purge = time.time()
                    n = jobs.purge_expired()
                    if n:
                        log.info("Purged %d expired jobs", n)
                    n = recordings.purge()
                    if n:
                        log.info("Removed %d old or abandoned browser recordings", n)
                job = self._next()
                if not job:
                    self._stop.wait(2.0)
                    continue
                self._run(job)
            except Exception:  # never let the worker thread die
                log.exception("Worker loop error")
                self._stop.wait(5.0)

    def _run(self, job: dict) -> None:
        job_id = job["id"]
        if self._shutting_down:
            return
        with db.connect() as c:  # claim it only if it is still queued (Cancel/Delete may have raced us)
            cur = c.execute("UPDATE jobs SET status='running', stage='starting', percent=0, message='Starting', "
                            "started_at=?, attempts=attempts+1, error=NULL, updated_at=? WHERE id=? AND status='queued'",
                            (db.now(), db.now(), job_id))
            if cur.rowcount != 1:
                return
        self.current = job_id
        jobs.job_dir(job_id).mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "app.pipeline.runner", job_id]
        log.info("Starting job %s", job_id)
        proc = subprocess.Popen(cmd, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                start_new_session=True)
        self._proc = proc
        cancelled = False
        while proc.poll() is None:
            time.sleep(1.0)
            cur = db.get_job(job_id)
            if cur is None or cur["cancel_requested"]:
                cancelled = True
                self._kill(proc)
                break
        rc = proc.wait()
        self._proc = None
        cur = db.get_job(job_id)
        if cur is None:  # deleted while running
            pass
        elif self._shutting_down and cur["status"] == "running" and not cancelled:
            pass  # stop() puts it back in the queue
        elif cancelled:
            with db.connect() as c:  # don't overwrite a job that finished just before the cancel landed
                c.execute("UPDATE jobs SET status='cancelled', stage='cancelled', message='Cancelled by user', "
                          "cancel_requested=0, finished_at=?, updated_at=? WHERE id=? AND status='running'",
                          (db.now(), db.now(), job_id))
            db.update_job(job_id, cancel_requested=0)
        elif cur["status"] == "running":
            # The runner always records its own outcome; reaching here means it died hard.
            db.update_job(job_id, status="failed", stage=cur["stage"],
                          error=f"Processing stopped unexpectedly (exit code {rc}). "
                                "This is usually an out-of-memory kill; try a smaller Whisper model or batch size.",
                          message="Failed", finished_at=db.now())
        log.info("Job %s finished with exit code %s", job_id, rc)
        self.current = None

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


worker = Worker()
