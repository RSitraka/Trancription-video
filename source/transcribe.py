"""Transcription faster-whisper : morceau par morceau, puis fusion."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from source import chunker, media
from source.config import settings

log = logging.getLogger(__name__)

_MODEL = None  # chargé une seule fois par processus (~5 Go de VRAM en float16)
_BATCHED = None


@dataclass(slots=True)
class Segment:
    start: float
    end: float
    text: str
    confidence: float | None = None
    ocr: list[str] = field(default_factory=list)
    frame: str | None = None
    # Langue détectée par Whisper : sert à traduire les sous-titres.
    lang: str | None = None

    def shifted(self, offset: float) -> "Segment":
        return Segment(
            start=self.start + offset,
            end=self.end + offset,
            text=self.text,
            confidence=self.confidence,
            ocr=list(self.ocr),
            frame=self.frame,
            lang=self.lang,
        )


def get_model():
    """Charge le modèle à la demande : l'API n'a pas à payer ce coût."""
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel

        log.info(
            "Chargement de %s sur %s (%s)",
            settings.whisper_model, settings.whisper_device, settings.whisper_compute_type,
        )
        _MODEL = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )
    return _MODEL


def get_pipeline():
    """Transcription par lots : les passages découpés par le VAD sont décodés
    ensemble au lieu d'un par un, ce qui occupe enfin toute la carte."""
    global _BATCHED
    if _BATCHED is None:
        from faster_whisper import BatchedInferencePipeline

        _BATCHED = BatchedInferencePipeline(model=get_model())
    return _BATCHED


def transcribe_file(path: Path, language: str | None = None) -> list[Segment]:
    """Transcrit un fichier audio court (un morceau) en segments horodatés.

    `language="auto"` laisse Whisper détecter la langue : indispensable quand la
    piste audio n'est pas celle qu'annonce le titre du fichier (doublage
    automatique, par exemple).
    """
    requested = language or settings.language
    if requested in ("auto", ""):
        requested = None

    if settings.whisper_batch_size > 0:
        # Le mode par lots s'appuie toujours sur le VAD pour découper l'audio.
        segments, info = get_pipeline().transcribe(
            str(path),
            language=requested,
            beam_size=settings.beam_size,
            batch_size=settings.whisper_batch_size,
        )
    else:
        segments, info = get_model().transcribe(
            str(path),
            language=requested,
            vad_filter=settings.vad_filter,   # saute les silences : gain de temps net
            beam_size=settings.beam_size,
        )
    if requested is None:
        log.info("Langue détectée : %s (%.1f %%)",
                 info.language, info.language_probability * 100)
    return [
        Segment(
            start=s.start,
            end=s.end,
            text=s.text.strip(),
            confidence=getattr(s, "avg_logprob", None),
            lang=requested or getattr(info, "language", None),
        )
        for s in segments
        if s.text.strip()
    ]


def merge(groups: list[list[Segment]], overlap: float) -> list[Segment]:
    """Recolle les morceaux en supprimant les doublons nés du recouvrement.

    Un segment répété se reconnaît à deux signes conjoints : un texte identique
    au précédent et un démarrage situé dans la zone de recouvrement.
    """
    merged: list[Segment] = []
    for group in groups:
        for segment in group:
            if merged:
                previous = merged[-1]
                duplicate = (
                    segment.text.lower() == previous.text.lower()
                    and segment.start < previous.end + overlap
                )
                if duplicate:
                    continue
            merged.append(segment)
    return merged


def transcribe_media(
    source: Path,
    work_dir: Path,
    language: str | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> list[Segment]:
    """Chaîne complète : extraction audio → découpage → transcription → fusion.

    Le fichier source n'est jamais chargé en mémoire, quelle que soit sa taille.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    info = media.probe(source)

    audio = work_dir / "audio.wav"
    if not audio.exists():
        # Piste audio complète, plus un morceau extrait à la fois.
        media.require_space(
            work_dir,
            int((info.duration + settings.chunk_duration) * media.WAV_BYTES_PER_SECOND),
            "la piste audio",
        )
        log.info("Extraction audio de %s (%.1f Go)", source.name, info.size_gb)
        media.extract_audio(
            source, audio,
            on_progress=lambda p: on_progress(p * 0.1) if on_progress else None,
        )

    plan = chunker.plan(audio, work_dir, duration=info.duration)
    groups: list[list[Segment]] = []

    # Sur une reprise, annoncer d'emblée ce qui est déjà fait : sinon la barre
    # affiche 0 % pendant tout le premier morceau et paraît bloquée.
    if on_progress and plan.progress:
        on_progress(0.1 + 0.9 * plan.progress)

    for chunk in plan.chunks:
        cache = work_dir / f"chunk_{chunk.index:04d}.json"

        if chunk.status == "done" and cache.exists():
            groups.append(_load_cache(cache))
            continue

        chunker.materialize(audio, chunk)
        log.info("Transcription du morceau %d/%d", chunk.index + 1, len(plan.chunks))
        try:
            local = transcribe_file(chunk.file, language)
        except Exception:
            plan.mark(chunk, "failed")
            raise

        shifted = [s.shifted(chunk.start) for s in local]
        _save_cache(cache, shifted)
        groups.append(shifted)

        plan.mark(chunk, "done")
        plan.cleanup(chunk)

        if on_progress:
            on_progress(0.1 + 0.9 * plan.progress)

    if settings.delete_chunks_after and audio.exists():
        audio.unlink()

    return merge(groups, overlap=settings.chunk_overlap)


def _save_cache(path: Path, segments: list[Segment]) -> None:
    """Cache par morceau : une reprise ne retranscrit pas ce qui est déjà fait."""
    path.write_text(
        json.dumps([asdict(s) for s in segments], ensure_ascii=False),
        encoding="utf-8",
    )


def _load_cache(path: Path) -> list[Segment]:
    return [Segment(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
