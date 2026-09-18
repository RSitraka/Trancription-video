"""Compression : calculs de débit, plan de découpage, et encodages réels."""

import pytest

from source import compress, media
from source.config import settings

MO = compress.MO


def info(duration=3600.0, size=10 * 1024**3, video=True, audio=True, bitrate=None):
    return media.MediaInfo(path=None, duration=duration, size=size, has_video=video,
                           has_audio=audio, video_codec="h264", audio_codec="aac",
                           width=1920, height=1080, bitrate=bitrate)


@pytest.mark.parametrize("total, audio", [
    (5000, 128), (1000, 128), (999, 96), (400, 96), (399, 64), (150, 64), (149, 48), (0, 48)])
def test_audio_kbps_for(total, audio):
    assert compress.audio_kbps_for(total) == audio


def test_part_path(tmp_path):
    assert compress.part_path(tmp_path / "cours_compressed.mp4", 3).name == "cours_compressed_3.mp4"


def test_plan_parts(monkeypatch):
    monkeypatch.setattr(settings, "split_min_kbps", 1000)
    limit = 300 * MO
    # 1 h sous 300 Mo ≈ 633 kbps < 1000 : découpage nécessaire.
    assert compress.plan_parts(info(), limit, split=True) == 2
    assert compress.plan_parts(info(), limit, split=False) == 1
    # 6 h de vidéo : chaque partie garde au moins 1000 kbps.
    parts = compress.plan_parts(info(duration=6 * 3600), limit, split=True)
    assert limit * compress.SAFETY * 8 / 1000 / (6 * 3600 / parts) >= 1000
    # Budget suffisant : une seule partie.
    assert compress.plan_parts(info(duration=600), limit, split=True) == 1
    # Source à faible débit : inutile d'exiger plus qu'elle n'a.
    assert compress.plan_parts(info(bitrate=500_000), limit, split=True) == 1
    # Audio seul : plancher de 64 kbps.
    assert compress.plan_parts(info(video=False, duration=12 * 3600), limit, True) == 2


def keyframes_every(count, size):
    return [(float(i), i * size) for i in range(count)], count * size


def test_plan_cuts_single_part():
    frames, total = keyframes_every(10, 1000)
    assert compress._plan_cuts(frames, total, total + compress.MP4_FIXED_BYTES) == []


@pytest.mark.parametrize("count, size, cap_parts", [(100, 10_000, 3.5), (57, 7_919, 2.2),
                                                     (400, 3_000, 7.9)])
def test_plan_cuts_minimal_and_balanced(count, size, cap_parts):
    frames, total = keyframes_every(count, size)
    cap = total / cap_parts + compress.MP4_FIXED_BYTES
    budget = cap - compress.MP4_FIXED_BYTES
    cuts = compress._plan_cuts(frames, total, cap)

    offsets = dict(frames)
    bounds = [0, *(offsets[t] for t in cuts), total]
    sizes = [b - a for a, b in zip(bounds, bounds[1:])]
    assert len(sizes) == int(cap_parts) + 1                    # nombre minimal
    assert all(s <= budget for s in sizes)                     # chaque partie tient
    assert cuts == sorted(cuts) and len(set(cuts)) == len(cuts)
    assert max(sizes) - min(sizes) <= 2 * size                 # parties équilibrées


def test_plan_cuts_gap_too_large():
    frames = [(0.0, 0), (1.0, 5_000_000)]
    with pytest.raises(media.FFmpegError, match="images-clés"):
        compress._plan_cuts(frames, 6_000_000, 1_000_000)


def test_encode_to_size_refuses_impossible_budget(tmp_path):
    with pytest.raises(ValueError, match="ne suffisent pas"):
        compress._encode_to_size(tmp_path / "s.mp4", tmp_path / "d.mp4", info(duration=36000),
                                 0, 36000, 10 * MO, "libx264", None)
    with pytest.raises(ValueError, match="d'audio"):
        compress._encode_to_size(tmp_path / "s.m4a", tmp_path / "d.m4a",
                                 info(duration=36000, video=False), 0, 36000, 1 * MO, "aac", None)


def test_encode_to_size_retries_then_gives_up(tmp_path, monkeypatch):
    budgets = []

    def too_big(source, destination, info_, codec, start, length, video_kbps, audio_kbps, _):
        budgets.append(video_kbps)
        destination.write_bytes(b"x" * 2000)

    monkeypatch.setattr(compress, "_encode", too_big)
    target = tmp_path / "d.mp4"
    with pytest.raises(RuntimeError, match="après 3 essais"):
        compress._encode_to_size(tmp_path / "s.mp4", target, info(duration=0.001), 0, 0.001,
                                 1000, "libx264", None)
    assert len(budgets) == compress.MAX_ATTEMPTS
    assert budgets == sorted(budgets, reverse=True)            # budget réduit à chaque essai
    assert not target.exists()


# --- Encodages réels ---------------------------------------------------------

@pytest.mark.ffmpeg
def test_copy_when_already_under_limit(clips, tmp_path):
    progress = []
    [out] = compress.compress(clips.video, tmp_path / "c.mp4", max_mb=100,
                              on_progress=progress.append)
    assert out.read_bytes() == clips.video.read_bytes()
    assert progress == [1.0]


@pytest.mark.ffmpeg
def test_crf_encoding(clips, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "preset", "ultrafast")
    [out] = compress.compress(clips.video, tmp_path / "crf.mp4", codec="libx264")
    result = media.probe(out)
    assert result.video_codec == "h264" and result.has_audio
    assert result.duration == pytest.approx(4, abs=0.2)


@pytest.mark.ffmpeg
def test_split_copy_parts_under_limit(clips, tmp_path):
    source = media.probe(clips.heavy)
    limit_mb = source.size / MO / 2.5
    outputs = compress.compress(clips.heavy, tmp_path / "heavy_compressed.mp4",
                                max_mb=limit_mb, split=True)
    assert len(outputs) >= 3
    assert [p.name for p in outputs] == [
        f"heavy_compressed_{i}.mp4" for i in range(1, len(outputs) + 1)]
    assert all(p.stat().st_size <= limit_mb * MO for p in outputs)
    total = sum(media.probe(p).duration for p in outputs)
    assert total == pytest.approx(source.duration, abs=0.5)    # rien de perdu
    assert not list(tmp_path.glob(".*"))                       # dossier de travail nettoyé


@pytest.mark.ffmpeg
def test_encode_to_size_single_file(clips, tmp_path):
    source = media.probe(clips.heavy)
    limit = int(source.size * 0.5)
    [out] = compress.compress_to_size(clips.heavy, tmp_path / "one.mp4", limit / MO,
                                      "libx264", None, source, split=False)
    assert out.stat().st_size <= limit
    assert media.probe(out).duration == pytest.approx(source.duration, abs=0.5)
    assert not list(tmp_path.glob("*.2pass*"))                 # journaux de passe purgés


@pytest.mark.ffmpeg
def test_audio_only_to_size(clips, tmp_path):
    source = media.probe(clips.audio_only)
    limit = int(source.size * 0.6)
    [out] = compress.compress_to_size(clips.audio_only, tmp_path / "a.m4a", limit / MO,
                                      "libx264", None, source, split=False)
    result = media.probe(out)
    assert out.stat().st_size <= limit and result.has_audio and not result.has_video


def test_compress_to_size_checks_output_space(tmp_path, monkeypatch):
    from types import SimpleNamespace

    encoded = []
    monkeypatch.setattr(compress, "_encode_to_size", lambda *a, **k: encoded.append(a))
    monkeypatch.setattr(media.shutil, "disk_usage",
                        lambda path: SimpleNamespace(total=0, used=0, free=2 * 10**9))
    source = info(duration=300 * 3600, size=6 * 1000**4)                # 6 To, 300 h
    with pytest.raises(media.DiskSpaceError, match="fichiers compressés"):
        compress.compress_to_size(tmp_path / "s.mp4", tmp_path / "out" / "s_compressed.mp4",
                                  300, "libx264", None, source, split=True)
    assert encoded == []


def test_compress_space_check_counts_parts_already_done(tmp_path, monkeypatch):
    """Reprise : les parties déjà écrites ne sont pas comptées deux fois."""
    out = tmp_path / "sortie"
    out.mkdir()
    for i in (1, 2):
        (out / f"s_compressed_{i}.mp4").write_bytes(b"x" * 1000)
    needed = []
    monkeypatch.setattr(media, "require_space", lambda folder, n, what: needed.append(n))
    monkeypatch.setattr(compress, "_encode_to_size", lambda *a, **k: None)
    monkeypatch.setattr(compress, "plan_parts", lambda *a: 1)
    monkeypatch.setattr(compress, "_log_ratio", lambda *a: None)
    source = info(duration=60, size=10**9, video=False)
    compress.compress_to_size(tmp_path / "s.m4a", out / "s_compressed.mp4", 500, "aac", None, source)
    assert needed == [500 * MO - 2000]
