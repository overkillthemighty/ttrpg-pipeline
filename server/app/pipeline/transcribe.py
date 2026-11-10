"""WhisperX transcription (run through its CLI so progress can be streamed into the job)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

PROGRESS_RE = re.compile(r"Progress:\s*([0-9]+(?:\.[0-9]+)?)%")
# tqdm bars (model downloads):  " 68%|######8   | 244M/360M [00:12<00:05, 21.9MB/s]"
TQDM_RE = re.compile(r"^\s*(\d{1,3})%\|.*?\|\s*([0-9.]+\s*[A-Za-z]*)/([0-9.]+\s*[A-Za-z]*)")

# Share of the transcribe stage given to each phase: (load end, transcription end, alignment end).
# The rest (if any) is diarization.
PHASES_PLAIN = (0.05, 0.85, 1.0)
PHASES_DIARIZE = (0.05, 0.65, 0.75)


def _mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 60}m {seconds % 60:02d}s"


class _Tracker:
    """Turns WhisperX output into stage progress with a message, ETA, and an elapsed-time heartbeat.

    Phases: load (model download/load, voice detection; no real progress available)
    -> transcribe ("Progress: x%" lines) -> align (a second "Progress: x%" run)
    -> diarize (optional; no progress available).
    """

    def __init__(self, progress: Callable[[float, str], None], diarize: bool = False):
        self._progress = progress
        self.load_end, self.trans_end, self.align_end = PHASES_DIARIZE if diarize else PHASES_PLAIN
        self._lock = threading.Lock()
        self.phase = "load"
        self.phase_started = time.time()
        self.frac = 0.0
        self.base_msg = "Loading the model"
        self.detail = ""
        self.last_pct = -1.0
        self.first_pct: tuple[float, float] | None = None  # (time, pct) of the first progress line in this phase
        self._dl_bucket: dict[str, int] = {}
        self._last_emit = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._emit()
        self._thread.start()

    # -- output parsing
    def whisper_progress(self, pct: float) -> None:
        with self._lock:
            if self.phase in ("load", "speech"):
                self._enter("transcribe", "Transcribing audio")
            elif self.phase in ("transcribe", "align_load") and (self.phase == "align_load" or pct < self.last_pct - 50 or self.last_pct >= 99.99):
                self._enter("align", "Aligning words")
            self.last_pct = pct
            now = time.time()
            if self.first_pct is None:
                self.first_pct = (now, pct)
            if self.phase == "transcribe":
                self.frac = self.load_end + (self.trans_end - self.load_end) * pct / 100
            else:
                self.frac = self.trans_end + (self.align_end - self.trans_end) * pct / 100
            self.detail = f"{pct:.0f}%"
            t0, p0 = self.first_pct
            if pct - p0 >= 2 and now - t0 >= 20:
                left = (now - t0) * (100 - pct) / (pct - p0)
                self.detail += f", about {_mmss(left)} left"
            self._emit()

    def download(self, pct: int, done: str, total: str) -> str | None:
        """Model download bar: show it in the message; log only every 25%."""
        with self._lock:
            self.detail = f"downloading model files (first run only): {pct}% of {total}"
            self._emit()
            bucket = 100 if pct >= 100 else pct // 25 * 25
            if self._dl_bucket.get(total) == bucket:
                return None
            self._dl_bucket[total] = bucket
            return f"Downloading model files: {pct}% of {total}"

    def hint(self, line: str) -> None:
        """React to WhisperX's own log lines (needs --log-level info)."""
        low = line.lower()
        with self._lock:
            if "performing transcription" in low and self.phase == "load":
                # Voice detection runs first; the "Progress:" lines start after it.
                self._enter("speech", "Detecting speech")
            elif "performing alignment" in low and self.phase != "align":
                self._enter("align", "Aligning words")
            elif "performing diarization" in low and self.phase != "diarize":
                self._enter("diarize", "Identifying speakers")
            elif self.phase == "transcribe" and self.last_pct >= 99.99 and "align" in low:
                self._enter("align_load", "Loading the alignment model")
            else:
                return
            self._emit()

    # -- internals (call with the lock held)
    def _enter(self, phase: str, msg: str) -> None:
        self.phase, self.base_msg, self.detail = phase, msg, ""
        self.phase_started = time.time()
        self.first_pct = None
        self.last_pct = -1.0
        self.frac = {"speech": self.load_end, "transcribe": self.load_end, "align_load": self.trans_end,
                     "align": self.trans_end, "diarize": self.align_end}.get(phase, self.frac)

    def _emit(self) -> None:
        msg = self.base_msg + (f": {self.detail}" if self.detail else "")
        msg += f" ({_mmss(time.time() - self.phase_started)} so far)" if "left" not in msg else ""
        self._last_emit = time.time()
        self._progress(self.frac, msg)

    def _heartbeat(self) -> None:
        while not self._stop.wait(5):
            with self._lock:
                if time.time() - self._last_emit >= 5:
                    self._emit()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)



def cuda_available() -> bool:
    try:
        import torch  # noqa: WPS433 (heavy import only when needed)
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def pick_device(device: str, compute_type: str) -> tuple[str, str]:
    if device == "auto":
        device = "cuda" if cuda_available() else "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


def whisperx_cmd() -> list[str]:
    exe = shutil.which("whisperx")
    return [exe] if exe else [sys.executable, "-m", "whisperx"]


def transcribe(audio: Path, out_dir: Path, *, model: str, language: str, device: str, compute_type: str,
               batch_size: int, threads: int, log: Callable[[str], None],
               progress: Callable[[float, str], None], diarize: bool = False,
               min_speakers: int | None = None, max_speakers: int | None = None,
               diarize_model: str = "", hf_token: str = "") -> Path:
    """Run WhisperX; progress(fraction 0..1, message). Returns the JSON transcript path.

    With diarize=True each segment/word gets a "speaker" (SPEAKER_00, ...). The Hugging Face
    token is passed in the environment, never on the command line (which is logged).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = whisperx_cmd() + [
        str(audio), "--model", model, "--language", language, "--device", device,
        "--compute_type", compute_type, "--batch_size", str(batch_size),
        "--output_format", "json", "--output_dir", str(out_dir),
        "--print_progress", "True", "--verbose", "False", "--log-level", "info",
    ]
    if threads > 0:
        cmd += ["--threads", str(threads)]
    if diarize:
        cmd += ["--diarize"]
        if min_speakers:
            cmd += ["--min_speakers", str(min_speakers)]
        if max_speakers:
            cmd += ["--max_speakers", str(max_speakers)]
        if diarize_model:
            cmd += ["--diarize_model", diarize_model]
    log("$ " + " ".join(cmd))
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    if hf_token:
        env["HF_TOKEN"] = hf_token
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                            bufsize=1, env=env)
    tracker = _Tracker(progress, diarize=diarize)
    recent: list[str] = []
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line.strip():
                continue
            m = PROGRESS_RE.search(line)
            if m:
                tracker.whisper_progress(float(m.group(1)))
                continue
            d = TQDM_RE.match(line)
            if d:
                msg = tracker.download(int(d.group(1)), d.group(2), d.group(3))
                if msg:
                    log(msg)
                continue
            tracker.hint(line)
            log(line)
            recent = (recent + [line])[-3:]
    finally:
        tracker.stop()
    rc = proc.wait()
    if rc != 0:
        hint = f": {recent[-1][:300]}" if recent else ""
        raise RuntimeError(f"WhisperX failed with exit code {rc}{hint}")
    out = out_dir / (audio.stem + ".json")
    if not out.exists():
        found = sorted(out_dir.glob("*.json"))
        if not found:
            raise RuntimeError("WhisperX finished but wrote no JSON transcript")
        out = found[0]
    return out
