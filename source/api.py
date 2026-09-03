"""API FastAPI : création de jobs, upload chunké reprenable, progression."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from source import __version__, media
from source.config import settings
from source.db import Job, Part, SessionLocal, init_db
from source.tasks import process_job

log = logging.getLogger(__name__)

app = FastAPI(
    title="Transcription Vidéo & Audio",
    version=__version__,
    description="Transcription et compression de fichiers volumineux (500 Go+).",
)


def uploads_dir(job_id: str) -> Path:
    return settings.work_dir / "uploads" / job_id


@app.on_event("startup")
def startup() -> None:
    settings.ensure_dirs()
    try:
        init_db()
    except Exception as error:            # la base peut démarrer après l'API
        log.warning("base indisponible au démarrage : %s", error)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.post("/jobs", status_code=201)
def create_job(payload: dict = Body(...)) -> dict:
    """Crée un job, soit pour un fichier déjà présent dans /media, soit pour un upload.

    - `{"path": "/media/cours.mp4"}`        → traitement immédiat
    - `{"filename": "...", "size": 1234}`   → upload chunké, puis /complete
    """
    init_db()
    mode = payload.get("mode", "transcribe")
    if mode not in ("transcribe", "compress", "process"):
        raise HTTPException(400, f"mode inconnu : {mode}")

    options = payload.get("options", {}) or {}
    path = payload.get("path")

    with SessionLocal() as session:
        if path:
            source = Path(path)
            if not source.exists():
                raise HTTPException(404, f"fichier introuvable : {source}")
            info = media.probe(source)
            job = Job(
                filename=source.name, source_path=str(source), size=info.size,
                status="queued", mode=mode, options=options,
            )
            session.add(job)
            session.commit()
            process_job.delay(job.id)
            return job.as_dict()

        filename = payload.get("filename")
        if not filename:
            raise HTTPException(400, "fournir soit 'path', soit 'filename' + 'size'")

        job = Job(
            filename=filename, size=int(payload.get("size", 0)),
            status="uploading", mode=mode, options=options,
        )
        session.add(job)
        session.commit()
        uploads_dir(job.id).mkdir(parents=True, exist_ok=True)
        return job.as_dict()


@app.put("/jobs/{job_id}/parts/{index}")
async def upload_part(job_id: str, index: int, request: Request) -> dict:
    """Reçoit un morceau d'upload en flux — jamais de fichier entier en mémoire."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job introuvable")

    target = uploads_dir(job_id) / f"part_{index:06d}"
    target.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    with target.open("wb") as handle:
        async for block in request.stream():
            handle.write(block)
            size += len(block)

    with SessionLocal() as session:
        existing = (
            session.query(Part).filter_by(job_id=job_id, index=index).one_or_none()
        )
        if existing is None:
            session.add(Part(job_id=job_id, index=index, size=size))
        else:
            existing.size = size
        session.commit()

    return {"index": index, "size": size}


@app.get("/jobs/{job_id}/parts")
def list_parts(job_id: str) -> dict:
    """Morceaux déjà reçus : permet au client de reprendre un transfert coupé."""
    with SessionLocal() as session:
        parts = session.query(Part).filter_by(job_id=job_id).order_by(Part.index).all()
        return {"received": [p.index for p in parts], "bytes": sum(p.size for p in parts)}


@app.post("/jobs/{job_id}/complete")
def complete_upload(job_id: str) -> dict:
    """Assemble les morceaux et met le job en file d'attente."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job introuvable")
        filename = job.filename

    parts = sorted(uploads_dir(job_id).glob("part_*"))
    if not parts:
        raise HTTPException(400, "aucun morceau reçu")

    destination = settings.work_dir / "sources" / job_id / filename
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Concaténation en flux : la mémoire reste constante quel que soit le volume.
    with destination.open("wb") as output:
        for part in parts:
            with part.open("rb") as handle:
                shutil.copyfileobj(handle, output, length=8 * 1024 * 1024)
            part.unlink()

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        job.source_path = str(destination)
        job.size = destination.stat().st_size
        job.status = "queued"
        session.commit()
        result = job.as_dict()

    process_job.delay(job_id)
    return result


@app.get("/jobs")
def list_jobs(limit: int = 50) -> list[dict]:
    with SessionLocal() as session:
        jobs = session.query(Job).order_by(Job.created_at.desc()).limit(limit).all()
        return [job.as_dict() for job in jobs]


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job introuvable")
        return job.as_dict()


@app.get("/jobs/{job_id}/result.{extension}")
def get_result(job_id: str, extension: str):
    """Télécharge un fichier produit (srt, vtt, json, txt, csv, mp4)."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job introuvable")
        outputs = job.outputs or []

    match = next((o for o in outputs if o.endswith(f".{extension}")), None)
    if match is None:
        raise HTTPException(
            404, f"aucune sortie .{extension} (statut : {job.status})"
        )
    return FileResponse(match, filename=Path(match).name)


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str) -> JSONResponse:
    """Supprime le job et ses fichiers temporaires."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job introuvable")
        session.delete(job)
        session.commit()

    for directory in (uploads_dir(job_id), settings.work_dir / "sources" / job_id):
        shutil.rmtree(directory, ignore_errors=True)
    return JSONResponse({"deleted": job_id})
