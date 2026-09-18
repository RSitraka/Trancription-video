"""Enveloppe FFmpeg : sondage, exécution, extraction audio, parcours de dossiers."""

import wave

import pytest

from source import media


@pytest.mark.parametrize("seconds, separator, expected", [
    (0, ",", "00:00:00,000"),
    (1.5, ",", "00:00:01,500"),
    (61.001, ".", "00:01:01.001"),
    (3600 * 25 + 0.9994, ",", "25:00:00,999"),
    (59.9996, ",", "00:01:00,000"),        # l'arrondi reporte sur la minute
    (-3, ",", "00:00:00,000"),             # jamais de temps négatif
])
def test_format_timestamp(seconds, separator, expected):
    assert media.format_timestamp(seconds, separator) == expected


def test_iter_media_filters_extensions_and_hidden(tmp_path):
    for name in ("b.MP4", "a.mp3", "notes.txt", "cover.jpg", "sub/c.mkv",
                 ".cache/hidden.mp4", "sub/.x.mp4"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    names = [p.relative_to(tmp_path).as_posix() for p in media.iter_media(tmp_path)]
    assert names == ["a.mp3", "b.MP4", "sub/c.mkv"]


def test_media_extensions_cover_audio_and_video():
    assert {".mp4", ".mkv", ".mp3", ".wav"} <= media.MEDIA_EXTENSIONS
    assert not media.AUDIO_EXTENSIONS & media.VIDEO_EXTENSIONS


def test_probe_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        media.probe(tmp_path / "absent.mp4")


@pytest.mark.ffmpeg
def test_probe_rejects_unreadable_file(tmp_path):
    fake = tmp_path / "faux.mp4"
    fake.write_bytes(b"pas une video" * 100)
    with pytest.raises(media.FFmpegError):
        media.probe(fake)


@pytest.mark.ffmpeg
def test_probe_video(clips):
    info = media.probe(clips.video)
    assert info.has_video and info.has_audio
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert (info.width, info.height) == (320, 240)
    assert info.duration == pytest.approx(4, abs=0.2)
    assert info.size == clips.video.stat().st_size
    assert info.bitrate and info.bitrate > 0
    assert info.size_gb == info.size / 1024**3


@pytest.mark.ffmpeg
def test_probe_audio_only_and_silent(clips):
    audio = media.probe(clips.audio_only)
    assert audio.has_audio and not audio.has_video and audio.width is None
    silent = media.probe(clips.silent)
    assert silent.has_video and not silent.has_audio


@pytest.mark.ffmpeg
def test_run_reports_progress(clips, tmp_path):
    seen = []
    media.run(["-i", str(clips.video), "-c", "copy", str(tmp_path / "copie.mp4")],
              duration=4, on_progress=seen.append)
    assert (tmp_path / "copie.mp4").stat().st_size > 0
    assert seen and all(0 <= p <= 1 for p in seen)
    assert seen == sorted(seen)


@pytest.mark.ffmpeg
def test_run_raises_on_failure(tmp_path):
    with pytest.raises(media.FFmpegError, match="code"):
        media.run(["-i", str(tmp_path / "absent.mp4"), str(tmp_path / "x.mp4")])


@pytest.mark.ffmpeg
def test_extract_audio_16k_mono(clips, tmp_path):
    target = media.extract_audio(clips.video, tmp_path / "sub" / "audio.wav")
    with wave.open(str(target)) as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() / 16000 == pytest.approx(4, abs=0.2)


@pytest.mark.ffmpeg
def test_extract_audio_without_audio_track(clips, tmp_path):
    with pytest.raises(media.FFmpegError, match="aucune piste audio"):
        media.extract_audio(clips.silent, tmp_path / "audio.wav")


def test_extract_audio_uses_rf64_beyond_4_gb(tmp_path, monkeypatch):
    """Au-delà de ~37 h d'audio, un WAV classique ne peut plus indiquer sa taille."""
    calls = []
    monkeypatch.setattr(media, "probe", lambda path: media.MediaInfo(
        path, 40 * 3600, 1, True, True, "h264", "aac", 1, 1, 1))
    monkeypatch.setattr(media, "run", lambda args, **kwargs: calls.append(args))
    media.extract_audio(tmp_path / "long.mp4", tmp_path / "audio.wav")
    args = calls[0]
    assert args[args.index("-rf64") + 1] == "auto"


def test_require_space(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(media.shutil, "disk_usage",
                        lambda path: SimpleNamespace(total=0, used=0, free=5 * 10**9))
    media.require_space(tmp_path / "sous", 3 * 10**9, "un essai")          # 3 + 1 Go de marge ≤ 5
    assert (tmp_path / "sous").is_dir()
    with pytest.raises(media.DiskSpaceError, match="la piste audio : 4.5 Go nécessaires"):
        media.require_space(tmp_path, int(4.5e9), "la piste audio")
