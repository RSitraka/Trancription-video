"""Transcription : modèle Whisper simulé, fusion, cache et reprise."""

from types import SimpleNamespace

import pytest

from source import transcribe
from source.config import settings
from source.transcribe import Segment


class FakeModel:
    """Imite faster-whisper : un segment par seconde d'audio, plus un vide."""

    def __init__(self, fail_on: int | None = None):
        self.calls = []
        self.fail_on = fail_on

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise RuntimeError("CUDA out of memory")
        segments = [
            SimpleNamespace(start=0.0, end=1.0, text=f"  morceau {len(self.calls)} ",
                            avg_logprob=-0.2),
            SimpleNamespace(start=1.0, end=1.5, text="   ", avg_logprob=-1.0),
        ]
        return iter(segments), SimpleNamespace(language="fr", language_probability=0.98)


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(transcribe, "get_model", lambda: fake)
    monkeypatch.setattr(settings, "whisper_batch_size", 0)
    return fake


def test_segment_shifted_is_a_copy():
    original = Segment(1, 2, "bonjour", 0.5, ["titre"], "f.jpg")
    moved = original.shifted(10)
    assert (moved.start, moved.end, moved.text, moved.frame) == (11, 12, "bonjour", "f.jpg")
    moved.ocr.append("autre")
    assert original.ocr == ["titre"]


def test_merge_removes_overlap_duplicates():
    groups = [
        [Segment(0, 5, "Bonjour"), Segment(5, 10, "on commence")],
        [Segment(9, 11, "On commence"), Segment(11, 14, "la suite")],
    ]
    merged = transcribe.merge(groups, overlap=2)
    assert [s.text for s in merged] == ["Bonjour", "on commence", "la suite"]


def test_merge_keeps_real_repetition():
    groups = [[Segment(0, 1, "oui")], [Segment(30, 31, "oui")]]
    assert len(transcribe.merge(groups, overlap=2)) == 2
    assert transcribe.merge([], overlap=2) == []


@pytest.mark.parametrize("requested, sent", [("auto", None), ("en", "en")])
def test_transcribe_file_language(model, tmp_path, requested, sent):
    segments = transcribe.transcribe_file(tmp_path / "c.wav", requested)
    assert model.calls[0][1]["language"] == sent
    assert [s.text for s in segments] == ["morceau 1"]        # texte vide écarté
    assert segments[0].confidence == -0.2


def test_transcribe_file_uses_default_language(model, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "language", "es")
    transcribe.transcribe_file(tmp_path / "c.wav")
    assert model.calls[0][1]["language"] == "es"
    assert model.calls[0][1]["vad_filter"] == settings.vad_filter


def test_transcribe_file_batched(monkeypatch, tmp_path):
    fake = FakeModel()
    monkeypatch.setattr(transcribe, "get_pipeline", lambda: fake)
    monkeypatch.setattr(settings, "whisper_batch_size", 8)
    transcribe.transcribe_file(tmp_path / "c.wav", "fr")
    assert fake.calls[0][1]["batch_size"] == 8


@pytest.mark.ffmpeg
def test_transcribe_media_offsets_and_resume(model, clips, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "chunk_duration", 3)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    monkeypatch.setattr(settings, "delete_chunks_after", True)
    work = tmp_path / "work"
    progress = []

    segments = transcribe.transcribe_media(clips.video, work, "fr", progress.append)
    assert [(s.start, s.text) for s in segments] == [(0.0, "morceau 1"), (3.0, "morceau 2")]
    assert progress[-1] == pytest.approx(1.0)
    assert not (work / "audio.wav").exists()                  # purgé
    assert not list(work.glob("chunk_*.wav"))
    assert len(list(work.glob("chunk_*.json"))) == 2          # cache conservé

    # Reprise : rien n'est retranscrit.
    again = transcribe.transcribe_media(clips.video, work, "fr")
    assert len(model.calls) == 2
    assert [s.text for s in again] == ["morceau 1", "morceau 2"]


@pytest.mark.ffmpeg
def test_transcribe_media_marks_failed_chunk(clips, tmp_path, monkeypatch):
    from source import chunker

    fake = FakeModel(fail_on=2)
    monkeypatch.setattr(transcribe, "get_model", lambda: fake)
    monkeypatch.setattr(settings, "whisper_batch_size", 0)
    monkeypatch.setattr(settings, "chunk_duration", 3)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    work = tmp_path / "work"

    with pytest.raises(RuntimeError):
        transcribe.transcribe_media(clips.video, work)
    state = chunker.ChunkSet.load(work)
    assert [c.status for c in state.chunks] == ["done", "failed"]


@pytest.mark.ffmpeg
def test_transcribe_media_refuses_when_disk_too_small(model, clips, tmp_path, monkeypatch):
    """Vérifié avant l'extraction : pas d'échec après des heures de lecture."""
    from types import SimpleNamespace

    from source import media

    extracted = []
    monkeypatch.setattr(media, "extract_audio", lambda *a, **k: extracted.append(a))
    monkeypatch.setattr(media.shutil, "disk_usage",
                        lambda path: SimpleNamespace(total=0, used=0, free=10**6))
    with pytest.raises(media.DiskSpaceError, match="piste audio"):
        transcribe.transcribe_media(clips.video, tmp_path / "work")
    assert extracted == [] and model.calls == []
