"""ffmpeg helpers: probing, clap sync (for raw uploads), preprocessing, and mixing."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

import numpy as np

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
Log = Callable[[str], None]


def run(cmd: list[str], log: Log) -> None:
    log("$ " + " ".join(cmd))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    if p.stdout.strip():
        for line in p.stdout.strip().splitlines()[-40:]:
            log("  " + line)
    if p.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed with exit code {p.returncode}")


def duration(path: Path) -> float:
    p = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Not a readable audio file: {path.name}: {p.stderr.strip()[-500:]}")
    try:
        return float(json.loads(p.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        raise RuntimeError(f"Could not read the duration of {path.name}")


# ----------------------------------------------------------------- clap sync

def _read_mono(path: Path, seconds: float, start: float, sr: int = 16000) -> np.ndarray:
    cmd = [FFMPEG, "-nostdin", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(path),
           "-ac", "1", "-ar", str(sr), "-af", "highpass=f=80", "-f", "f32le", "pipe:1"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg could not read {path.name}: {p.stderr.decode(errors='replace')[-500:]}")
    return np.frombuffer(p.stdout, dtype=np.float32)


def detect_clap(path: Path, search_start: float, window: float, sr: int = 16000) -> tuple[float, float]:
    """Time (s) and strength of the loudest 20 ms envelope peak in the search window."""
    a = _read_mono(path, window, search_start, sr)
    if a.size == 0:
        raise RuntimeError(f"{path.name}: no audio in the clap search window")
    win = max(1, int(sr * 0.020))
    env = np.convolve(np.abs(a), np.ones(win) / win, mode="same")
    i = int(np.argmax(env))
    return search_start + i / sr, float(env[i])


def find_claps(inputs: dict[str, Path], search_start: float, window: float, log: Log) -> dict:
    """Detected clap per track: {"clap_seconds": {name: s}, "strength": {name: v}, "spread_seconds": s}."""
    peaks, strength = {}, {}
    for name, p in inputs.items():
        t, v = detect_clap(p, search_start, window)
        peaks[name], strength[name] = t, v
        log(f"Clap for {name}: {t:.4f}s (strength {v:.4f})")
    spread = max(peaks.values()) - min(peaks.values())
    return {"clap_seconds": peaks, "strength": strength, "spread_seconds": spread}


def write_synced(inputs: dict[str, Path], out_dir: Path, clap_seconds: dict[str, float] | None, log: Log) -> dict[str, float]:
    """Trim each track so the claps line up (None = keep the tracks as recorded); write 16 kHz mono FLAC.

    Returns the seconds trimmed from the start of each track."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ref = min(clap_seconds.values()) if clap_seconds else 0.0
    trims: dict[str, float] = {}
    for name, p in inputs.items():
        delta = (clap_seconds[name] - ref) if clap_seconds else 0.0
        if clap_seconds and delta >= duration(p):
            raise RuntimeError(f"The clap time for {name} ({clap_seconds[name]:.3f}s) is past the end of its recording")
        dst = out_dir / f"synced_{name}.flac"
        af = []
        if delta >= 0.0005:
            af.append(f"atrim=start={delta:.6f},asetpts=PTS-STARTPTS")
            log(f"{name}: trimming {delta * 1000:.1f} ms")
        run([FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-i", str(p), "-ac", "1", "-ar", "16000",
             *(["-af", ",".join(af)] if af else []), "-c:a", "flac", str(dst)], log)
        trims[name] = round(delta if delta >= 0.0005 else 0.0, 6)
    return trims


# ----------------------------------------------------------------- preprocessing / mix

def preprocess(src: Path, dst: Path, log: Log) -> None:
    run([FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000",
         "-af", "highpass=f=80,loudnorm=I=-18:TP=-1.5:LRA=11,alimiter=limit=0.95",
         "-c:a", "pcm_s16le", str(dst)], log)


def mix(srcs: list[Path], dst: Path, log: Log) -> None:
    args = [FFMPEG, "-nostdin", "-y", "-loglevel", "error"]
    for s in srcs:
        args += ["-i", str(s)]
    pads = "".join(f"[{i}:a]" for i in range(len(srcs)))
    flt = f"{pads}amix=inputs={len(srcs)}:duration=longest:normalize=1,alimiter=limit=0.95[aout]"
    run(args + ["-filter_complex", flt, "-map", "[aout]", "-c:a", "pcm_s16le", str(dst)], log)


def encode_flac(src: Path, dst: Path, log: Log) -> None:
    """Lossless copy of the mix for storage (about half the size of the WAV)."""
    tmp = dst.with_name(dst.name + ".tmp.flac")
    run([FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-i", str(src), "-c:a", "flac",
         "-compression_level", "8", str(tmp)], log)
    tmp.replace(dst)
