"""Update tracker: is this server running the newest code on GitHub?

The running commit is read once at startup from the repo's .git (mounted read-only at REPO_GIT_DIR, see
docker-compose.yml) or from APP_VERSION. Every UPDATE_CHECK_HOURS the server asks GitHub what is on the
branch after that commit. Only commits that change what goes into the image count: a docs-only push
rebuilds an identical image, Compose doesn't restart the container, and the startup commit stays old.

Admins see a banner when an update is due, and get one Nextcloud notification per new commit once it has
been waiting longer than the grace period (pushes normally deploy within minutes, once the server is idle).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger("ttrpg.updates")

# Paths (relative to the repo root) whose changes reach the running container.
IMAGE_PATHS = ("server/app/", "server/client/", "server/requirements.txt", "server/Dockerfile",
               "server/entrypoint.sh", "server/.dockerignore", "server/docker-compose.yml",
               "server/docker-compose.gpu.yml")
# A newer commit younger than this is "on its way" (CI tests, then waits for idle), not yet a warning.
GRACE_SECONDS = 30 * 60

_SHA = re.compile(r"^[0-9a-f]{40}$")


def _read_ref(git_dir: Path, ref: str) -> str:
    loose = git_dir / ref
    if loose.is_file():
        return loose.read_text().strip()
    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return ""


def read_commit(git_dir: Path) -> str:
    """The checked-out commit of a .git directory, without needing git installed."""
    try:
        head = (git_dir / "HEAD").read_text().strip()
        sha = _read_ref(git_dir, head[5:].strip()) if head.startswith("ref:") else head
    except OSError:
        return ""
    return sha if _SHA.match(sha) else ""


def _github_repo(url: str) -> str:
    m = re.match(r"^https?://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else ""


@dataclass
class Status:
    commit: str = ""                 # running commit (full sha), "" if unknown
    state: str = "unknown"           # unknown | disabled | current | pending | behind | error
    detail: str = ""
    latest: str = ""                 # newest commit on the branch
    commits: list[dict] = field(default_factory=list)   # newer commits that change the image, newest first
    waiting_since: float = 0.0       # date of the oldest of those
    pushed_at: float = 0.0           # date of the newest
    checked_at: float = 0.0

    @property
    def short(self) -> str:
        return self.commit[:7] if self.commit else "unknown"

    @property
    def needs_attention(self) -> bool:
        return self.state in {"pending", "behind"}


class Tracker:
    def __init__(self) -> None:
        self.repo = _github_repo(settings.repo_url)
        self.branch = os.environ.get("UPDATE_BRANCH", "main").strip() or "main"
        self.enabled = os.environ.get("UPDATE_CHECK", "true").strip().lower() not in {"0", "false", "no", "off"}
        try:
            self.interval = max(0.25, float(os.environ.get("UPDATE_CHECK_HOURS", "1"))) * 3600
        except ValueError:
            self.interval = 3600
        self.status = Status()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.notified_file = settings.data_dir / "update_notified"

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        env = os.environ.get("APP_VERSION", "").strip().lower()
        self.status.commit = env if _SHA.match(env) else read_commit(Path(os.environ.get("REPO_GIT_DIR", "/srv/repo.git")))
        if self.status.commit:
            log.info("Running commit %s", self.status.commit[:7])
        if not self.enabled or not self.repo:
            self.status.state = "disabled"
            self.status.detail = "Update checks are off (UPDATE_CHECK=false or REPO_URL isn't a GitHub repo)."
            return
        if not self.status.commit:
            self.status.detail = "This server doesn't know which commit it is running (see README: update check)."
            return
        self._thread = threading.Thread(target=self._loop, name="update-check", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(20):   # let startup settle first
            return
        while not self._stop.is_set():
            self.check()
            self._stop.wait(self.interval)

    # ------------------------------------------------------------ checking
    def _compare(self) -> dict:
        url = f"https://api.github.com/repos/{self.repo}/compare/{self.status.commit}...{self.branch}"
        r = httpx.get(url, timeout=20, headers={"Accept": "application/vnd.github+json",
                                                "User-Agent": "ttrpg-pipeline-update-check"})
        if r.status_code == 404:
            raise LookupError(f"GitHub doesn't know commit {self.status.commit[:7]} on {self.repo}")
        r.raise_for_status()
        return r.json()

    def check(self) -> Status:
        """Ask GitHub now. Never raises; problems end up in status.state == 'error'."""
        if not self.status.commit or self.status.state == "disabled":
            return self.status
        with self._lock:
            new = Status(commit=self.status.commit, checked_at=time.time())
            try:
                from .api import busy_reasons
                apply_compare(new, self._compare(), busy_reasons())
            except Exception as e:   # network, rate limit, odd JSON: keep it off the pages' critical path
                new.state, new.detail = "error", f"Update check failed: {e}"
                log.warning(new.detail)
            self.status = new
            if new.state == "behind":
                self._notify(new)
            return new

    def _notify(self, st: Status) -> None:
        try:
            if self.notified_file.read_text().strip() == st.latest:
                return
        except OSError:
            pass
        from .pipeline.nextcloud import notify
        lines = "\n".join(f"- {c['short']} {c['title']}" for c in st.commits[:10])
        notify("TTRPG server is out of date",
               f"Running {st.short}; {len(st.commits)} newer change(s) on GitHub {self.branch} haven't been deployed:\n"
               f"{lines}\n\nDeploys normally run once the server is idle. Check the Deploy workflow on GitHub.",
               log.info)
        try:
            self.notified_file.write_text(st.latest)
        except OSError:
            pass


def apply_compare(st: Status, data: dict, busy: list[str] | None = None) -> None:
    """Fill a Status from GitHub's compare response (running commit ... branch).

    `busy` is why the server can't be restarted right now (see api.busy_reasons): while it is busy the
    deploy is expected to wait, so that is "pending", not "behind"."""
    commits = data.get("commits") or []
    st.latest = commits[-1]["sha"] if commits else st.commit
    if data.get("status") in {"identical", "behind"} or not commits:
        st.state = "current"
        return
    files = [f.get("filename", "") for f in data.get("files") or []]
    if not any(f.startswith(IMAGE_PATHS) for f in files) and len(files) < 300:
        # Only docs/tests changed since this build (GitHub lists up to 300 files; beyond that, assume code).
        st.state, st.detail = "current", f"{len(commits)} newer commit(s) on GitHub only change docs or tests."
        return
    for c in reversed(commits):
        msg = (c.get("commit") or {}).get("message", "")
        date = ((c.get("commit") or {}).get("committer") or {}).get("date", "")
        st.commits.append({"sha": c["sha"], "short": c["sha"][:7], "title": msg.splitlines()[0] if msg else "",
                           "date": _ts(date), "url": c.get("html_url", "")})
    dates = [c["date"] for c in st.commits if c["date"]]
    st.waiting_since, st.pushed_at = min(dates, default=0.0), max(dates, default=0.0)
    notes = []
    if data.get("status") == "diverged":
        notes.append("This server runs a commit that isn't on GitHub's branch (a local change?).")
    # Every push starts a new deploy, so the grace runs from the newest commit.
    if busy:
        st.state = "pending"
        notes.append("The deploy waits until the server is idle: " + "; ".join(busy) + ".")
    elif st.pushed_at and time.time() - st.pushed_at < GRACE_SECONDS:
        st.state = "pending"
    else:
        st.state = "behind"
    st.detail = " ".join(notes)


def _ts(iso: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


tracker = Tracker()
