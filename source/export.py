"""Écriture des résultats : TXT, SRT, VTT, JSON, CSV."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from source.config import settings
from source.media import format_timestamp
from source.transcribe import Segment

FORMATS = ("txt", "srt", "vtt", "json", "csv", "rag")

# Le format RAG produit du JSON Lines : une ligne = un passage indexable.
SUFFIXES = {"rag": ".rag.jsonl"}


def write(
    segments: list[Segment],
    destination: Path,
    formats: list[str],
    frames: list | None = None,
) -> list[Path]:
    """Écrit les formats demandés. `frames` ne concerne que `json` et `rag`."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for fmt in formats:
        if fmt not in FORMATS:
            raise ValueError(f"format inconnu : {fmt} (attendus : {', '.join(FORMATS)})")
        # Ajout, pas remplacement : « Node.js tutorial » deviendrait sinon
        # « Node.srt », et « titre.en » (sous-titres d'origine) « titre.srt ».
        path = destination.with_name(destination.name + SUFFIXES.get(fmt, f".{fmt}"))
        writer = globals()[f"_write_{fmt}"]
        if fmt in ("json", "rag"):
            writer(segments, path, frames or [])
        else:
            writer(segments, path)
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


def _write_json(segments: list[Segment], path: Path, frames: list | None = None) -> None:
    payload = {
        "count": len(segments),
        "duration": segments[-1].end if segments else 0.0,
        "segments": [asdict(s) for s in segments],
        # Toutes les captures détectées, y compris celles qu'aucune parole ne
        # recouvre : sans cette liste, une diapositive montrée en silence
        # disparaîtrait du résultat.
        "frames": [
            {"timestamp": round(f.timestamp, 2),
             "image": f"{Path(f.path).parent.name}/{Path(f.path).name}",
             "text": f.text}
            for f in (frames or [])
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(segments: list[Segment], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["start", "end", "text", "ocr"])
        for s in segments:
            writer.writerow([f"{s.start:.3f}", f"{s.end:.3f}", s.text, " | ".join(s.ocr)])


def _write_rag(segments: list[Segment], path: Path, frames: list | None = None) -> None:
    """JSON Lines prêt à indexer : un passage par ligne, horodaté et sourcé.

    Les segments Whisper (une phrase, 5 à 15 s) sont trop courts pour être
    embarqués tels quels : isolés, ils perdent leur contexte et la recherche
    vectorielle devient bruitée. On les regroupe en passages d'environ
    `rag_chunk_chars` caractères, avec un recouvrement de quelques segments
    pour ne pas couper une explication en deux.
    """
    source = path.name.removesuffix(".rag.jsonl")
    overlap = max(settings.rag_overlap_segments, 0)

    passages: list[list[Segment]] = []
    current: list[Segment] = []
    length = 0

    for segment in segments:
        current.append(segment)
        length += len(segment.text) + 1
        if length >= settings.rag_chunk_chars:
            passages.append(current)
            current = current[-overlap:] if overlap else []
            length = sum(len(s.text) + 1 for s in current)
    if current and (not passages or current is not passages[-1]):
        passages.append(current)

    covered: set[str] = set()
    with path.open("w", encoding="utf-8") as handle:
        for index, group in enumerate(passages):
            if not group:
                continue
            ocr = sorted({line for s in group for line in s.ocr})
            # Captures d'écran couvertes par le passage, dans l'ordre du temps.
            shots = list(dict.fromkeys(
                f"{Path(s.frame).parent.name}/{Path(s.frame).name}"
                for s in group if s.frame
            ))
            record = {
                "id": f"{source}#{index:04d}",
                "text": " ".join(s.text for s in group),
                "source": source,
                "chunk": index,
                "start": round(group[0].start, 2),
                "end": round(group[-1].end, 2),
                # Repère lisible, à afficher dans les citations du RAG.
                "timecode": format_timestamp(group[0].start, "."),
                "segments": len(group),
            }
            record["kind"] = "speech"
            if ocr:
                record["screen_text"] = ocr        # texte lu à l'écran (OCR)
            if shots:
                record["frames"] = shots           # images illustrant le passage
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            covered.update(shots)

        # Diapositives qu'aucun passage parlé ne référence : elles deviennent
        # des passages à part entière, indexés sur leur texte à l'écran.
        for frame in frames or []:
            name = f"{Path(frame.path).parent.name}/{Path(frame.path).name}"
            if name in covered or not frame.text:
                continue
            handle.write(json.dumps({
                "id": f"{source}#screen-{name}",
                "text": " ".join(frame.text),
                "source": source,
                "kind": "screen",
                "start": round(frame.timestamp, 2),
                "end": round(frame.timestamp, 2),
                "timecode": format_timestamp(frame.timestamp, "."),
                "screen_text": frame.text,
                "frames": [name],
            }, ensure_ascii=False) + "\n")
