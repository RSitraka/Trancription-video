"""Plan de découpage : recouvrement, persistance, reprise."""

import pytest

from source import chunker, media
from source.config import settings


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(settings, "chunk_duration", 10)
    monkeypatch.setattr(settings, "chunk_overlap", 2)


def test_plan_covers_duration_with_overlap(tmp_path, small_chunks):
    plan = chunker.plan(tmp_path / "audio.wav", tmp_path / "work", duration=25)
    assert [(c.index, c.start, c.duration) for c in plan.chunks] == [
        (0, 0.0, 12), (1, 10.0, 12), (2, 20.0, 5)]
    assert all(c.status == "pending" for c in plan.chunks)
    assert plan.chunks[0].file.name == "chunk_0000.wav"
    assert (tmp_path / "work" / chunker.STATE_FILE).exists()


def test_plan_exact_multiple(tmp_path, small_chunks):
    plan = chunker.plan(tmp_path / "a.wav", tmp_path / "w", duration=20)
    assert [c.start for c in plan.chunks] == [0, 10]
    assert plan.chunks[-1].duration == 10


def test_plan_is_resumed_from_disk(tmp_path, small_chunks):
    work = tmp_path / "work"
    plan = chunker.plan(tmp_path / "audio.wav", work, duration=25)
    plan.mark(plan.chunks[0], "done")

    # Un changement de réglage ne doit pas casser un travail déjà entamé.
    settings.chunk_duration = 5
    again = chunker.plan(tmp_path / "audio.wav", work, duration=25)
    assert len(again.chunks) == 3
    assert again.chunks[0].status == "done"
    assert again.progress == pytest.approx(1 / 3)
    assert [c.index for c in again.pending] == [1, 2]


def test_progress_of_empty_set(tmp_path):
    assert chunker.ChunkSet(tmp_path, []).progress == 0.0
    assert chunker.ChunkSet.load(tmp_path) is None


@pytest.mark.parametrize("delete", [True, False])
def test_cleanup_respects_setting(tmp_path, monkeypatch, delete):
    monkeypatch.setattr(settings, "delete_chunks_after", delete)
    wav = tmp_path / "chunk.wav"
    wav.write_bytes(b"RIFF")
    chunk = chunker.Chunk(index=0, path=str(wav), start=0, duration=1)
    chunker.ChunkSet(tmp_path, [chunk]).cleanup(chunk)
    assert wav.exists() is not delete


@pytest.mark.ffmpeg
def test_materialize_extracts_window(clips, tmp_path):
    audio = media.extract_audio(clips.video, tmp_path / "audio.wav")
    chunk = chunker.Chunk(index=1, path=str(tmp_path / "c1.wav"), start=1.0, duration=2.0)
    path = chunker.materialize(audio, chunk)
    assert media.probe(path).duration == pytest.approx(2.0, abs=0.05)

    # Déjà présent : pas de nouvelle extraction.
    before = path.stat().st_mtime_ns
    chunker.materialize(audio, chunk)
    assert path.stat().st_mtime_ns == before
