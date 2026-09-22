"""Orchestration : enchaîne extraction, transcription, OCR et compression.

Point d'entrée commun à la CLI (`main.py`) et aux workers Celery (`tasks.py`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from source import compress as compress_module
from source import export, media, titling, transcribe, translate
from source.config import settings

log = logging.getLogger(__name__)

ProgressHook = Callable[[str, float], None]


@dataclass(slots=True)
class Result:
    source: Path
    outputs: list[Path] = field(default_factory=list)
    # Ce qu'il faut signaler sans que ce soit un échec (vidéo muette…).
    note: str = ""
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
    subtitle_language: str | None = None,
    on_progress: ProgressHook = _noop,
) -> Result:
    """Vidéo/audio → fichiers de sous-titres horodatés.

    `language` est la langue **parlée** (« auto » : détectée). `subtitle_language`
    est celle des sous-titres voulus : si elle diffère, le texte est traduit
    (horodatages inchangés) et les sous-titres d'origine sont gardés à côté,
    sous le nom `<titre>.<langue>.srt`.

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
    if not info.has_audio:
        raise ValueError(
            f"« {source.name} » n'a aucune piste audio : rien à transcrire. "
            "C'est le cas des vidéos téléchargées en flux séparés (YouTube « videoplayback ») "
            "et des vidéos muettes ; la compression, elle, reste possible."
        )
    result = Result(source=source, original_bytes=info.size)

    on_progress("extracting", 0.0)
    segments = transcribe.transcribe_media(
        source, work_dir, language=language,
        on_progress=lambda p: on_progress("transcribing", p),
    )

    spoken = _spoken_language(language, segments)
    target = subtitle_language if subtitle_language not in (None, "", "same", "auto") else None
    original: list = []
    if target and spoken and target != spoken and segments:
        log.info("Traduction des sous-titres : %s → %s", spoken, target)
        on_progress("translating", 0.0)
        original = segments
        segments = translate.translate_segments(
            segments, spoken, target, on_progress=lambda p: on_progress("translating", p))

    # « VID_20260918.mp4 » ne dit rien : le contenu donne alors le nom. Sans
    # parole exploitable, le texte affiché à l'écran est lu (OCR).
    result.title = titling.choose(source, segments,
                                  video=source if info.has_video else None,
                                  duration=info.duration)
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
    if original:
        # Sous-titres dans la langue parlée, à côté de la traduction.
        result.outputs += export.write(original, output_dir / f"{result.title}.{spoken}", ["srt"])
        result.note = f"sous-titres traduits : {spoken} → {target}"
    on_progress("done", 1.0)

    log.info("%d segments écrits dans %s", len(segments), output_dir)
    return result


# Mots-outils les plus courants : de quoi reconnaître la langue d'un texte
# quand Whisper ne l'a pas notée (transcription reprise d'un ancien cache).
FUNCTION_WORDS = {
    "fr": {"le", "la", "les", "de", "des", "et", "est", "un", "une", "que", "qui", "pour",
           "pas", "dans", "ce", "vous", "nous", "on", "avec", "sur", "je", "c'est"},
    "en": {"the", "and", "is", "of", "to", "in", "that", "it", "you", "for", "this",
           "with", "are", "be", "on", "not", "have", "we", "can", "your"},
    "es": {"el", "los", "las", "y", "en", "una", "es", "por", "con", "para", "no",
           "se", "lo", "del", "como", "pero"},
    "de": {"der", "die", "das", "und", "ist", "ich", "nicht", "mit", "sie", "es", "ein",
           "eine", "zu", "den", "auf", "wir"},
    "it": {"il", "di", "che", "non", "per", "sono", "gli", "della", "questo", "anche",
           "ma", "come", "nel", "alla"},
    "pt": {"o", "os", "não", "um", "uma", "com", "é", "do", "da", "em", "para", "você",
           "mas", "isso"},
}


def _guess_language(segments: list) -> str | None:
    """Langue d'un texte d'après ses mots-outils ; None si rien de net."""
    words = re.findall(r"[\w']+", " ".join(s.text for s in segments[:200]).lower())
    scores = {lang: sum(w in vocab for w in words) for lang, vocab in FUNCTION_WORDS.items()}
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    return best if top >= 3 and top >= 1.5 * second else None


def _spoken_language(requested: str | None, segments: list) -> str | None:
    """Langue parlée : celle demandée, sinon celle détectée par Whisper, sinon
    celle reconnue dans le texte."""
    if requested and requested not in ("auto", ""):
        return requested
    detected = [s.lang for s in segments if getattr(s, "lang", None)]
    if detected:
        return max(set(detected), key=detected.count)
    return _guess_language(segments)


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
    info = media.probe(source)
    title = title or titling.choose(source, video=source if info.has_video else None,
                                    duration=info.duration)
    output_dir = title_dir(output_dir or settings.output_dir, source, subdir, title)

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
    subtitle_language: str | None = None,
    on_progress: ProgressHook = _noop,
) -> Result:
    """Transcription puis compression, sur le même fichier source.

    Une vidéo sans piste audio (flux vidéo seul, vidéo muette) est simplement
    compressée : mieux qu'un échec, puisque c'est la moitié utile du travail.
    """
    if not media.probe(source).has_audio:
        log.warning("%s n'a aucune piste audio : compression seule", source.name)
        return _compress_only(source, output_dir, target_gb, max_mb, split, subdir, on_progress)

    # La fin de la transcription n'est pas la fin du job : pas de « done » à mi-course.
    transcribed = transcription(
        source, output_dir=output_dir, language=language,
        formats=formats, ocr=ocr, subdir=subdir, subtitle_language=subtitle_language,
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
        note=transcribed.note,
        segments=transcribed.segments,
        original_bytes=transcribed.original_bytes,
        final_bytes=compressed.final_bytes,
    )


def _compress_only(source, output_dir, target_gb, max_mb, split, subdir, on_progress) -> Result:
    """Compression seule, quand il n'y a pas de parole à transcrire."""
    result = compression(
        source, output_dir=output_dir, target_gb=target_gb, max_mb=max_mb,
        split=split, subdir=subdir, on_progress=on_progress,
    )
    result.note = "aucune piste audio : compressée sans transcription"
    return result


RUNNERS = {"transcribe": transcription, "compress": compression, "process": process}
