#!/usr/bin/env python3
"""Interface en ligne de commande du pipeline de transcription/compression.

Exemples (depuis l'hôte, tout s'exécute en conteneur) :

    docker compose run --rm cli python main.py transcribe /media/cours.mp4 --lang fr
    docker compose run --rm cli python main.py compress   /media/cours.mp4 --target-gb 450
    docker compose run --rm cli python main.py process    /media/cours.mp4 --formats srt,json
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from source import media, pipeline
from source.config import settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Transcription et compression de fichiers audio/vidéo volumineux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("source", type=Path, help="fichier source (ex. /media/cours.mp4)")
        sub.add_argument("--out", type=Path, default=None, help="dossier de sortie")

    info = subparsers.add_parser("info", help="afficher les métadonnées du fichier")
    info.add_argument("source", type=Path)

    export = subparsers.add_parser(
        "export", help="regénérer des formats depuis un .json déjà produit")
    export.add_argument("source", type=Path, help="fichier .json de transcription")
    export.add_argument("--formats", default="rag", help="srt,vtt,json,txt,csv,rag")
    export.add_argument("--out", type=Path, default=None)

    index = subparsers.add_parser("index", help="indexer un .rag.jsonl dans Qdrant")
    index.add_argument("source", type=Path, help="fichier .rag.jsonl")
    index.add_argument("--collection", default=None)

    search = subparsers.add_parser("search", help="rechercher dans les transcriptions")
    search.add_argument("question", help="question en langage naturel")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--collection", default=None)

    transcribe = subparsers.add_parser("transcribe", help="transcrire uniquement")
    common(transcribe)
    transcribe.add_argument("--lang", default=None,
                            help="code langue (fr, en, es...) ou 'auto' pour détecter")
    transcribe.add_argument("--formats", default="srt,json", help="srt,vtt,json,txt,csv")
    transcribe.add_argument("--ocr", action="store_true", help="analyser les images")

    compress = subparsers.add_parser("compress", help="compresser uniquement")
    common(compress)
    compress.add_argument("--target-gb", type=float, default=None, help="taille visée")
    compress.add_argument("--max-mb", type=float, default=None,
                          help="taille maximale garantie en Mo (ex. 300 pour le RAG)")
    compress.add_argument("--codec", default=None, help="libx265, hevc_nvenc, ...")
    compress.add_argument("--no-split", action="store_true",
                          help="un seul fichier même si la vidéo est trop longue")

    process = subparsers.add_parser("process", help="transcrire puis compresser")
    common(process)
    process.add_argument("--lang", default=None, help="code langue ou 'auto'")
    process.add_argument("--formats", default="srt,json")
    process.add_argument("--ocr", action="store_true")
    process.add_argument("--target-gb", type=float, default=None)
    process.add_argument("--max-mb", type=float, default=None)

    return parser


def progress_printer():
    """Barre de progression sur une seule ligne, utilisable hors terminal."""
    state = {"stage": "", "shown": -1}

    def report(stage: str, value: float) -> None:
        percent = int(value * 100)
        if stage == state["stage"] and percent == state["shown"]:
            return
        state.update(stage=stage, shown=percent)
        bar = "█" * (percent // 4) + "·" * (25 - percent // 4)
        end = "\n" if stage == "done" else ""
        print(f"\r{stage:<14} [{bar}] {percent:3d} %{end}", end="", flush=True)

    return report


def show_info(source: Path) -> int:
    data = media.probe(source)
    print(f"Fichier      : {data.path}")
    print(f"Taille       : {data.size_gb:.2f} Go")
    print(f"Durée        : {data.duration / 3600:.2f} h ({data.duration:.0f} s)")
    print(f"Vidéo        : {data.video_codec or '—'} {data.width or ''}x{data.height or ''}")
    print(f"Audio        : {data.audio_codec or '—'}")
    if data.bitrate:
        print(f"Débit global : {data.bitrate / 1_000_000:.1f} Mbps")
    print(f"Morceaux     : ~{max(int(data.duration // settings.chunk_duration) + 1, 1)}")
    return 0


def reexport(args) -> int:
    """Produit d'autres formats à partir d'un JSON existant, sans retranscrire."""
    import json

    from source import export
    from source.transcribe import Segment

    payload = json.loads(args.source.read_text(encoding="utf-8"))
    segments = [Segment(**item) for item in payload["segments"]]
    destination = (args.out or args.source.parent) / args.source.stem
    formats = [f.strip() for f in args.formats.split(",") if f.strip()]

    for path in export.write(segments, destination, formats):
        print(f"  → {path}")
    print(f"{len(segments)} segments réexportés.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.command == "info":
        return show_info(args.source)

    if args.command == "export":
        return reexport(args)

    if args.command == "index":
        from source import rag

        count = rag.index(args.source, args.collection)
        print(f"{count} passages indexés.")
        return 0

    if args.command == "search":
        from source import rag

        for hit in rag.search(args.question, args.limit, args.collection):
            print(f"\n[{hit['score']}] {hit['timecode']}  ({hit['source']})")
            print("  " + hit["text"][:400] + ("…" if len(hit["text"]) > 400 else ""))
        return 0

    if not args.source.exists():
        print(f"Fichier introuvable : {args.source}", file=sys.stderr)
        print("Rappel : utiliser le chemin conteneur (/media/...), pas le chemin hôte.",
              file=sys.stderr)
        return 1

    report = progress_printer()
    kwargs: dict = {"output_dir": args.out, "on_progress": report}

    if args.command in ("transcribe", "process"):
        kwargs["language"] = args.lang
        kwargs["formats"] = [f.strip() for f in args.formats.split(",") if f.strip()]
        kwargs["ocr"] = args.ocr or None
    if args.command in ("compress", "process"):
        kwargs["target_gb"] = args.target_gb
        kwargs["max_mb"] = args.max_mb
    if args.command == "compress":
        kwargs["split"] = not args.no_split
        kwargs["codec"] = args.codec

    try:
        result = pipeline.RUNNERS[args.command](args.source, **kwargs)
    except KeyboardInterrupt:
        print("\nInterrompu — la reprise repartira du dernier morceau traité.")
        return 130
    except Exception as error:
        print(f"\nÉchec : {error}", file=sys.stderr)
        return 1

    print(f"\nSource   : {result.original_gb:.3f} Go")
    if result.ratio is not None:
        print(f"Compressé: {result.final_gb:.3f} Go (÷ {result.ratio:.1f})")
    if result.segments:
        print(f"Segments : {result.segments}")
    for output in result.outputs:
        print(f"  → {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
