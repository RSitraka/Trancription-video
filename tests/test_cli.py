"""Ligne de commande (main.py)."""

import json
from pathlib import Path

import pytest

import main
from source import pipeline
from source.pipeline import Result


def test_parser_requires_command():
    with pytest.raises(SystemExit):
        main.build_parser().parse_args([])


def test_parser_compress_options():
    args = main.build_parser().parse_args(
        ["compress", "/media/a.mp4", "--max-mb", "300", "--no-split", "--codec", "hevc_nvenc"])
    assert (args.source, args.max_mb, args.no_split, args.codec) == (
        Path("/media/a.mp4"), 300.0, True, "hevc_nvenc")


def test_missing_file(capsys, tmp_path):
    assert main.main(["transcribe", str(tmp_path / "absent.mp4")]) == 1
    assert "chemin conteneur" in capsys.readouterr().err


@pytest.mark.ffmpeg
def test_info(clips, capsys):
    assert main.main(["info", str(clips.video)]) == 0
    out = capsys.readouterr().out
    assert "h264 320x240" in out and "Morceaux     : ~1" in out


def test_export_from_json(tmp_path, capsys):
    source = tmp_path / "cours.json"
    source.write_text(json.dumps({"segments": [
        {"start": 0, "end": 1, "text": "bonjour", "confidence": None, "ocr": [],
         "frame": None}]}), encoding="utf-8")
    assert main.main(["export", str(source), "--formats", "srt, txt", "--out",
                      str(tmp_path / "o")]) == 0
    assert (tmp_path / "o" / "cours.txt").read_text() == "bonjour"
    assert "1 segments réexportés" in capsys.readouterr().out


def test_compress_passes_options(tmp_path, monkeypatch, capsys):
    source = tmp_path / "cours.mp4"
    source.write_bytes(b"x" * 1000)
    received = {}

    def runner(path, **kwargs):
        received.update(kwargs)
        kwargs["on_progress"]("done", 1.0)
        return Result(path, outputs=[tmp_path / "c.mp4"], original_bytes=1000, final_bytes=100)

    monkeypatch.setitem(pipeline.RUNNERS, "compress", runner)
    assert main.main(["compress", str(source), "--max-mb", "300", "--no-split"]) == 0
    assert received["max_mb"] == 300 and received["split"] is False
    assert received["codec"] is None and received["target_gb"] is None
    assert "÷ 10.0" in capsys.readouterr().out


@pytest.mark.parametrize("error, code", [(KeyboardInterrupt, 130), (RuntimeError("x"), 1)])
def test_failures_exit_codes(tmp_path, monkeypatch, error, code):
    source = tmp_path / "cours.mp4"
    source.write_bytes(b"x")

    def runner(path, **kwargs):
        raise error

    monkeypatch.setitem(pipeline.RUNNERS, "process", runner)
    assert main.main(["process", str(source)]) == code


def test_progress_printer(capsys):
    report = main.progress_printer()
    report("compressing", 0.5)
    report("compressing", 0.5)                                  # pas de répétition
    report("done", 1.0)
    out = capsys.readouterr().out
    assert out.count("50 %") == 1 and out.endswith("100 %\n")
