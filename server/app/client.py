"""Laptop scripts served from the dashboard (Laptop scripts page)."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from functools import lru_cache
from pathlib import Path

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"
SCRIPTS = ["record.ps1", "Sync-TTRPGLavs.ps1", "Submit-Session.ps1", "TTRPG.Common.ps1"]

README = """TTRPG recorder scripts (version {version})
Downloaded from {url}

1. Before extracting: right-click the zip > Properties > tick "Unblock" > OK.
   (Or after extracting, run in PowerShell:  Get-ChildItem *.ps1 | Unblock-File)
2. Extract straight into your recorder folder (no subfolder), next to ffmpeg.exe and ffprobe.exe.
   The scripts also find ffmpeg one folder up, or on PATH.
3. {config_step}
4. Run .\\record.ps1

Updating later: download the scripts again WITHOUT a key and extract over the old ones.
That zip has no config.json, so your settings are kept.
"""


def available() -> bool:
    return all((CLIENT_DIR / n).is_file() for n in SCRIPTS + ["config.example.json"])


@lru_cache(maxsize=1)
def version() -> str:
    """Short hash of the scripts; Submit-Session.ps1 compares it to warn about outdated copies."""
    h = hashlib.sha256()
    for name in SCRIPTS:
        p = CLIENT_DIR / name
        if p.is_file():
            h.update(name.encode())
            h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def _crlf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _config(base_url: str, api_key: str | None) -> bytes:
    cfg = json.loads((CLIENT_DIR / "config.example.json").read_text(encoding="utf-8"))
    cfg.setdefault("server", {})
    cfg["server"]["url"] = base_url
    cfg["server"]["apiKey"] = api_key or "ttrpg_paste-the-key-from-the-dashboard"
    return json.dumps(cfg, indent=2).encode("utf-8")


def build_zip(base_url: str, api_key: str | None = None) -> bytes:
    """Zip of the scripts. With api_key it also contains a ready-to-use config.json."""
    if not available():
        raise FileNotFoundError("The client scripts are missing from this server image")
    buf = io.BytesIO()
    ver = version()
    if api_key:
        step = "config.json is already set up with this server's address and a new API key. Check playerCount and roomMic in it."
    else:
        step = "If you don't have a config.json yet: copy config.example.json to config.json and put your API key in server.apiKey."
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in SCRIPTS:
            z.writestr(name, _crlf((CLIENT_DIR / name).read_bytes()))
        z.writestr("config.example.json", _crlf(_config(base_url, None)))
        if api_key:
            z.writestr("config.json", _crlf(_config(base_url, api_key)))
        z.writestr("client_version.txt", ver + "\r\n")
        z.writestr("README.txt", _crlf(README.format(version=ver, url=base_url, config_step=step).encode()))
    return buf.getvalue()
