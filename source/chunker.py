"""Découpage de l'audio en morceaux traitables, avec recouvrement et reprise."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from source import media
from source.config import settings

log = logging.getLogger(__name__)

STATE_FILE = "chunks.json"


@dataclass(slots=True)
class Chunk:
    index: int
    path: str
    start: float             # offset absolu dans le média, en secondes
    duration: float          # durée réelle du morceau, recouvrement inclus
    status: str = "pending"  # pending | done | failed

    @property
    def file(self) -> Path:
        return Path(self.path)


class ChunkSet:
    """Plan de découpage persisté sur disque : une interruption se reprend."""

    def __init__(self, work_dir: Path, chunks: list[Chunk]):
        self.work_dir = work_dir
        self.chunks = chunks

    # --- persistance ---------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.work_dir / STATE_FILE

    def save(self) -> None:
        self.state_path.write_text(
            json.dumps([asdict(c) for c in self.chunks], indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, work_dir: Path) -> "ChunkSet | None":
        state = work_dir / STATE_FILE
        if not state.exists():
            return None
        raw = json.loads(state.read_text(encoding="utf-8"))
        return cls(work_dir, [Chunk(**item) for item in raw])

    # --- état ----------------------------------------------------------

    @property
    def pending(self) -> list[Chunk]:
        return [c for c in self.chunks if c.status != "done"]

    @property
    def progress(self) -> float:
        if not self.chunks:
            return 0.0
        done = sum(1 for c in self.chunks if c.status == "done")
        return done / len(self.chunks)

    def mark(self, chunk: Chunk, status: str) -> None:
        chunk.status = status
        self.save()

    def cleanup(self, chunk: Chunk) -> None:
        """Supprime le morceau dès sa transcription validée."""
        if settings.delete_chunks_after and chunk.file.exists():
            chunk.file.unlink()


def plan(audio: Path, work_dir: Path, duration: float | None = None) -> ChunkSet:
    """Construit (ou recharge) le plan de découpage d'une piste audio."""
    existing = ChunkSet.load(work_dir)
    if existing is not None:
        log.info("Plan existant repris : %d morceaux, %.0f%% déjà traités",
                 len(existing.chunks), existing.progress * 100)
        return existing

    total = duration if duration is not None else media.probe(audio).duration
    step = settings.chunk_duration
    overlap = settings.chunk_overlap

    chunks: list[Chunk] = []
    index, start = 0, 0.0
    while start < total:
        # Le recouvrement évite qu'un mot soit coupé à la frontière.
        length = min(step + overlap, total - start)
        chunks.append(
            Chunk(
                index=index,
                path=str(work_dir / f"chunk_{index:04d}.wav"),
                start=start,
                duration=length,
            )
        )
        index += 1
        start += step

    work_dir.mkdir(parents=True, exist_ok=True)
    chunk_set = ChunkSet(work_dir, chunks)
    chunk_set.save()
    log.info("Plan créé : %d morceaux de %d s (recouvrement %d s)",
             len(chunks), step, overlap)
    return chunk_set


def materialize(audio: Path, chunk: Chunk) -> Path:
    """Extrait physiquement un morceau. Le WAV permet un seek exact et instantané."""
    if chunk.file.exists():
        return chunk.file

    media.run([
        "-ss", f"{chunk.start:.3f}",
        "-t", f"{chunk.duration:.3f}",
        "-i", str(audio),
        "-c", "copy",
        str(chunk.file),
    ])
    return chunk.file
