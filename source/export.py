"""Écriture des résultats : TXT, SRT, VTT, JSON, CSV."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from source.media import format_timestamp
from source.transcribe import Segment

FORMATS = ("txt", "srt", "vtt", "json", "csv")


def write(segments: list[Segment], destination: Path, formats: list[str]) -> list[Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for fmt in formats:
        if fmt not in FORMATS:
            raise ValueError(f"format inconnu : {fmt} (attendus : {', '.join(FORMATS)})")
        path = destination.with_suffix(f".{fmt}")
        globals()[f"_write_{fmt}"](segments, path)
        written.append(path)
    return written


def _write_txt(segments: list[Segment], path: Path) -> None:
    path.write_text("\n".join(s.text for s in segments), encoding="utf-8")


def _write_srt(segments: list[Segment], path: Path) -> None:
    blocks = []
    for i, s in enumerate(segments, start=1):
        start = format_timestamp(s.start, ",")
        end = format_timestamp(s.end, ",")
        blocks.append(f"{i}\n{start} --> {end}\n{s.text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def _write_vtt(segments: list[Segment], path: Path) -> None:
    lines = ["WEBVTT", ""]
    for s in segments:
        lines.append(f"{format_timestamp(s.start, '.')} --> {format_timestamp(s.end, '.')}")
        lines.append(s.text)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_json(segments: list[Segment], path: Path) -> None:
    payload = {
        "count": len(segments),
        "duration": segments[-1].end if segments else 0.0,
        "segments": [asdict(s) for s in segments],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(segments: list[Segment], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["start", "end", "text", "ocr"])
        for s in segments:
            writer.writerow([f"{s.start:.3f}", f"{s.end:.3f}", s.text, " | ".join(s.ocr)])
