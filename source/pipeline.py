"""Orchestration : enchaîne extraction, transcription, OCR et compression.

Point d'entrée commun à la CLI (`main.py`) et aux workers Celery (`tasks.py`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from source import compress as compress_module
from source import export, media, transcribe
from source.config import settings

log = logging.getLogger(__name__)

ProgressHook = Callable[[str, float], None]


@dataclass(slots=True)
class Result:
    source: Path
    outputs: list[Path] = field(default_factory=list)
    segments: int = 0
    # Tailles en octets : un ratio calculé sur des gigaoctets arrondis serait faux.
    original_bytes: int = 0
    final_bytes: int | None = None

    @property
    def original_gb(self) -> float:
        return self.original_bytes / 1024**3

    @property
    def final_gb(self) -> float | None:
        return None if self.final_bytes is None else self.final_bytes / 1024**3

    @property
    def ratio(self) -> float | None:
        if not self.final_bytes:
            return None
        return self.original_bytes / self.final_bytes


def _noop(stage: str, progress: float) -> None:
    pass


def transcription(
    source: Path,
    output_dir: Path | None = None,
    work_dir: Path | None = None,
    language: str | None = None,
    formats: list[str] | None = None,
    ocr: bool | None = None,
    on_progress: ProgressHook = _noop,
) -> Result:
    """Vidéo/audio → fichiers de sous-titres horodatés."""
    settings.ensure_dirs()
    output_dir = output_dir or settings.output_dir
    work_dir = work_dir or settings.work_dir / source.stem
    formats = formats or ["srt", "json"]

    info = media.probe(source)
    result = Result(source=source, original_bytes=info.size)

    on_progress("extracting", 0.0)
    segments = transcribe.transcribe_media(
        source, work_dir, language=language,
        on_progress=lambda p: on_progress("transcribing", p),
    )

    use_ocr = settings.enable_ocr if ocr is None else ocr
    if use_ocr and info.has_video:
        from source import frames

        on_progress("ocr", 0.0)
        segments = frames.analyze(source, work_dir, segments)

    result.segments = len(segments)
    result.outputs = export.write(segments, output_dir / source.stem, formats)
    on_progress("done", 1.0)

    log.info("%d segments écrits dans %s", len(segments), output_dir)
    return result


def compression(
    source: Path,
    output_dir: Path | None = None,
    target_gb: float | None = None,
    codec: str | None = None,
    on_progress: ProgressHook = _noop,
) -> Result:
    """Ré-encode la vidéo pour tenir sous la taille visée."""
    settings.ensure_dirs()
    output_dir = output_dir or settings.output_dir

    info = media.probe(source)
    if not info.has_video:
        raise ValueError(f"{source} ne contient pas de piste vidéo à compresser")

    destination = output_dir / f"{source.stem}_compressed.mp4"
    compress_module.compress(
        source, destination,
        target_gb=target_gb if target_gb is not None else settings.target_size_gb,
        codec=codec,
        on_progress=lambda p: on_progress("compressing", p),
    )

    on_progress("done", 1.0)
    return Result(
        source=source,
        outputs=[destination],
        original_bytes=info.size,
        final_bytes=media.probe(destination).size,
    )


def process(
    source: Path,
    output_dir: Path | None = None,
    target_gb: float | None = None,
    language: str | None = None,
    formats: list[str] | None = None,
    ocr: bool | None = None,
    on_progress: ProgressHook = _noop,
) -> Result:
    """Transcription puis compression, sur le même fichier source."""
    transcribed = transcription(
        source, output_dir=output_dir, language=language,
        formats=formats, ocr=ocr,
        on_progress=lambda stage, p: on_progress(stage, p * 0.5),
    )
    compressed = compression(
        source, output_dir=output_dir, target_gb=target_gb,
        on_progress=lambda stage, p: on_progress(stage, 0.5 + p * 0.5),
    )
    return Result(
        source=source,
        outputs=[*transcribed.outputs, *compressed.outputs],
        segments=transcribed.segments,
        original_bytes=transcribed.original_bytes,
        final_bytes=compressed.final_bytes,
    )


RUNNERS = {"transcribe": transcription, "compress": compression, "process": process}
