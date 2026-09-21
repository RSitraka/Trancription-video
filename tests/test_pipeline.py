"""Orchestration : dossiers de sortie, enchaînement et progression."""

from pathlib import Path

import pytest

from source import pipeline, transcribe
from source.transcribe import Segment


def test_runners():
    assert set(pipeline.RUNNERS) == {"transcribe", "compress", "process"}


def test_title_dir(tmp_path):
    folder = pipeline.title_dir(tmp_path, Path("/media/Cours 1.mp4"), "module/a")
    assert folder == (tmp_path / "module" / "a" / "Cours 1").resolve()
    assert folder.is_dir()
    assert pipeline.title_dir(tmp_path, Path("x.mkv")) == (tmp_path / "x").resolve()


@pytest.mark.parametrize("subdir", ["../evil", "a/../../evil", "/etc"])
def test_title_dir_rejects_escape(tmp_path, subdir):
    with pytest.raises(ValueError, match="invalide"):
        pipeline.title_dir(tmp_path / "out", Path("x.mp4"), subdir)


def test_result_ratio():
    result = pipeline.Result(Path("x"), original_bytes=10 * 1024**3, final_bytes=1024**3)
    assert result.ratio == 10 and result.original_gb == 10 and result.final_gb == 1
    empty = pipeline.Result(Path("x"), original_bytes=5)
    assert empty.ratio is None and empty.final_gb is None


@pytest.fixture
def fake_transcription(monkeypatch):
    def transcribe_media(source, work_dir, language=None, on_progress=None):
        on_progress(0.5)
        on_progress(1.0)
        return [Segment(0, 1, f"langue={language}")]

    monkeypatch.setattr(transcribe, "transcribe_media", transcribe_media)


@pytest.mark.ffmpeg
def test_transcription_writes_formats(clips, dirs, fake_transcription):
    stages = []
    result = pipeline.transcription(
        clips.video, language="en", formats=["srt", "txt"], subdir="module",
        on_progress=lambda stage, p: stages.append((stage, p)))
    folder = dirs.out / "module" / "video"
    assert [p.name for p in result.outputs] == ["video.srt", "video.txt"]
    assert all(p.parent == folder.resolve() for p in result.outputs)
    assert (folder / "video.txt").read_text() == "langue=en"
    assert result.segments == 1
    assert stages[0] == ("extracting", 0.0) and stages[-1] == ("done", 1.0)


@pytest.mark.ffmpeg
def test_compression_refuses_audio_without_size(clips):
    with pytest.raises(ValueError, match="pas de piste vidéo"):
        pipeline.compression(clips.audio_only)


@pytest.mark.ffmpeg
def test_process_progress_is_monotonic(clips, dirs, fake_transcription):
    progress = []
    result = pipeline.process(
        clips.video, max_mb=500, formats=["json"],
        on_progress=lambda stage, p: progress.append((stage, p)))

    names = sorted(p.name for p in result.outputs)
    assert names == ["video.json", "video_compressed.mp4"]
    values = [p for _, p in progress]
    assert values == sorted(values)
    # « done » n'est annoncé qu'à la toute fin, pas après la transcription.
    assert [s for s, _ in progress].count("done") == 1 and progress[-1] == ("done", 1.0)
    assert result.ratio == pytest.approx(1.0)                  # copie : déjà sous 500 Mo


@pytest.mark.ffmpeg
def test_vague_file_name_is_titled_from_the_content(clips, dirs, monkeypatch, tmp_path):
    """« VID_20260918.mp4 » : dossier et fichiers prennent le titre du contenu."""
    source = tmp_path / "VID_20260918.mp4"
    source.write_bytes(clips.video.read_bytes())
    monkeypatch.setattr(transcribe, "transcribe_media", lambda *a, **k: [
        Segment(0, 4, "Dans cette vidéo, on va voir comment installer Docker sous Windows.")])

    result = pipeline.process(source, max_mb=500, formats=["srt"])

    assert result.title == "Installer Docker sous Windows"
    folder = dirs.out / result.title
    assert sorted(p.name for p in folder.iterdir()) == [
        "Installer Docker sous Windows.srt", "Installer Docker sous Windows_compressed.mp4"]
    assert all(Path(o).parent == folder.resolve() for o in result.outputs)


@pytest.mark.ffmpeg
def test_meaningful_file_name_is_untouched(clips, dirs, fake_transcription):
    """Un nom choisi par l'utilisateur ne doit pas être réécrit."""
    result = pipeline.transcription(clips.heavy, formats=["srt"])
    assert result.title == "heavy"
    assert (dirs.out / "heavy" / "heavy.srt").is_file()
