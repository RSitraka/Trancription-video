"""Workers Celery : exécution asynchrone des jobs, progression persistée."""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

from celery import Celery

from source import download, pipeline
from source.config import settings
from source.db import Job, SessionLocal, init_db

log = logging.getLogger(__name__)

celery_app = Celery("transcription", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_track_started=True,
    task_acks_late=True,               # un worker tué ne perd pas le job
    worker_prefetch_multiplier=1,      # un job lourd à la fois par worker
    task_time_limit=None,              # 1 To peut prendre des heures
    result_expires=86400,
    # Redis considère un message « perdu » passé ce délai et le redistribue.
    # La valeur par défaut (1 h) est plus courte qu'un job de transcription :
    # la tâche serait relancée en double alors qu'elle tourne encore.
    # 30 jours : un fichier de 2 To découpé en parties peut tourner plusieurs
    # jours, et une redistribution lancerait un second encodage en parallèle.
    broker_transport_options={"visibility_timeout": 30 * 86400},
)


def _update(job_id: str, **fields) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        session.commit()


@celery_app.task(bind=True, name="jobs.process")
def process_job(self, job_id: str) -> dict:
    """Exécute un job : transcription, compression, ou les deux."""
    init_db()

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise ValueError(f"job introuvable : {job_id}")
        source = Path(job.source_path or "")
        mode = job.mode
        options = dict(job.options or {})

    # Traitement lancé à partir d'un lien : la vidéo est d'abord téléchargée,
    # au même endroit qu'un fichier envoyé (supprimée avec le traitement).
    url = options.pop("url", None)
    if url and not job.source_path:
        _update(job_id, status="downloading", progress=0.0, error=None)
        try:
            source = download.download(
                url, settings.work_dir / "sources" / job_id,
                on_progress=lambda p: _update(job_id, status="downloading", progress=p))
        except Exception as error:                  # noqa: BLE001 - tracé en base
            log.warning("téléchargement du job %s en échec : %s", job_id, error)
            _update(job_id, status="failed", error=f"téléchargement impossible : {error}")
            raise
        _update(job_id, source_path=str(source), filename=source.name,
                size=source.stat().st_size)

    if not source.exists():
        _update(job_id, status="failed", error=f"fichier absent : {source}")
        raise FileNotFoundError(source)

    # Lu ici : le contexte Celery est propre au thread, et la compression
    # rapporte sa progression depuis plusieurs threads d'encodage.
    task_id = self.request.id

    def on_progress(stage: str, value: float) -> None:
        _update(job_id, status=stage, progress=value)
        self.update_state(task_id=task_id, state="PROGRESS",
                          meta={"stage": stage, "progress": value})

    runner = pipeline.RUNNERS[mode]
    accepted = set(inspect.signature(runner).parameters)
    kwargs = {k: v for k, v in options.items() if k in accepted}

    try:
        _update(job_id, status="running", progress=0.0, error=None)
        result = runner(source, on_progress=on_progress, **kwargs)
    except Exception as error:                      # noqa: BLE001 - tracé en base
        log.exception("job %s en échec", job_id)
        _update(job_id, status="failed", error=str(error)[:4000])
        raise

    outputs = [str(p) for p in result.outputs]
    # Le titre sert à retrouver les fichiers produits quand le nom du fichier
    # ne dit rien (source/titling.py).
    _update(job_id, status="done", progress=1.0, outputs=outputs, title=result.title,
            note=result.note or None)
    return {"job_id": job_id, "outputs": outputs, "segments": result.segments}
