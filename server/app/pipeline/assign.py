"""Speaker assignment from per-player lav microphones.

How it works
------------
1. Each lav track is reduced to a loudness timeline: one dB value per 20 ms frame.
2. A "bleed matrix" is estimated automatically: when player S talks, how many dB
   quieter is S's voice on every other player's mic? (Optional enrollment
   windows override the estimate for the players they cover.)
3. For every transcribed word we measure the loudness on each mic over the
   word's exact time span. For each candidate speaker S we predict what the
   other mics *should* read if only S were talking (S level minus bleed). The
   candidate that leaves the least unexplained energy on the other mics wins.
   The true speaker typically leaves ~0 dB unexplained, a wrong candidate
   leaves 15-20 dB, so no hard "8 dB dominance" rule is needed.
4. A Viterbi pass picks the cheapest speaker sequence, with a cost for switching
   speaker mid-sentence, so single noisy words don't flicker between players.
   Whisper segments that really contain more than one speaker are split.

Lookups are O(1) per word (frame index = time / frame length), so an hour of
audio is assigned in about a second.
"""
from __future__ import annotations

import csv
import json
import math
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

FRAME_SEC = 0.02
SAMPLE_RATE = 16000
EPS = 1e-10
MAX_COST = 30.0


@dataclass
class AssignOptions:
    silence_db: float = -60.0          # mic reading below this counts as silent / gated
    overlap_db: float = 10.0           # unexplained energy above this on another live mic => "A+B"
    label_overlaps: bool = True        # emit "A+B" labels for clear cross-talk
    switch_cost_db: float = 12.0       # evidence needed to change speaker mid-segment
    switch_cost_boundary_db: float = 3.0  # ...and between segments / after a pause
    pad_sec: float = 0.04              # widen each word span to absorb alignment jitter
    enrollment: dict[str, tuple[float, float]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loudness timeline
# ---------------------------------------------------------------------------

def frame_energy_db(path: str, ffmpeg: str = "ffmpeg", highpass_hz: int = 80) -> np.ndarray:
    """Decode a file with ffmpeg (streaming) and return per-frame RMS in dBFS."""
    frame_len = int(SAMPLE_RATE * FRAME_SEC)
    cmd = [
        ffmpeg, "-nostdin", "-loglevel", "error", "-i", path,
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-af", f"highpass=f={highpass_hz}",
        "-f", "f32le", "pipe:1",
    ]
    errf = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf)
    out: list[np.ndarray] = []
    leftover = np.zeros(0, dtype=np.float32)
    chunk_bytes = frame_len * 4 * 5000  # ~100 s per read
    assert proc.stdout is not None
    while True:
        buf = proc.stdout.read(chunk_bytes)
        if not buf:
            break
        a = np.frombuffer(buf[: len(buf) - len(buf) % 4], dtype=np.float32)
        if leftover.size:
            a = np.concatenate([leftover, a])
        n = len(a) // frame_len
        if n:
            frames = a[: n * frame_len].reshape(n, frame_len)
            out.append(10.0 * np.log10(np.mean(frames * frames, axis=1) + EPS))
        leftover = a[n * frame_len:].copy()
    proc.wait()
    errf.seek(0)
    err = errf.read()
    errf.close()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed reading {path}: {err.decode(errors='replace')[-2000:]}")
    if leftover.size:
        out.append(np.array([10.0 * math.log10(float(np.mean(leftover * leftover)) + EPS)], dtype=np.float32))
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


def build_energy_matrix(lavs: dict[str, str], ffmpeg: str = "ffmpeg") -> tuple[list[str], np.ndarray]:
    names = list(lavs.keys())
    rows = [frame_energy_db(lavs[n], ffmpeg) for n in names]
    n = max(len(r) for r in rows)
    E = np.full((len(names), n), -100.0, dtype=np.float32)
    for i, r in enumerate(rows):
        E[i, : len(r)] = r
    return names, E


# ---------------------------------------------------------------------------
# Bleed matrix
# ---------------------------------------------------------------------------

def estimate_bleed(names: list[str], E: np.ndarray, opts: AssignOptions) -> np.ndarray:
    """B[s, m] = typical dB drop of speaker s's voice on mic m (B[s, s] = 0)."""
    k = len(names)
    B = np.full((k, k), 12.0, dtype=np.float32)  # sane default for lavs
    np.fill_diagonal(B, 0.0)

    live = E > opts.silence_db
    top = np.argmax(E, axis=0)
    loud_level = np.percentile(np.where(live, E, np.nan), 75, axis=1)  # per-mic "talking" level

    for s in range(k):
        mask = (top == s) & (E[s] >= np.nan_to_num(loud_level[s], nan=-30.0))
        if mask.sum() < 200:  # < 4 s of evidence: keep default
            continue
        for m in range(k):
            if m == s:
                continue
            both = mask & live[m]
            if both.sum() >= 100:
                B[s, m] = float(np.median(E[s, both] - E[m, both]))

    # Enrollment windows (if given) override the automatic estimate.
    for spk, (t0, t1) in opts.enrollment.items():
        if spk not in names:
            continue
        s = names.index(spk)
        f0, f1 = int(t0 / FRAME_SEC), int(t1 / FRAME_SEC)
        seg = E[:, f0:f1]
        if seg.shape[1] < 10:
            continue
        own = seg[s]
        talk = own > np.percentile(own, 50)
        for m in range(k):
            if m != s and talk.any():
                B[s, m] = float(np.median(own[talk] - seg[m, talk]))
    return B


# ---------------------------------------------------------------------------
# Word assignment
# ---------------------------------------------------------------------------

def _span_levels(E: np.ndarray, start: float, end: float) -> np.ndarray:
    n = E.shape[1]
    f0 = min(max(0, int(start / FRAME_SEC)), n - 1)
    f1 = min(max(f0 + 1, int(math.ceil(end / FRAME_SEC))), n)
    p = np.power(10.0, E[:, f0:f1] / 10.0).mean(axis=1)
    return 10.0 * np.log10(p + EPS)


def candidate_costs(levels: np.ndarray, B: np.ndarray, opts: AssignOptions) -> tuple[np.ndarray, np.ndarray]:
    """Unexplained energy (dB) per candidate speaker, and which mic it shows up on.

    If only speaker s were talking, mic m should read levels[s] - B[s, m]. Anything
    louder than that on a live mic is energy s cannot explain. The true speaker
    leaves ~0 dB unexplained; a wrong candidate typically leaves 15-20 dB.
    """
    k = len(levels)
    live = levels > opts.silence_db
    cost = np.full(k, MAX_COST, dtype=np.float32)
    worst = np.full(k, -1)
    if not live.any():
        return np.zeros(k, dtype=np.float32), worst      # no information: all equal
    for s in range(k):
        if not live[s]:
            continue
        if k == 1:
            cost[s] = 0.0
            continue
        excess = np.where(live, levels - (levels[s] - B[s]), -np.inf)
        excess[s] = -np.inf
        m = int(np.argmax(excess))
        cost[s] = min(MAX_COST, max(0.0, float(excess[m])))
        worst[s] = m
    return cost, worst


def _viterbi(costs: np.ndarray, penalties: np.ndarray) -> np.ndarray:
    """Cheapest label path; penalties[i] is the cost of switching speaker before item i."""
    n, k = costs.shape
    back = np.zeros((n, k), dtype=np.int32)
    dp = costs[0].astype(np.float64).copy()
    for i in range(1, n):
        best_prev = int(np.argmin(dp))
        stay = dp
        switch = dp[best_prev] + penalties[i]
        use_switch = switch < stay
        back[i] = np.where(use_switch, best_prev, np.arange(k))
        dp = np.where(use_switch, switch, stay) + costs[i]
    path = np.zeros(n, dtype=np.int32)
    path[-1] = int(np.argmin(dp))
    for i in range(n - 1, 0, -1):
        path[i - 1] = back[i, path[i]]
    return path


def _join_words(words: list[dict]) -> str:
    return " ".join((w.get("word") or "").strip() for w in words if (w.get("word") or "").strip())


def assign_transcript(transcript: dict, names: list[str], E: np.ndarray, B: np.ndarray, opts: AssignOptions) -> list[dict]:
    """Return a list of speaker turns (dicts with start, end, speaker, text, words)."""
    k = len(names)
    items: list[tuple[int, dict]] = []   # (segment index, word)
    segs = transcript.get("segments", [])
    for si, seg in enumerate(segs):
        words = seg.get("words") or []
        if not any("start" in w for w in words):
            # Untimed segment: treat the whole segment as one "word".
            words = [{"word": (seg.get("text") or "").strip(), "start": float(seg.get("start", 0)),
                      "end": float(seg.get("end", 0))}]
        last_end = float(seg.get("start", 0))
        for w in words:
            if "start" not in w or "end" not in w:
                w["start"], w["end"] = last_end, last_end
            last_end = float(w["end"])
            items.append((si, w))
    if not items:
        return []

    costs = np.zeros((len(items), k), dtype=np.float32)
    worst = np.zeros((len(items), k), dtype=np.int32)
    penalties = np.zeros(len(items), dtype=np.float32)
    prev_seg, prev_end = -1, 0.0
    for i, (si, w) in enumerate(items):
        st, en = float(w["start"]) - opts.pad_sec, float(w["end"]) + opts.pad_sec
        lv = _span_levels(E, max(0.0, st), en)
        costs[i], worst[i] = candidate_costs(lv, B, opts)
        w["mic_db"] = {names[m]: round(float(lv[m]), 1) for m in range(k)}
        gap = float(w["start"]) - prev_end
        penalties[i] = opts.switch_cost_db if (si == prev_seg and gap < 1.0) else opts.switch_cost_boundary_db
        prev_seg, prev_end = si, float(w["end"])

    path = _viterbi(costs, penalties)

    for i, (_, w) in enumerate(items):
        srt = np.sort(costs[i])
        w["speaker"] = names[path[i]]
        w["speaker_confidence_db"] = round(float(srt[1] - srt[0]) if k > 1 else 40.0, 1)

    # Group consecutive words of the same speaker within a segment into turns.
    turns: list[dict] = []
    i = 0
    while i < len(items):
        si, _ = items[i]
        j = i
        while j + 1 < len(items) and items[j + 1][0] == si and path[j + 1] == path[i]:
            j += 1
        ws = [items[x][1] for x in range(i, j + 1)]
        spk = int(path[i])
        label = names[spk]
        start, end = float(ws[0]["start"]), float(ws[-1]["end"])
        if opts.label_overlaps and k > 1:
            lv = _span_levels(E, start, end)
            c, wm = candidate_costs(lv, B, opts)
            other = int(wm[spk])
            if c[spk] >= opts.overlap_db and other >= 0 and lv[other] > opts.silence_db:
                label = f"{names[spk]}+{names[other]}"
        confs = [w["speaker_confidence_db"] for w in ws]
        for w in ws:
            w["speaker"] = label
        turns.append({"start": start, "end": end, "speaker": label,
                      "confidence_db": round(float(np.median(confs)), 1),
                      "text": _join_words(ws), "words": ws})
        i = j + 1
    return turns


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h, m, s = int(seconds // 3600), int((seconds % 3600) // 60), seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}" if h else f"{m:02d}:{s:05.2f}"


def write_outputs(turns: list[dict], names: list[str], B: np.ndarray, source: dict,
                  out_json: str, out_csv: str, out_md: str, title: str = "TTRPG Transcript") -> dict:
    bleed = {names[s]: {names[m]: round(float(B[s, m]), 1) for m in range(len(names)) if m != s}
             for s in range(len(names))}
    data = {
        "segments": [
            {"start": t["start"], "end": t["end"], "text": t["text"], "speaker": t["speaker"],
             "lav_speaker": t["speaker"], "speaker_confidence_db": t["confidence_db"], "words": t["words"]}
            for t in turns
        ],
        "language": source.get("language"),
        "lav_assignment_note": "Speaker labels assigned per word from lav mic levels with an automatically estimated bleed matrix; multi-speaker segments are split.",
        "bleed_matrix_db": bleed,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["start", "end", "speaker", "text"])
        w.writeheader()
        for t in turns:
            w.writerow({"start": f"{t['start']:.2f}", "end": f"{t['end']:.2f}", "speaker": t["speaker"], "text": t["text"]})

    with open(out_md, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write("_Speaker labels assigned from lav mic levels (per word, bleed-corrected)._\n\n")
        last = None
        for t in turns:
            if not t["text"]:
                continue
            if t["speaker"] == last:
                f.write(t["text"] + "\n\n")
            else:
                f.write(f"**[{_ts(t['start'])}] {t['speaker']}:** {t['text']}\n\n")
            last = t["speaker"]

    counts = Counter(t["speaker"] for t in turns)
    return {"turns": len(turns), "labels": dict(counts), "bleed_matrix_db": bleed}


def run(transcript_json: str, lavs: dict[str, str], out_json: str, out_csv: str, out_md: str,
        opts: AssignOptions | None = None, title: str = "TTRPG Transcript", ffmpeg: str = "ffmpeg",
        log=print) -> dict:
    opts = opts or AssignOptions()
    with open(transcript_json, encoding="utf-8") as f:
        transcript = json.load(f)
    names, E = build_energy_matrix(lavs, ffmpeg)
    B = estimate_bleed(names, E, opts)
    log("Bleed matrix (dB drop of row speaker on column mic): " + json.dumps(
        {names[s]: [round(float(x), 1) for x in B[s]] for s in range(len(names))}))
    turns = assign_transcript(transcript, names, E, B, opts)
    return write_outputs(turns, names, B, transcript, out_json, out_csv, out_md, title)
