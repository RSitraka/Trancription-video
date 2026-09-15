"""Compression vidéo : CRF (qualité constante) ou taille maximale garantie.

Avec une taille maximale, une vidéo trop longue pour y tenir avec une image
correcte est découpée en parties, chacune sous la limite. C'est le seul moyen de
traiter un fichier de plusieurs To : 300 Mo ne suffisent pas à 6 h de vidéo,
même en 360p.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from source import media
from source.config import settings

log = logging.getLogger(__name__)

Progress = Callable[[float], None] | None

# Encodeurs matériels : ~10x plus rapides, ~20 % plus gros à qualité égale.
HARDWARE_CODECS = {"hevc_nvenc", "h264_nvenc", "av1_nvenc"}

# Mo décimal (10⁶ octets) : un fichier sous la limite l'est aussi en Mio.
MO = 1000**2

# Marge sous la limite : le débit d'un encodeur n'est tenu qu'à quelques % près,
# et le conteneur MP4 ajoute ses propres octets.
SAFETY = 0.95
MAX_ATTEMPTS = 3

# Découpage sans ré-encodage : la taille de chaque paquet est connue, seule
# l'enveloppe MP4 est estimée (index d'environ 20 octets par paquet, en-têtes).
# D'où une marge bien plus fine qu'à l'encodage.
CUT_SAFETY = 0.99
MP4_BYTES_PER_PACKET = 24
MP4_FIXED_BYTES = 256 * 1024

# Hauteur d'image maximale selon le débit vidéo disponible (kbps). Moins de
# pixels à débit égal donne une image nette plutôt qu'une bouillie de blocs.
HEIGHT_FOR_KBPS = [(2500, 1080), (1200, 720), (700, 540), (400, 480), (0, 360)]
MIN_VIDEO_KBPS = 40
MIN_AUDIO_KBPS = 24
# En deçà, découper pour paralléliser coûte plus (démarrages, recollage) qu'il
# ne rapporte.
MIN_PIECE_SECONDS = 300


def audio_kbps_for(total_kbps: float) -> int:
    """Débit audio selon le budget : la parole reste prioritaire, c'est elle que
    le RAG transcrit ensuite."""
    for threshold, kbps in ((1000, 128), (400, 96), (150, 64)):
        if total_kbps >= threshold:
            return kbps
    return 48


def compress(
    source: Path,
    destination: Path,
    target_gb: float | None = None,
    codec: str | None = None,
    on_progress: Progress = None,
    max_mb: float | None = None,
    split: bool = True,
) -> list[Path]:
    """Ré-encode la vidéo et renvoie le ou les fichiers produits.

    - `max_mb` : aucun fichier produit ne dépassera cette taille (Mo = 10⁶ octets).
      `split` autorise le découpage en parties quand une seule ne suffit pas.
    - `target_gb` : ancienne cible globale (TARGET_SIZE_GB), traitée pareil.
    - sinon : encodage CRF, qualité constante, taille libre.
    """
    info = media.probe(source)
    codec = codec or settings.video_codec
    destination.parent.mkdir(parents=True, exist_ok=True)

    if max_mb is None and target_gb is not None:
        max_mb = target_gb * 1024**3 / MO
    if max_mb is not None:
        return compress_to_size(source, destination, max_mb, codec, on_progress, info, split)

    quality = ["-cq", str(settings.crf)] if codec in HARDWARE_CODECS else ["-crf", str(settings.crf)]
    log.info("Compression CRF %s en %s", settings.crf, codec)
    media.run(
        ["-i", str(source), "-movflags", "+faststart", "-c:v", codec, *quality,
         "-preset", settings.preset, "-c:a", "aac", "-b:a", settings.audio_bitrate,
         str(destination)],
        duration=info.duration,
        on_progress=on_progress,
    )
    _log_ratio(info.size, [destination])
    return [destination]


def plan_parts(info: media.MediaInfo, limit: int, split: bool) -> int:
    """Nombre de parties nécessaires pour garder un débit correct dans chacune."""
    budget_kbps = limit * SAFETY * 8 / 1000 / info.duration
    floor = settings.split_min_kbps if info.has_video else 64
    # Inutile d'exiger plus de débit que la source n'en a.
    if info.bitrate:
        floor = min(floor, info.bitrate / 1000)
    if not split or budget_kbps >= floor:
        return 1
    return math.ceil(info.duration / (limit * SAFETY * 8 / 1000 / floor))


def compress_to_size(
    source: Path,
    destination: Path,
    max_mb: float,
    codec: str,
    on_progress: Progress,
    info: media.MediaInfo,
    split: bool = True,
) -> list[Path]:
    limit = int(max_mb * MO)

    # Déjà sous la limite : ré-encoder ne ferait que dégrader l'image.
    if info.size <= limit:
        log.info("%s fait déjà %.0f Mo ≤ %.0f Mo : copie sans ré-encodage",
                 source.name, info.size / MO, max_mb)
        shutil.copyfile(source, destination)
        if on_progress:
            on_progress(1.0)
        return [destination]

    if info.duration <= 0:
        raise ValueError("durée inconnue : impossible de viser une taille")

    parts = plan_parts(info, limit, split)

    # Source déjà compacte : la couper suffit. Ré-encoder prendrait des heures,
    # dégraderait l'image et ne ferait pas gagner de place. On coupe dès que
    # cela ne produit guère plus de fichiers qu'un ré-encodage.
    copy_parts = math.ceil(info.size / (limit * CUT_SAFETY))
    if split and info.has_video and copy_parts <= max(parts + 1, parts * 1.5):
        try:
            return split_copy(source, destination, info, limit, on_progress)
        except media.FFmpegError as error:
            # Codec que le MP4 n'accepte pas tel quel (WMV, MPEG-2…) : ré-encodage.
            log.warning("découpage sans ré-encodage impossible (%s) : ré-encodage",
                        str(error).splitlines()[0])

    if parts == 1:
        _encode_to_size(source, destination, info, 0.0, info.duration, limit, codec, on_progress)
        _log_ratio(info.size, [destination])
        return [destination]

    length = info.duration / parts
    log.info("%s : %.1f h découpées en %d parties de %.0f min, ≤ %.0f Mo chacune",
             source.name, info.duration / 3600, parts, length / 60, max_mb)

    outputs = []
    for index in range(parts):
        part = part_path(destination, index + 1)
        # Reprise : une partie complète n'est jamais refaite. L'écriture passe
        # par un nom temporaire, donc un fichier final est forcément entier. Le
        # nom ne porte plus le nombre de parties : la durée vérifie qu'il s'agit
        # bien d'une partie de ce découpage.
        if (part.exists() and part.stat().st_size <= limit
                and abs(media.probe(part).duration - length) < 2):
            log.info("partie %d/%d déjà faite, ignorée", index + 1, parts)
        else:
            pending = part.with_name(f"{part.stem}.encours{part.suffix}")
            _encode_to_size(
                source, pending, info, index * length, length, limit, codec,
                (lambda p, i=index: on_progress((i + p) / parts)) if on_progress else None,
            )
            pending.replace(part)
        outputs.append(part)
        if on_progress:
            on_progress((index + 1) / parts)

    _log_ratio(info.size, outputs)
    return outputs


def part_path(destination: Path, number: int) -> Path:
    """`cours_compressed.mp4` → `cours_compressed_1.mp4`, `_2`, …"""
    return destination.with_name(f"{destination.stem}_{number}{destination.suffix}")


def split_copy(
    source: Path,
    destination: Path,
    info: media.MediaInfo,
    limit: int,
    on_progress: Progress,
) -> list[Path]:
    """Coupe la source en parties ≤ `limit` sans ré-encoder (vitesse de lecture
    du disque, aucune perte). Les coupes tombent sur des images-clés."""
    work = destination.parent / f".{destination.stem}.decoupe"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    scan = (lambda p: on_progress(p * 0.4)) if on_progress else None
    cut = (lambda p: on_progress(0.4 + p * 0.6)) if on_progress else None
    try:
        # Les coupes suivent la taille réelle des données, pas la durée : un
        # passage animé pèse plus qu'un plan fixe. Chaque coupe tombe sur une
        # image-clé, la seule position possible sans ré-encoder.
        keyframes, total = _scan_keyframes(source, info.duration, scan)
        times = _plan_cuts(keyframes, total, limit * CUT_SAFETY)
        log.info("%s : découpage sans ré-encodage en %d parties d'environ %.0f Mo",
                 source.name, len(times) + 1, total / (len(times) + 1) / MO)
        pieces = _segment(source, work / "p", times, info.duration, cut)

        # Estimation déjouée (index MP4 plus gros que prévu) : seule la partie
        # en trop est recoupée.
        final: list[Path] = []
        for piece in pieces:
            final.extend(_fit(piece, limit))

        outputs = []
        for index, piece in enumerate(final, start=1):
            part = part_path(destination, index)
            piece.replace(part)
            outputs.append(part)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    _log_ratio(info.size, outputs)
    return outputs


def _scan_keyframes(source: Path, duration: float,
                    on_progress: Progress = None) -> tuple[list[tuple[float, int]], int]:
    """Images-clés de la vidéo : (instant en secondes depuis le début, octets
    écrits avant elle), et le total. Lit les en-têtes de paquets de la piste
    vidéo et de la piste audio conservées, sans rien décoder.

    Les paquets sont comptés dans l'ordre du fichier : c'est l'ordre dans lequel
    le découpage les range dans les parties."""
    streams = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type:format=start_time",
         "-of", "json", str(source)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(streams.stdout)
    kinds = [s["codec_type"] for s in data["streams"]]
    video = kinds.index("video")
    audio = kinds.index("audio") if "audio" in kinds else -1
    # ffmpeg ramène le début du fichier à 0 : les instants de coupe aussi.
    start = float(data.get("format", {}).get("start_time") or 0)

    keyframes: list[tuple[float, int]] = []
    total = 0
    proc = subprocess.Popen(
        ["ffprobe", "-v", "error", "-show_entries", "packet=stream_index,pts_time,size,flags",
         "-of", "csv=p=0", str(source)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1 << 16,
    )
    for count, line in enumerate(proc.stdout):
        stream, pts, size, flags = line.split(",", 4)[:4]
        index = int(stream)
        if index != video and index != audio:
            continue
        if index == video and "K" in flags and pts != "N/A":
            keyframes.append((float(pts) - start, total))
        total += int(size) + MP4_BYTES_PER_PACKET
        if on_progress and duration and count % 5000 == 0 and pts != "N/A":
            on_progress(min(max(float(pts) - start, 0) / duration, 1.0))
    _, stderr = proc.communicate()
    if proc.returncode != 0 or not keyframes:
        raise media.FFmpegError(f"lecture des images-clés impossible : {stderr.strip()[-500:]}")
    return keyframes, total


def _plan_cuts(keyframes: list[tuple[float, int]], total: int, cap: float) -> list[float]:
    """Instants de coupe donnant le moins de parties possible sous `cap` octets,
    de tailles aussi proches que possible.

    1. Au plus loin : chaque partie s'arrête à la dernière image-clé qui la
       garde sous la limite. Cela donne le nombre minimal de parties, n.
    2. Équilibrage : la k-ième coupe vise k × total / n, à l'image-clé la plus
       proche parmi celles qui laissent assez de place aux parties suivantes."""
    offsets = [offset for _, offset in keyframes]
    budget = cap - MP4_FIXED_BYTES

    def latest(position: int) -> int:
        """Dernière image-clé à laquelle couper une partie commencée à `position`."""
        index = bisect.bisect_right(offsets, offsets[position] + budget) - 1
        if index <= position:
            raise media.FFmpegError("écart entre images-clés plus gros que la taille maximale")
        return index

    # 1. Nombre minimal de parties.
    greedy, position = [], 0
    while total - offsets[position] > budget:
        position = latest(position)
        greedy.append(position)
    parts = len(greedy) + 1
    if parts == 1:
        return []

    # Coupe au plus tôt possible pour chaque rang, en partant de la fin : ce qui
    # suit la coupe k doit tenir dans les parties restantes.
    earliest = [0] * parts
    bound = total - budget
    for k in range(parts - 1, 0, -1):
        earliest[k] = bisect.bisect_left(offsets, bound)
        bound = offsets[earliest[k]] - budget

    # 2. Équilibrage entre le plus tôt et le plus tard possible.
    cuts, position = [], 0
    for k in range(1, parts):
        low, high = max(earliest[k], position + 1), latest(position)
        ideal = bisect.bisect_left(offsets, k * total / parts, low, high + 1)
        choices = [i for i in (ideal - 1, ideal) if low <= i <= high] or [high]
        position = min(choices, key=lambda i: abs(offsets[i] - k * total / parts))
        cuts.append(position)
    return [keyframes[i][0] for i in cuts]


def _segment(source: Path, prefix: Path, times: list[float], duration: float,
             on_progress: Progress = None) -> list[Path]:
    # Coupe juste avant l'instant de l'image-clé : un arrondi ne doit pas la
    # faire glisser à l'image-clé suivante.
    split = (["-segment_times", ",".join(f"{max(t - 0.001, 0.001):.3f}" for t in times)]
             if times else ["-segment_time", f"{duration + 3600:.0f}"])
    media.run(
        ["-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
         "-f", "segment", *split, "-reset_timestamps", "1",
         "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
         f"{prefix}%04d.mp4"],
        duration=duration, on_progress=on_progress,
    )
    return sorted(prefix.parent.glob(f"{prefix.name}*.mp4"))


def _fit(piece: Path, limit: int, depth: int = 0) -> list[Path]:
    size = piece.stat().st_size
    if size <= limit:
        return [piece]
    if depth >= 3:
        raise media.FFmpegError(f"{piece.name} reste au-dessus de la limite après découpage")
    # Même découpage selon la taille, avec une marge élargie de l'écart constaté.
    duration = media.probe(piece).duration
    keyframes, total = _scan_keyframes(piece, duration)
    cap = limit * CUT_SAFETY * min(limit / size, 0.98) ** (depth + 1)
    pieces = _segment(piece, piece.with_name(piece.stem + "_"),
                      _plan_cuts(keyframes, total, cap), duration)
    piece.unlink()
    return [p for chunk in pieces for p in _fit(chunk, limit, depth + 1)]


def _encode_to_size(
    source: Path,
    destination: Path,
    info: media.MediaInfo,
    start: float,
    length: float,
    limit: int,
    codec: str,
    on_progress: Progress,
) -> None:
    """Encode [start, start + length] de la source en un fichier ≤ `limit` octets."""
    budget = limit * SAFETY
    for attempt in range(1, MAX_ATTEMPTS + 1):
        total_kbps = budget * 8 / 1000 / length
        if info.bitrate:                              # ne jamais gonfler la source
            total_kbps = min(total_kbps, info.bitrate / 1000)

        if info.has_video:
            audio_kbps = audio_kbps_for(total_kbps)
            video_kbps = int(total_kbps - audio_kbps)
            if video_kbps < MIN_VIDEO_KBPS:
                raise ValueError(
                    f"{limit / MO:.0f} Mo ne suffisent pas pour {length / 3600:.1f} h de vidéo "
                    f"({video_kbps} kbps). Active le découpage en parties ou augmente la taille."
                )
        else:
            audio_kbps, video_kbps = min(128, int(total_kbps)), 0
            if audio_kbps < MIN_AUDIO_KBPS:
                raise ValueError(
                    f"{limit / MO:.0f} Mo ne suffisent pas pour {length / 3600:.1f} h d'audio. "
                    "Active le découpage en parties ou augmente la taille."
                )

        log.info("%s [%.0f s +%.0f s] essai %d : vidéo %d kbps, audio %d kbps",
                 source.name, start, length, attempt, video_kbps, audio_kbps)
        _encode(source, destination, info, codec, start, length,
                video_kbps, audio_kbps, on_progress)

        size = destination.stat().st_size
        if size <= limit:
            return

        # L'encodeur a dépassé : on réduit le budget en proportion, avec un peu
        # plus de marge, et on recommence.
        log.warning("%.0f Mo produits > %.0f Mo autorisés : nouvel essai",
                    size / MO, limit / MO)
        budget *= (limit / size) * 0.93

    destination.unlink(missing_ok=True)
    raise RuntimeError(f"impossible de descendre sous {limit / MO:.0f} Mo après {MAX_ATTEMPTS} essais")


def _encode(
    source: Path,
    destination: Path,
    info: media.MediaInfo,
    codec: str,
    start: float,
    length: float,
    video_kbps: int,
    audio_kbps: int,
    on_progress: Progress,
) -> None:
    hardware = codec in HARDWARE_CODECS and info.has_video
    # -ss/-t avant -i : saut direct dans le fichier, sans décoder ce qui précède
    # (indispensable sur 2 To).
    window = ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"]
    outputs = ["-map", "0:v:0?", "-map", "0:a:0?", "-sn", "-dn", "-movflags", "+faststart"]
    common = [*window, "-i", str(source), *outputs]

    # En dessous de 64 kbps, la stéréo mange la moitié du débit pour rien.
    channels = ["-ac", "1"] if audio_kbps <= 64 else []
    audio = ["-c:a", "aac", "-b:a", f"{audio_kbps}k", *channels] if info.has_audio else ["-an"]

    if not info.has_video:
        media.run([*common, *audio, str(destination)], duration=length, on_progress=on_progress)
        return

    height = next(h for kbps, h in HEIGHT_FOR_KBPS if video_kbps >= kbps)
    filters = ["-vf", f"scale=-2:'min({height},ih)':flags=lanczos"]
    # Moins d'images par seconde = plus de débit par image quand il manque.
    if video_kbps < 600:
        filters += ["-fpsmax", "25"]
    rate = ["-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.5)}k",
            "-bufsize", f"{video_kbps * 2}k"]

    if hardware:
        def encode_piece(piece_start: float, piece_length: float, target: Path,
                         progress: Progress) -> None:
            window = ["-ss", f"{piece_start:.3f}", "-t", f"{piece_length:.3f}"]
            # NVENC n'a pas de `-pass` : la double analyse se fait en une passe.
            encoder = ["-c:v", codec, "-preset", "p5", "-tune", "hq", "-rc", "vbr",
                       "-multipass", "qres", *rate, *audio, str(target)]
            # Décodage, redimensionnement et encodage sur la carte : les images
            # ne repassent pas par le CPU, qui sinon plafonne la vitesse.
            on_gpu = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", *window,
                      "-i", str(source), *outputs,
                      "-vf", f"scale_cuda=-2:'min({height},ih)'", *filters[2:], *encoder]
            try:
                media.run(on_gpu, duration=piece_length, on_progress=progress)
            except media.FFmpegError as error:
                # Codec que la carte ne décode pas : décodage et redimensionnement CPU.
                log.warning("chaîne GPU complète impossible, repli CPU : %s",
                            str(error).splitlines()[-1][:200])
                media.run([*window, "-i", str(source), *outputs, *filters, *encoder],
                          duration=piece_length, on_progress=progress)

        _in_parallel(encode_piece, destination, start, length, on_progress)
        return

    # CPU : libx264 en deux passes, seul encodeur logiciel où `-pass` est fiable.
    passlog = str(destination.with_suffix(".2pass"))
    base = [*common, *filters, "-c:v", "libx264", "-preset", "medium", *rate,
            "-passlogfile", passlog]
    media.run([*base, "-pass", "1", "-an", "-f", "null", "/dev/null"], duration=length,
              on_progress=(lambda p: on_progress(p * 0.5)) if on_progress else None)
    media.run([*base, "-pass", "2", *audio, str(destination)], duration=length,
              on_progress=(lambda p: on_progress(0.5 + p * 0.5)) if on_progress else None)
    for leftover in destination.parent.glob(f"{destination.stem}.2pass*"):
        leftover.unlink()


def _in_parallel(
    encode_piece: Callable[[float, float, Path, Progress], None],
    destination: Path,
    start: float,
    length: float,
    on_progress: Progress,
) -> None:
    """Encode [start, start + length] en morceaux simultanés, puis les recolle
    sans ré-encoder. NVENC tient plusieurs sessions : 4 morceaux vont ~3x plus
    vite qu'un seul flux, dont le décodage et l'audio limitent la vitesse."""
    count = max(1, min(settings.encode_jobs, int(length // MIN_PIECE_SECONDS)))
    if count == 1:
        encode_piece(start, length, destination, on_progress)
        return

    work = destination.with_name(f".{destination.stem}.morceaux")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    piece = length / count
    done = [0.0] * count
    lock = threading.Lock()

    def report(index: int, value: float) -> None:
        with lock:
            done[index] = value
            total = sum(done) / count
        if on_progress:
            on_progress(total)

    def run(index: int) -> Path:
        target = work / f"{index:03d}.mp4"
        encode_piece(start + index * piece, piece, target, lambda p: report(index, p))
        return target

    try:
        with ThreadPoolExecutor(max_workers=count) as pool:
            pieces = list(pool.map(run, range(count)))
        listing = work / "liste.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in pieces), encoding="utf-8")
        media.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0",
                   "-c", "copy", "-movflags", "+faststart", str(destination)])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _log_ratio(original: int, outputs: list[Path]) -> None:
    total = sum(p.stat().st_size for p in outputs)
    log.info("%.0f Mo → %.0f Mo en %d fichier(s) (÷ %.1f)",
             original / MO, total / MO, len(outputs), original / max(total, 1))
