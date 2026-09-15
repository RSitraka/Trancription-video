"""Enveloppe FFmpeg : sondage, extraction audio, exécution avec progression."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

_PROGRESS_TIME = re.compile(r"out_time_ms=(\d+)")


class FFmpegError(RuntimeError):
    pass


@dataclass(slots=True)
class MediaInfo:
    path: Path
    duration: float          # secondes
    size: int                # octets
    has_video: bool
    has_audio: bool
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    bitrate: int | None      # bits/s, global

    @property
    def size_gb(self) -> float:
        return self.size / 1024**3


def probe(path: Path | str) -> MediaInfo:
    """Lit les métadonnées sans décoder le fichier (constant, même pour 1 To)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    cmd = [
        "ffprobe", "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FFmpegError(f"ffprobe a échoué sur {path}: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    return MediaInfo(
        path=path,
        duration=float(fmt.get("duration") or 0.0),
        size=int(fmt.get("size") or path.stat().st_size),
        has_video=video is not None,
        has_audio=audio is not None,
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        width=int(video["width"]) if video and "width" in video else None,
        height=int(video["height"]) if video and "height" in video else None,
        bitrate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
    )


def run(
    args: Iterable[str],
    duration: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Exécute FFmpeg en relayant la progression (0..1) via -progress pipe:1."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args]
    if on_progress and duration:
        cmd += ["-progress", "pipe:1", "-nostats"]

    log.debug("ffmpeg: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )

    if on_progress and duration and proc.stdout:
        for line in proc.stdout:
            match = _PROGRESS_TIME.match(line.strip())
            if match:
                seconds = int(match.group(1)) / 1_000_000
                on_progress(min(seconds / duration, 1.0))

    _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg a échoué (code {proc.returncode}):\n{stderr[-2000:]}")


def extract_audio(
    source: Path,
    destination: Path,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Extrait la piste audio en WAV 16 kHz mono, format attendu par Whisper.

    Le fichier source est lu en flux : la mémoire utilisée ne dépend pas de
    sa taille.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    info = probe(source)
    if not info.has_audio:
        raise FFmpegError(f"{source} ne contient aucune piste audio")

    run(
        [
            "-i", str(source),
            "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(destination),
        ],
        duration=info.duration,
        on_progress=on_progress,
    )
    return destination


# Extensions reconnues lors du parcours d'un dossier. Un fichier non listé est
# ignoré plutôt que confié à ffmpeg : dans un dossier de plusieurs To, les
# .txt, .jpg et autres .nfo sont nombreux.
AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma",
    ".aiff", ".aif", ".amr", ".mka", ".weba",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".flv", ".3gp", ".3g2", ".ts", ".mts", ".m2ts", ".ogv",
    ".vob", ".asf", ".rm", ".rmvb", ".divx", ".f4v",
}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


def iter_media(folder: Path) -> Iterable[Path]:
    """Fichiers audio/vidéo d'un dossier et de ses sous-dossiers, triés."""
    for path in sorted(folder.rglob("*")):
        if (path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
                and not any(part.startswith(".") for part in path.relative_to(folder).parts)):
            yield path


def format_timestamp(seconds: float, separator: str = ",") -> str:
    """Formate en HH:MM:SS,mmm (SRT) ou HH:MM:SS.mmm (VTT)."""
    if seconds < 0:
        seconds = 0.0
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{milliseconds:03d}"
