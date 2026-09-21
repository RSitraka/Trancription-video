"""Configuration, modèle de données et workers Celery."""

import pytest
from conftest import add_job

from source import pipeline, tasks
from source.config import Settings
from source.db import Job, Part
from source.pipeline import Result


# --- Configuration -----------------------------------------------------------

def test_sqlalchemy_url_forces_psycopg():
    settings = Settings(database_url="postgresql://u:p@h:5432/db")
    assert settings.sqlalchemy_url == "postgresql+psycopg://u:p@h:5432/db"
    assert Settings(database_url="sqlite:///x.db").sqlalchemy_url == "sqlite:///x.db"


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("CRF", "30")
    monkeypatch.setenv("TARGET_SIZE_GB", "1.5")
    monkeypatch.setenv("ENABLE_OCR", "true")
    settings = Settings()
    assert (settings.crf, settings.target_size_gb, settings.enable_ocr) == (30, 1.5, True)


def test_ensure_dirs(tmp_path):
    settings = Settings(work_dir=tmp_path / "a" / "tmp", output_dir=tmp_path / "b" / "out")
    settings.ensure_dirs()
    assert settings.work_dir.is_dir() and settings.output_dir.is_dir()


# --- Base de données ---------------------------------------------------------

def test_job_defaults_and_as_dict(session):
    job = add_job(session, filename="cours.mp4", status="pending", size=600 * 1024**3,
                  progress=0.123456)
    data = job.as_dict()
    assert len(job.id) == 32
    assert data["size"] == 600 * 1024**3                        # au-delà de 2³¹
    assert data["progress"] == 0.123
    assert data["outputs"] == [] and data["created_at"]


def test_parts_are_deleted_with_job(session):
    job = add_job(session)
    session.add_all([Part(job_id=job.id, index=i, size=10) for i in range(3)])
    session.commit()
    session.delete(job)
    session.commit()
    assert session.query(Part).count() == 0


# --- Workers -----------------------------------------------------------------

@pytest.fixture
def no_backend(monkeypatch):
    states = []
    monkeypatch.setattr(tasks.process_job, "update_state",
                        lambda **kwargs: states.append(kwargs))
    return states


def run(job_id):
    return tasks.process_job.apply(args=[job_id])


def test_process_job_success(session, dirs, monkeypatch, no_backend):
    source = dirs.media / "cours.mp4"
    source.write_bytes(b"x")
    received = {}

    def runner(path, on_progress, max_mb=None, subdir=None):
        received.update(path=path, max_mb=max_mb, subdir=subdir)
        on_progress("compressing", 0.5)
        return Result(path, outputs=[dirs.out / "cours_compressed.mp4"], segments=3)

    monkeypatch.setitem(pipeline.RUNNERS, "compress", runner)
    job = add_job(session, status="queued", source_path=str(source),
                  options={"max_mb": 300, "subdir": "m", "inconnu": True})

    result = run(job.id)
    assert result.successful()
    assert result.result == {"job_id": job.id, "outputs": [str(dirs.out / "cours_compressed.mp4")],
                             "segments": 3}
    # Option inconnue du runner ignorée, les autres transmises.
    assert received == {"path": source, "max_mb": 300, "subdir": "m"}
    assert no_backend[0]["meta"] == {"stage": "compressing", "progress": 0.5}

    session.expire_all()
    stored = session.get(Job, job.id)
    assert (stored.status, stored.progress, stored.error) == ("done", 1.0, None)


def test_process_job_missing_source(session, dirs, no_backend):
    job = add_job(session, status="queued", source_path=str(dirs.media / "absent.mp4"))
    assert run(job.id).failed()
    session.expire_all()
    stored = session.get(Job, job.id)
    assert stored.status == "failed" and "fichier absent" in stored.error


def test_process_job_runner_error_is_stored(session, dirs, monkeypatch, no_backend):
    source = dirs.media / "cours.mp4"
    source.write_bytes(b"x")

    def boom(path, on_progress):
        raise RuntimeError("e" * 5000)

    monkeypatch.setitem(pipeline.RUNNERS, "transcribe", boom)
    job = add_job(session, status="queued", mode="transcribe", source_path=str(source))
    assert run(job.id).failed()
    session.expire_all()
    stored = session.get(Job, job.id)
    assert stored.status == "failed" and len(stored.error) == 4000


def test_process_job_unknown_job(no_backend):
    assert run("inexistant").failed()


def test_celery_is_configured_for_long_jobs():
    conf = tasks.celery_app.conf
    assert conf.task_acks_late and conf.worker_prefetch_multiplier == 1
    assert conf.broker_transport_options["visibility_timeout"] >= 7 * 86400


def test_process_job_stores_title_and_note(session, dirs, monkeypatch, no_backend):
    source = dirs.media / "VID_2026.mp4"
    source.write_bytes(b"x")
    monkeypatch.setitem(pipeline.RUNNERS, "process", lambda path, on_progress: Result(
        path, outputs=[], title="Réunion budget", note="aucune piste audio : compressée"))
    job = add_job(session, status="queued", mode="process", source_path=str(source))

    assert run(job.id).successful()
    session.expire_all()
    stored = session.get(Job, job.id)
    assert stored.title == "Réunion budget"
    assert stored.note.startswith("aucune piste audio")
