"""Fixtures communes.

Les tests sont autonomes : SQLite remplace PostgreSQL, Celery et Qdrant sont
simulés. Seul FFmpeg est réel, pour vérifier la compression et le découpage
sur de vrais fichiers générés à la volée (quelques secondes chacun).

Lancement : `make test` (ou `docker compose --profile test run --rm test`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Avant tout import de `source` : le moteur SQLAlchemy est créé à l'import.
os.environ.setdefault(
    "DATABASE_URL", f"sqlite:///{tempfile.gettempdir()}/transcription-tests.db")
os.environ.setdefault("WHISPER_MODEL", "tiny")

from source import api, auth, tasks  # noqa: E402
from source.config import settings  # noqa: E402
from source.db import Base, SessionLocal, engine  # noqa: E402

TOKEN = "jeton-de-test-0123456789"

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg absent")


def _create_schema() -> None:
    # init_db() ajoute des colonnes avec une syntaxe propre à PostgreSQL.
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def database(monkeypatch):
    """Base vide pour chaque test."""
    Base.metadata.drop_all(engine)
    _create_schema()
    monkeypatch.setattr(api, "init_db", _create_schema)
    monkeypatch.setattr(tasks, "init_db", _create_schema)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def session():
    with SessionLocal() as s:
        yield s


@pytest.fixture(autouse=True)
def dirs(tmp_path, monkeypatch):
    """Dossiers media / travail / sortie isolés dans le dossier temporaire du test."""
    paths = SimpleNamespace(
        media=tmp_path / "media", work=tmp_path / "work", out=tmp_path / "out",
        outside=tmp_path / "outside",
    )
    for path in vars(paths).values():
        path.mkdir()
    monkeypatch.setattr(settings, "media_dir", paths.media)
    monkeypatch.setattr(settings, "work_dir", paths.work)
    monkeypatch.setattr(settings, "output_dir", paths.out)
    return paths


@pytest.fixture(autouse=True)
def api_token(monkeypatch):
    """Mode jeton, avec un jeton connu : les tests d'API vérifient le cas le plus
    strict. Le mode local est testé à part (tests/test_security.py, SEC-14)."""
    monkeypatch.setattr(settings, "require_token", True)
    monkeypatch.setattr(settings, "allowed_hosts", "localhost,127.0.0.1,::1,testserver")
    monkeypatch.setattr(settings, "api_token", TOKEN)
    monkeypatch.setattr(auth, "_generated", None)
    return TOKEN


@pytest.fixture
def celery(monkeypatch):
    """Courtier simulé : enregistre les mises en file et les révocations."""
    calls = SimpleNamespace(queued=[], revoked=[])

    def delay(job_id):
        calls.queued.append(job_id)
        return SimpleNamespace(id=f"task-{job_id}")

    monkeypatch.setattr(api.process_job, "delay", delay)
    monkeypatch.setattr(api.celery_app.control, "revoke",
                        lambda task_id, **kwargs: calls.revoked.append(task_id))
    return calls


@pytest.fixture
def client(celery):
    """Client HTTP authentifié, sans les événements de démarrage (préchargement
    d'un modèle)."""
    from fastapi.testclient import TestClient

    return TestClient(api.app, headers={"Authorization": f"Bearer {TOKEN}"})


@pytest.fixture
def anonymous(celery):
    """Client sans jeton : ce que voit n'importe qui d'autre."""
    from fastapi.testclient import TestClient

    return TestClient(api.app)


# --- Médias générés ----------------------------------------------------------

def make_clip(path: Path, seconds: float = 4, video: bool = True, audio: bool = True,
              size: str = "320x240", rate: int = 25, gop: int | None = None,
              bitrate: str | None = None) -> Path:
    """Clip de test : mire animée et/ou tonalité, encodé en H.264/AAC."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if video:
        args += ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if video:
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        if gop:
            args += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
        if bitrate:
            args += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]
    if audio:
        args += ["-c:a", "aac", "-b:a", "64k"]
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([*args, str(path)], check=True)
    return path


@pytest.fixture(scope="session")
def clips(tmp_path_factory):
    """Clips partagés par toute la session (générés une seule fois)."""
    if not HAS_FFMPEG:
        pytest.skip("FFmpeg absent")
    root = tmp_path_factory.mktemp("clips")
    return SimpleNamespace(
        video=make_clip(root / "video.mp4", seconds=4),
        audio_only=make_clip(root / "audio.m4a", seconds=3, video=False),
        silent=make_clip(root / "silent.mp4", seconds=2, audio=False),
        # ~2 Mo, une image-clé par seconde : découpable sans ré-encodage.
        heavy=make_clip(root / "heavy.mp4", seconds=8, size="640x360", gop=25,
                        bitrate="2000k"),
    )


def add_job(session, **fields):
    """Crée un job en base avec des valeurs par défaut raisonnables."""
    from source.db import Job

    values = dict(filename="cours.mp4", status="done", mode="compress", options={},
                  outputs=[])
    values.update(fields)
    job = Job(**values)
    session.add(job)
    session.commit()
    return job
