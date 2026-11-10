"""Publish results to Nextcloud over WebDAV (chunked upload for big files) and send notifications."""
from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

from ..config import settings

CHUNK = 10 * 1024 * 1024          # Nextcloud chunking v2 requires 5 MB..5 GB per chunk
CHUNK_THRESHOLD = 50 * 1024 * 1024


def _read_chunks(path: Path, size: int = 1024 * 1024):
    with open(path, "rb") as fh:
        while True:
            b = fh.read(size)
            if not b:
                return
            yield b


def _enc(path: str) -> str:
    return "/".join(quote(p) for p in path.strip("/").split("/") if p)


class Nextcloud:
    def __init__(self, log: Callable[[str], None]):
        self.log = log
        self.base = settings.nc_url
        self.user = settings.nc_user
        self.files_root = f"{self.base}/remote.php/dav/files/{quote(self.user)}"
        self.client = httpx.Client(auth=(self.user, settings.nc_password), timeout=httpx.Timeout(60, read=300),
                                   verify=settings.nc_verify_tls, headers={"OCS-APIRequest": "true"})

    def close(self) -> None:
        self.client.close()

    def _req(self, method: str, url: str, ok: set[int], file: Path | None = None, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(4):
            try:
                if file is not None:  # reopen on every attempt so retries resend the whole body
                    headers = {**kw.get("headers", {}), "Content-Length": str(file.stat().st_size)}
                    rest = {k: v for k, v in kw.items() if k != "headers"}
                    r = self.client.request(method, url, content=_read_chunks(file), headers=headers, **rest)
                else:
                    r = self.client.request(method, url, **kw)
                if r.status_code in ok:
                    return r
                if r.status_code in (401, 403):
                    raise RuntimeError(f"Nextcloud refused {method} ({r.status_code}). Check NEXTCLOUD_USER / NEXTCLOUD_APP_PASSWORD.")
                last = RuntimeError(f"Nextcloud {method} {url} -> HTTP {r.status_code}: {r.text[:300]}")
                if r.status_code < 500 and r.status_code != 429:
                    break
            except httpx.HTTPError as e:
                last = e
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(str(last))

    def ensure_dir(self, path: str) -> None:
        cur = ""
        for part in [p for p in path.strip("/").split("/") if p]:
            cur = f"{cur}/{part}" if cur else part
            # 201 created, 405 already exists
            self._req("MKCOL", f"{self.files_root}/{_enc(cur)}", {201, 405})

    def upload(self, local: Path, remote_path: str) -> None:
        dest = f"{self.files_root}/{_enc(remote_path)}"
        size = local.stat().st_size
        if size < CHUNK_THRESHOLD or not self._chunked(local, dest, size):
            self._req("PUT", dest, {200, 201, 204}, file=local)
        self.log(f"Uploaded {local.name} -> {remote_path} ({size / 1048576:.1f} MB)")

    def _chunked(self, local: Path, dest: str, size: int) -> bool:
        """Nextcloud chunked upload v2. Returns False if the server doesn't support it."""
        upload_dir = f"{self.base}/remote.php/dav/uploads/{quote(self.user)}/ttrpg-{secrets.token_hex(8)}"
        hdr = {"Destination": dest}
        try:
            self._req("MKCOL", upload_dir, {201}, headers=hdr)
        except RuntimeError as e:
            self.log(f"Chunked upload unavailable ({e}); falling back to a single PUT")
            return False
        try:
            with open(local, "rb") as fh:
                n = 0
                while True:
                    data = fh.read(CHUNK)
                    if not data:
                        break
                    n += 1
                    self._req("PUT", f"{upload_dir}/{n:05d}", {201, 204}, content=data,
                              headers={**hdr, "OC-Total-Length": str(size)})
            self._req("MOVE", f"{upload_dir}/.file", {201, 204},
                      headers={**hdr, "OC-Total-Length": str(size), "Overwrite": "T"})
            return True
        except Exception:
            try:
                self.client.request("DELETE", upload_dir)
            except Exception:
                pass
            raise


def publish(final_dir: Path, files: list[str], session_date: str, log: Callable[[str], None]) -> dict:
    nc = Nextcloud(log)
    try:
        folder = f"{settings.nc_results_dir}/{session_date}"
        nc.ensure_dir(folder)
        for name in files:
            nc.upload(final_dir / name, f"{folder}/{name}")
        logs_path = None
        md = final_dir / "transcript_lav_assigned.md"
        if settings.nc_session_logs_dir and md.exists():
            nc.ensure_dir(settings.nc_session_logs_dir)
            logs_path = f"{settings.nc_session_logs_dir}/{session_date}_transcript_lav_assigned.md"
            nc.upload(md, logs_path)
        return {"folder": folder, "session_log": logs_path, "published_at": time.time()}
    finally:
        nc.close()


def notify(subject: str, message: str, log: Callable[[str], None]) -> None:
    """Send a Nextcloud notification (needs an admin account; failures are logged, never fatal)."""
    if not settings.nc_notify_enabled:
        return
    admin = settings.nc_notify_admin_user or settings.nc_user
    password = settings.nc_notify_admin_password or settings.nc_password
    target = settings.nc_notify_target_user or settings.nc_user
    url = f"{settings.nc_url}/ocs/v2.php/apps/notifications/api/v2/admin_notifications/{quote(target)}"
    try:
        r = httpx.post(url, auth=(admin, password), headers={"OCS-APIRequest": "true", "Accept": "application/json"},
                       data={"shortMessage": subject[:255], "longMessage": message[:4000]},
                       timeout=30, verify=settings.nc_verify_tls)
        if r.status_code >= 300:
            log(f"Nextcloud notification failed: HTTP {r.status_code} {r.text[:200]}")
        else:
            log(f"Sent Nextcloud notification to {target}")
    except httpx.HTTPError as e:
        log(f"Nextcloud notification failed: {e}")
