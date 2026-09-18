"""Images explicatives : détection de changement, alignement, OCR simulé."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from source import frames
from source.config import settings
from source.frames import Frame
from source.transcribe import Segment


def test_changed_uses_share_of_pixels(monkeypatch):
    monkeypatch.setattr(settings, "scene_threshold", 1)
    blank = np.full((100, 100), 255, dtype=np.uint8)
    title = blank.copy()
    title[10:14, 10:60] = 0                                     # 2 % de l'image
    assert frames._changed(blank, title)
    assert not frames._changed(blank, blank)

    noise = blank.copy()
    noise[:] = 240                                              # écart sous le seuil de 25
    assert not frames._changed(blank, noise)

    monkeypatch.setattr(settings, "scene_threshold", 5)
    assert not frames._changed(blank, title)


def test_enrich_aligns_frames_to_speech():
    shots = [Frame(0, Path("f0.jpg"), ["Intro"]), Frame(10, Path("f10.jpg"), ["Plan"]),
             Frame(30, Path("f30.jpg"), ["Fin"])]
    segments = [Segment(1, 5, "a"), Segment(12, 20, "b"), Segment(25, 29, "c"),
                Segment(31, 35, "d")]
    frames.enrich(segments, list(reversed(shots)))              # ordre indifférent
    assert [(s.frame, s.ocr) for s in segments] == [
        ("f0.jpg", ["Intro"]), ("f10.jpg", ["Plan"]), ("f10.jpg", ["Plan"]),
        ("f30.jpg", ["Fin"])]


def test_enrich_without_frames():
    segments = [Segment(0, 1, "a")]
    assert frames.enrich(segments, []) is segments and segments[0].frame is None


def test_enrich_ignores_frame_after_segment():
    segments = [Segment(0, 2, "avant")]
    frames.enrich(segments, [Frame(5, Path("f.jpg"), ["x"])])
    assert segments[0].frame is None


@pytest.fixture
def slides(tmp_path):
    """Trois « diapositives » unies de 2 s chacune."""
    path = tmp_path / "slides.mp4"
    colors = ["white", "black", "blue"]
    inputs = [arg for c in colors
              for arg in ("-f", "lavfi", "-i", f"color=c={c}:size=160x120:rate=10:duration=2")]
    concat = "".join(f"[{i}:v]" for i in range(3)) + "concat=n=3:v=1[v]"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
                    "-filter_complex", concat, "-map", "[v]", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@pytest.mark.ffmpeg
def test_detect_scenes_keeps_only_changes(slides, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "frame_interval", 1)
    monkeypatch.setattr(settings, "scene_threshold", 10)
    shots = frames.detect_scenes(slides, tmp_path / "captures")
    assert [round(f.timestamp) for f in shots] == [0, 2, 4]
    assert all(f.path.is_file() for f in shots)


def test_detect_scenes_unreadable(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"rien")
    with pytest.raises(RuntimeError, match="Impossible d'ouvrir"):
        frames.detect_scenes(bad, tmp_path / "out")


@pytest.mark.ffmpeg
def test_analyze_survives_missing_ocr(slides, tmp_path, monkeypatch):
    def broken(frame, languages="fra+eng"):
        raise OSError("tesseract absent")

    monkeypatch.setattr(frames, "read_text", broken)
    segments, shots = frames.analyze(slides, tmp_path, [Segment(2.5, 3, "noir")])
    assert shots and all(f.text == [] for f in shots)
    assert segments[0].frame.endswith("frame_000002s.jpg")
    assert (tmp_path / "slides_frames").is_dir()


@pytest.mark.ffmpeg
def test_analyze_attaches_text(slides, tmp_path, monkeypatch):
    monkeypatch.setattr(frames, "read_text", lambda f, languages="fra+eng": [f.path.name])
    segments, _ = frames.analyze(slides, tmp_path, [Segment(4.2, 5, "bleu")])
    assert segments[0].ocr == ["frame_000004s.jpg"]
