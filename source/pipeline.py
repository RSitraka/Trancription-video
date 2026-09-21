"""Orchestration : enchaîne extraction, transcription, OCR et compression.

Point d'entrée commun à la CLI (`main.py`) et aux workers Celery (`tasks.py`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from source import compress as compress_module
from source import export, media, titling, transcribe
from source.config import settings

log = logging.getLogger(__name__)

ProgressHook = Callable[[str, float], None]


@dataclass(slots=True)
class Result:
    source: Path
    outputs: list[Path] = field(default_factory=list)
    # Nom donné aux fichiers produits : celui du fichier s'il parle, sinon un
    # titre tiré de la transcription (source/titling.py).
    title: str = ""
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


def title_dir(output_dir: Path, source: Path, subdir: str | None = None,
              title: str | None = None) -> Path:
    """Dossier des fichiers produits pour une vidéo : `out/[sous-dossier/]<titre>/`.

    Parties compressées et sous-titres d'une même vidéo restent ensemble au
    lieu de se mélanger à ceux des autres vidéos. `title` remplace le nom du
    fichier quand celui-ci ne dit rien du contenu."""
    return _inside(output_dir, str(Path(subdir or "") / (title or source.stem)))


def _inside(output_dir: Path, subdir: str) -> Path:
    target = (output_dir / subdir).resolve()
    if not target.is_relative_to(output_dir.resolve()):
        raise ValueError(f"sous-dossier de sortie invalide : {subdir}")
    target.mkdir(parents=True, exist_ok=True)
    return target


def transcription(
    source: Path,
    output_dir: Path | None = None,
    work_dir: Path | None = None,
    language: str | None = None,
    formats: list[str] | None = None,
    ocr: bool | None = None,
    subdir: str | None = None,
    on_progress: ProgressHook = _noop,
) -> Result:
    """Vidéo/audio → fichiers de sous-titres horodatés.

    Les sous-titres vont dans le dossier au nom de la vidéo, à côté des parties
    compressées ; `subdir` évite que deux « intro.mp4 » de dossiers différents
    s'écrasent.
    """
    settings.ensure_dirs()
    base_output = output_dir or settings.output_dir
    # Le dossier de travail garde le nom du fichier : il sert à la reprise, et
    # le titre n'est connu qu'après la transcription.
    work_dir = work_dir or settings.work_dir / (subdir or "") / source.stem
    formats = formats or ["srt", "json"]

    info = media.probe(source)
    result = Result(source=source, original_bytes=info.size)

    on_progress("extracting", 0.0)
    segments = transcribe.transcribe_media(
        source, work_dir, language=language,
        on_progress=lambda p: on_progress("transcribing", p),
    )

    # « VID_20260918.mp4 » ne dit rien : le contenu donne alors le nom.
    result.title = titling.choose(source, segments)
    output_dir = title_dir(base_output, source, subdir, result.title)

    captures: list = []
    use_ocr = settings.enable_ocr if ocr is None else ocr
    if use_ocr and info.has_video:
        from source import frames

        on_progress("ocr", 0.0)
        segments, captures = frames.analyze(source, output_dir, segments)

    result.segments = len(segments)
    result.outputs = export.write(
        segments, output_dir / result.title, formats, frames=captures
    )
    on_progress("done", 1.0)

    log.info("%d segments écrits dans %s", len(segments), output_dir)
    return result


def compression(
    source: Path,
    output_dir: Path | None = None,
    target_gb: float | None = None,
    codec: str | None = None,
    max_mb: float | None = None,
    split: bool = True,
    subdir: str | None = None,
    title: str | None = None,
    on_progress: ProgressHook = _noop,
) -> Result:
    """Ré-encode la vidéo (ou l'audio seul) pour tenir sous la taille visée.

    `subdir` reproduit l'arborescence d'un dossier source dans les sorties ;
    `title` vient de la transcription quand le nom du fichier ne dit rien.
    """
    settings.ensure_dirs()
    title = title or titling.choose(source)
    output_dir = title_dir(output_dir or settings.output_dir, source, subdir, title)

    info = media.probe(source)
    if not info.has_video and max_mb is None:
        raise ValueError(f"{source} ne contient pas de piste vidéo à compresser")

    extension = "mp4" if info.has_video else "m4a"
    destination = output_dir / f"{title}_compressed.{extension}"
    outputs = compress_module.compress(
        source, destination,
        target_gb=target_gb if target_gb is not None else settings.target_size_gb,
        codec=codec,
        max_mb=max_mb,
        split=split,
        on_progress=lambda p: on_progress("compressing", p),
    )

    on_progress("done", 1.0)
    return Result(
        source=source,
        outputs=outputs,
        title=title,
        original_bytes=info.size,
        final_bytes=sum(p.stat().st_size for p in outputs),
    )


def process(
    source: Path,
    output_dir: Path | None = None,
    target_gb: float | None = None,
    language: str | None = None,
    formats: list[str] | None = None,
    ocr: bool | None = None,
    max_mb: float | None = None,
    split: bool = True,
    subdir: str | None = None,
    on_progress: ProgressHook = _noop,
) -> Result:
    """Transcription puis compression, sur le même fichier source."""
    # La fin de la transcription n'est pas la fin du job : pas de « done » à mi-course.
    transcribed = transcription(
        source, output_dir=output_dir, language=language,
        formats=formats, ocr=ocr, subdir=subdir,
        on_progress=lambda stage, p: on_progress(
            "compressing" if stage == "done" else stage, p * 0.5),
    )
    # Le même titre pour les sous-titres et les parties compressées.
    compressed = compression(
        source, output_dir=output_dir, target_gb=target_gb, max_mb=max_mb,
        split=split, subdir=subdir, title=transcribed.title,
        on_progress=lambda stage, p: on_progress(stage, 0.5 + p * 0.5),
    )
    return Result(
        source=source,
        outputs=[*transcribed.outputs, *compressed.outputs],
        title=transcribed.title,
        segments=transcribed.segments,
        original_bytes=transcribed.original_bytes,
        final_bytes=compressed.final_bytes,
    )


RUNNERS = {"transcribe": transcription, "compress": compression, "process": process}
