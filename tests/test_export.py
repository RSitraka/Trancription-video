"""Formats de sortie : TXT, SRT, VTT, JSON, CSV et JSON Lines pour le RAG."""

import csv
import json
from pathlib import Path

import pytest

from source import export
from source.config import settings
from source.frames import Frame
from source.transcribe import Segment

SEGMENTS = [
    Segment(0.0, 2.5, "Bonjour à tous", -0.1),
    Segment(2.5, 3661.25, "L'architecture « RAG », virgule, guillemets \"", -0.3,
            ["Architecture RAG"], "/data/out/cours/cours_frames/frame_000002s.jpg"),
]


def write(tmp_path, fmt, segments=SEGMENTS, frames=None):
    [path] = export.write(segments, tmp_path / "sortie" / "cours", [fmt], frames=frames)
    return path


def test_unknown_format(tmp_path):
    with pytest.raises(ValueError, match="format inconnu"):
        export.write(SEGMENTS, tmp_path / "cours", ["docx"])


def test_suffixes(tmp_path):
    paths = export.write(SEGMENTS, tmp_path / "cours", list(export.FORMATS))
    assert [p.name for p in paths] == [
        "cours.txt", "cours.srt", "cours.vtt", "cours.json", "cours.csv", "cours.rag.jsonl"]


def test_txt(tmp_path):
    assert write(tmp_path, "txt").read_text(encoding="utf-8") == (
        "Bonjour à tous\nL'architecture « RAG », virgule, guillemets \"")


def test_srt(tmp_path):
    text = write(tmp_path, "srt").read_text(encoding="utf-8")
    assert text.startswith("1\n00:00:00,000 --> 00:00:02,500\nBonjour à tous\n\n2\n")
    assert "00:00:02,500 --> 01:01:01,250" in text


def test_vtt(tmp_path):
    lines = write(tmp_path, "vtt").read_text(encoding="utf-8").splitlines()
    assert lines[:4] == ["WEBVTT", "", "00:00:00.000 --> 00:00:02.500", "Bonjour à tous"]


def test_json_includes_frames(tmp_path):
    frame = Frame(12.345, Path("/x/cours_frames/frame_000012s.jpg"), ["Titre"])
    data = json.loads(write(tmp_path, "json", frames=[frame]).read_text(encoding="utf-8"))
    assert data["count"] == 2 and data["duration"] == 3661.25
    assert data["segments"][1]["ocr"] == ["Architecture RAG"]
    assert data["frames"] == [
        {"timestamp": 12.35, "image": "cours_frames/frame_000012s.jpg", "text": ["Titre"]}]


def test_json_empty(tmp_path):
    data = json.loads(write(tmp_path, "json", segments=[]).read_text(encoding="utf-8"))
    assert data == {"count": 0, "duration": 0.0, "segments": [], "frames": []}


def test_json_roundtrip_to_segments(tmp_path):
    data = json.loads(write(tmp_path, "json").read_text(encoding="utf-8"))
    assert [Segment(**item) for item in data["segments"]] == SEGMENTS


def test_csv_escapes_separators(tmp_path):
    with write(tmp_path, "csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["start", "end", "text", "ocr"]
    assert rows[2] == ["2.500", "3661.250", SEGMENTS[1].text, "Architecture RAG"]


def rag_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_rag_groups_segments_with_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "rag_chunk_chars", 20)
    monkeypatch.setattr(settings, "rag_overlap_segments", 1)
    segments = [Segment(i * 10, i * 10 + 9, f"phrase numero {i}") for i in range(5)]
    records = rag_records(write(tmp_path, "rag", segments=segments))

    assert [r["id"] for r in records] == [f"cours#{i:04d}" for i in range(len(records))]
    assert all(r["source"] == "cours" and r["kind"] == "speech" for r in records)
    assert records[0]["text"] == "phrase numero 0 phrase numero 1"
    assert records[1]["text"].startswith("phrase numero 1")      # recouvrement
    assert records[0]["timecode"] == "00:00:00.000"
    assert records[-1]["end"] == 49
    covered = {s for r in records for s in r["text"].split(" phrase ")}
    assert any("4" in c for c in covered)                         # rien de perdu


def test_rag_without_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "rag_chunk_chars", 10)
    monkeypatch.setattr(settings, "rag_overlap_segments", 0)
    segments = [Segment(i, i + 1, f"segment {i}") for i in range(3)]
    records = rag_records(write(tmp_path, "rag", segments=segments))
    assert [r["segments"] for r in records] == [1, 1, 1]


def test_rag_screen_passages_for_silent_slides(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "rag_chunk_chars", 10_000)
    shown = Frame(2.0, Path("/data/out/cours/cours_frames/frame_000002s.jpg"), ["Parlé"])
    silent = Frame(900.0, Path("/data/out/cours/cours_frames/frame_000900s.jpg"),
                   ["Diapo", "muette"])
    blank = Frame(950.0, Path("/data/out/cours/cours_frames/frame_000950s.jpg"), [])
    records = rag_records(write(tmp_path, "rag", frames=[shown, silent, blank]))

    speech, screen = records
    assert speech["frames"] == ["cours_frames/frame_000002s.jpg"]
    assert speech["screen_text"] == ["Architecture RAG"]
    assert screen == {
        "id": "cours#screen-cours_frames/frame_000900s.jpg", "text": "Diapo muette",
        "source": "cours", "kind": "screen", "start": 900.0, "end": 900.0,
        "timecode": "00:15:00.000", "screen_text": ["Diapo", "muette"],
        "frames": ["cours_frames/frame_000900s.jpg"]}


def test_rag_is_utf8_not_escaped(tmp_path):
    assert "« RAG »" in write(tmp_path, "rag").read_text(encoding="utf-8")


def test_dotted_titles_keep_their_full_name(tmp_path):
    """« Node.js tutorial » ne doit pas devenir « Node.srt »."""
    [srt] = export.write(SEGMENTS, tmp_path / "Node.js tutorial", ["srt"])
    assert srt.name == "Node.js tutorial.srt"
    [original] = export.write(SEGMENTS, tmp_path / "Cours.en", ["srt"])
    assert original.name == "Cours.en.srt"
