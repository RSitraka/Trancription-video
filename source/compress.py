"""Compression vidéo : CRF (qualité constante) ou deux passes (taille visée)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from source import media
from source.config import settings

log = logging.getLogger(__name__)

# Encodeurs matériels : ~10x plus rapides, ~20 % plus gros à qualité égale.
HARDWARE_CODECS = {"hevc_nvenc", "h264_nvenc", "av1_nvenc"}


def target_bitrate(duration: float, target_gb: float, audio_kbps: int = 128) -> int:
    """Débit vidéo (kbps) permettant de tenir dans la taille demandée."""
    if duration <= 0:
        raise ValueError("durée inconnue : impossible de viser une taille")
    total_kbits = target_gb * 1024**3 * 8 / 1000
    video_kbps = total_kbits / duration - audio_kbps
    if video_kbps < 100:
        raise ValueError(
            f"taille cible trop basse : {video_kbps:.0f} kbps pour "
            f"{duration / 3600:.1f} h de vidéo"
        )
    return int(video_kbps)


def compress(
    source: Path,
    destination: Path,
    target_gb: float | None = None,
    codec: str | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Ré-encode la vidéo. Sans `target_gb`, encode en CRF (qualité constante)."""
    info = media.probe(source)
    codec = codec or settings.video_codec
    destination.parent.mkdir(parents=True, exist_ok=True)

    audio_args = ["-c:a", "aac", "-b:a", settings.audio_bitrate]
    common = ["-i", str(source), "-movflags", "+faststart"]

    # Une cible plus large que la source ferait *grossir* le fichier : dans ce
    # cas le CRF est toujours préférable.
    if target_gb is not None and target_gb >= info.size_gb:
        log.warning(
            "Taille cible (%.1f Go) supérieure à la source (%.1f Go) : passage en CRF",
            target_gb, info.size_gb,
        )
        target_gb = None

    if target_gb is None:
        quality = (
            ["-cq", str(settings.crf)] if codec in HARDWARE_CODECS
            else ["-crf", str(settings.crf)]
        )
        log.info("Compression CRF %s en %s", settings.crf, codec)
        media.run(
            [*common, "-c:v", codec, *quality, "-preset", settings.preset,
             *audio_args, str(destination)],
            duration=info.duration,
            on_progress=on_progress,
        )
    else:
        kbps = target_bitrate(
            info.duration, target_gb, int(settings.audio_bitrate.rstrip("k"))
        )
        log.info("Compression deux passes vers %.0f Go (%d kbps)", target_gb, kbps)
        passlog = str(destination.with_suffix(".2pass"))

        # Passe 1 : analyse seule, aucune sortie écrite.
        media.run(
            [*common, "-c:v", codec, "-b:v", f"{kbps}k", "-preset", settings.preset,
             "-pass", "1", "-passlogfile", passlog, "-an", "-f", "null", "/dev/null"],
            duration=info.duration,
            on_progress=(lambda p: on_progress(p * 0.5)) if on_progress else None,
        )
        # Passe 2 : encodage réel, répartition optimale du débit.
        media.run(
            [*common, "-c:v", codec, "-b:v", f"{kbps}k", "-preset", settings.preset,
             "-pass", "2", "-passlogfile", passlog, *audio_args, str(destination)],
            duration=info.duration,
            on_progress=(lambda p: on_progress(0.5 + p * 0.5)) if on_progress else None,
        )
        for leftover in destination.parent.glob(f"{destination.stem}.2pass*"):
            leftover.unlink()

    result = media.probe(destination)
    log.info(
        "%.1f Go → %.1f Go (÷ %.1f)",
        info.size_gb, result.size_gb, info.size / max(result.size, 1),
    )
    return destination
