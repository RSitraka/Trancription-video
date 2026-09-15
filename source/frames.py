"""Images explicatives : détection de changement de scène, OCR, alignement.

Optionnel — activé par ENABLE_OCR. Sert aux vidéos de cours ou de présentation,
où les diapositives portent une partie de l'information absente de l'audio.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from source.config import settings
from source.transcribe import Segment

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Frame:
    timestamp: float
    path: Path
    text: list[str]


def detect_scenes(video: Path, output_dir: Path) -> list[Frame]:
    """Échantillonne la vidéo et ne conserve que les images dont le contenu change.

    Extraire 25 images/s produirait ~900 000 fichiers quasi identiques pour 10 h
    de vidéo. On échantillonne toutes les `frame_interval` secondes et on compare
    à la dernière image retenue.
    """
    import cv2

    output_dir.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise RuntimeError(f"Impossible d'ouvrir {video}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(int(fps * settings.frame_interval), 1)

    frames: list[Frame] = []
    previous = None
    index = 0

    try:
        while True:
            ok, image = capture.read()
            if not ok:
                break

            if index % step == 0:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                changed = previous is None or _changed(previous, gray)
                if changed:
                    path = output_dir / f"frame_{int(index / fps):06d}s.jpg"
                    cv2.imwrite(str(path), image)
                    frames.append(Frame(timestamp=index / fps, path=path, text=[]))
                    previous = gray

            index += 1
    finally:
        capture.release()

    log.info("%d images retenues sur %d analysées", len(frames), index // step)
    return frames


def _changed(previous, current) -> bool:
    """Compare deux images sur la *proportion* de pixels modifiés.

    La moyenne des écarts, elle, rate les changements de diapositive : quand
    seul le texte change sur un fond blanc, les pixels touchés représentent
    quelques pour cent de l'image et la moyenne reste sous n'importe quel seuil.
    """
    import cv2
    import numpy as np

    difference = cv2.absdiff(previous, current)
    moved = float(np.count_nonzero(difference > 25)) / difference.size
    return moved * 100 > settings.scene_threshold


def read_text(frame: Frame, languages: str = "fra+eng") -> list[str]:
    """Lit le texte affiché (diapositives, code, tableaux) via Tesseract."""
    import pytesseract
    from PIL import Image

    raw = pytesseract.image_to_string(Image.open(frame.path), lang=languages)
    return [line.strip() for line in raw.splitlines() if line.strip()]


def enrich(segments: list[Segment], frames: list[Frame]) -> list[Segment]:
    """Rattache chaque image à la parole prononcée au même instant."""
    if not frames:
        return segments

    ordered = sorted(frames, key=lambda f: f.timestamp)
    cursor = 0

    for segment in segments:
        # Dernière image affichée au moment où le segment commence.
        while cursor + 1 < len(ordered) and ordered[cursor + 1].timestamp <= segment.start:
            cursor += 1
        current = ordered[cursor]
        if current.timestamp <= segment.end:
            segment.frame = str(current.path)
            segment.ocr = list(current.text)

    return segments


def analyze(
    video: Path, output_dir: Path, segments: list[Segment]
) -> tuple[list[Segment], list[Frame]]:
    """Chaîne complète : détection de scènes → OCR → alignement sur la parole.

    Les captures sont écrites à côté des transcriptions, et non dans le dossier
    de travail : celui-ci est purgé, elles doivent survivre au traitement.
    """
    frames = detect_scenes(video, output_dir / f"{video.stem}_frames")
    for frame in frames:
        try:
            frame.text = read_text(frame)
        except Exception as error:          # OCR indisponible : on garde l'image
            log.warning("OCR indisponible (%s)", error)
            break
    return enrich(segments, frames), frames
