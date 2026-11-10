"""Outputs for single-mic jobs: no lav levels, so speakers come from diarization or not at all.

Writes the same three files as the lav assignment (json/csv/md), so everything downstream
(Nextcloud folders, Session Logs) works unchanged.
"""
from __future__ import annotations

import csv
import json
from collections import Counter

from .assign import _ts

NO_SPEAKER = "Speaker"


def _speaker_names(transcript: dict) -> dict[str, str]:
    """SPEAKER_00, SPEAKER_03, ... -> "Speaker 1", "Speaker 2", ... in order of first appearance."""
    names: dict[str, str] = {}
    for seg in transcript.get("segments", []):
        for item in [seg] + list(seg.get("words") or []):
            raw = item.get("speaker")
            if raw and raw not in names:
                names[raw] = f"Speaker {len(names) + 1}"
    return names


def _turns(transcript: dict, diarized: bool) -> list[dict]:
    names = _speaker_names(transcript) if diarized else {}
    turns: list[dict] = []
    for seg in transcript.get("segments", []):
        seg_spk = names.get(seg.get("speaker"), "Unknown") if diarized else NO_SPEAKER
        words = [w for w in (seg.get("words") or []) if str(w.get("word", "")).strip()]
        if not diarized or not words:
            turns.append({"start": float(seg.get("start", 0.0)), "end": float(seg.get("end", 0.0)),
                          "speaker": seg_spk, "text": str(seg.get("text", "")).strip(), "words": words})
            continue
        # Split the segment wherever the word-level speaker changes.
        cur = None
        for w in words:
            spk = names.get(w.get("speaker"), cur["speaker"] if cur else seg_spk)
            start = float(w.get("start", cur["end"] if cur else seg.get("start", 0.0)))
            end = float(w.get("end", start))
            if cur is None or spk != cur["speaker"]:
                cur = {"start": start, "end": end, "speaker": spk, "words": []}
                turns.append(cur)
            cur["words"].append(w)
            cur["end"] = max(cur["end"], end)
        for t in turns:
            if "text" not in t:
                t["text"] = " ".join(str(w["word"]).strip() for w in t["words"]).strip()
    return [t for t in turns if t["text"]]


def run(transcript_json: str, out_json: str, out_csv: str, out_md: str, *, diarized: bool,
        title: str = "TTRPG Transcript", log=print) -> dict:
    with open(transcript_json, encoding="utf-8") as f:
        transcript = json.load(f)
    turns = _turns(transcript, diarized)
    note = ("Single-mic recording. Speakers were guessed by diarization; 'Speaker N' labels are not player names "
            "and may split or merge people." if diarized else "Single-mic recording without speaker labels.")

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"mode": "single", "diarized": diarized, "note": note, "language": transcript.get("language"),
                   "segments": [{"start": t["start"], "end": t["end"], "text": t["text"], "speaker": t["speaker"],
                                 "words": t["words"]} for t in turns]}, f, indent=2, ensure_ascii=False)

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["start", "end", "speaker", "text"])
        w.writeheader()
        for t in turns:
            w.writerow({"start": f"{t['start']:.2f}", "end": f"{t['end']:.2f}", "speaker": t["speaker"], "text": t["text"]})

    with open(out_md, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n_{note}_\n\n")
        last = None
        for t in turns:
            if not diarized:
                f.write(f"**[{_ts(t['start'])}]** {t['text']}\n\n")
            elif t["speaker"] == last:
                f.write(t["text"] + "\n\n")
            else:
                f.write(f"**[{_ts(t['start'])}] {t['speaker']}:** {t['text']}\n\n")
            last = t["speaker"]

    counts = Counter(t["speaker"] for t in turns)
    log(f"Single-mic transcript: {len(turns)} lines" + (f", speakers {dict(counts)}" if diarized else ""))
    return {"turns": len(turns), "labels": dict(counts)}
