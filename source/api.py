"""API FastAPI : création de jobs, upload chunké reprenable, progression."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from source import __version__, auth, media
from source.config import settings
from source.db import Job, Part, SessionLocal, init_db
from source.tasks import celery_app, process_job

log = logging.getLogger(__name__)

app = FastAPI(
    title="Transcription Vidéo & Audio",
    version=__version__,
    description="Transcription et compression de fichiers volumineux (500 Go+).",
    # Jeton exigé partout, sauf auth.PUBLIC_PATHS (voir source/auth.py).
    dependencies=[Depends(auth.require)],
)


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


MO = 1000**2
# Au-delà, un index de morceau ne correspond à aucun envoi réel (64 Mo × 100 000 = 6,4 To).
MAX_PARTS = 100_000


def uploads_dir(job_id: str) -> Path:
    return settings.work_dir / "uploads" / job_id


@app.on_event("startup")
def startup() -> None:
    settings.ensure_dirs()
    if settings.require_token:
        auth.token()                      # crée et journalise le jeton si besoin
    try:
        init_db()
    except Exception as error:            # la base peut démarrer après l'API
        log.warning("base indisponible au démarrage : %s", error)
    threading.Thread(target=_warm_up, daemon=True).start()


def _warm_up() -> None:
    """Charge le modèle d'embedding en fond : la première recherche reste rapide."""
    from source import rag

    try:
        rag._embedder()
        log.info("modèle d'embedding chargé")
    except Exception as error:            # noqa: BLE001 - la recherche le signalera
        log.warning("préchargement du modèle impossible : %s", error)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Interface web : dépôt de fichier, suivi des travaux, téléchargements."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Icône de l'onglet : les navigateurs la demandent à la racine, sans jeton."""
    return FileResponse(STATIC_DIR / "icon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api")
def api_root() -> dict:
    """Point d'entrée JSON : rappelle les routes disponibles."""
    return {
        "service": "Transcription Vidéo & Audio",
        "version": __version__,
        "docs": "/docs",
        "interface": "/",
        "routes": {
            "santé": "GET /health",
            "fichiers disponibles": "GET /media",
            "créer un job": "POST /jobs",
            "liste des jobs": "GET /jobs",
            "suivi d'un job": "GET /jobs/{id}",
            "résultat": "GET /jobs/{id}/result.{srt|vtt|json|txt|csv|mp4}",
        },
    }


@app.get("/health")
def health() -> dict:
    # Version déployée (make deploy), sinon celle du paquet.
    version = os.environ.get("APP_VERSION") or __version__
    return {"status": "ok", "version": version}


@app.post("/login")
def login(payload: dict = Body(...)) -> JSONResponse:
    """Échange le jeton contre un cookie de session pour le navigateur."""
    given = payload.get("token")
    if not isinstance(given, str) or not auth.check_token(given.strip()):
        raise HTTPException(401, "jeton invalide")
    response = JSONResponse({"status": "ok"})
    response.set_cookie(auth.COOKIE, auth.session_value(), max_age=auth.SESSION_SECONDS,
                        httponly=True, samesite="strict", path="/")
    return response


@app.post("/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


ACTIVE_STATUSES = ("queued", "running", "extracting", "transcribing", "ocr", "compressing")


def _source_states(session) -> tuple[set[str], set[str]]:
    """Sources déjà traitées avec succès, et sources en cours de traitement."""
    rows = session.query(Job.source_path, Job.status).filter(
        Job.source_path.isnot(None),
        Job.status.in_(("done", *ACTIVE_STATUSES))).all()
    done = {path for path, status in rows if status == "done"}
    active = {path for path, status in rows if status != "done"}
    return done, active


@app.get("/media")
def list_media() -> list[dict]:
    """Fichiers et dossiers de MEDIA_PATH, traités sur place sans upload.

    Un dossier est résumé (nombre de fichiers média, taille totale) : il peut
    contenir des milliers de vidéos. `done` et `active` comptent les fichiers
    déjà traités et en cours : l'interface n'affiche que ce qui reste à faire.
    """
    if not settings.media_dir.exists():
        return []
    with SessionLocal() as session:
        done_paths, active_paths = _source_states(session)
    entries = []
    for path in sorted(settings.media_dir.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_dir():
            files = list(media.iter_media(path))
            if not files:
                continue
            entries.append({
                "path": str(path), "name": path.name, "kind": "folder",
                "count": len(files),
                "size": sum(f.stat().st_size for f in files),
                "largest": max(f.stat().st_size for f in files),
                "done": sum(str(f) in done_paths for f in files),
                "active": sum(str(f) in active_paths for f in files),
            })
        elif path.is_file() and path.suffix.lower() in media.MEDIA_EXTENSIONS:
            size = path.stat().st_size
            entries.append({
                "path": str(path), "name": path.name, "kind": "file",
                "count": 1, "size": size, "largest": size,
                "done": int(str(path) in done_paths),
                "active": int(str(path) in active_paths),
            })
    return entries


def _allowed(path: Path) -> Path:
    """N'accepte que des chemins sous MEDIA_PATH ou le dossier de travail."""
    resolved = path.resolve()
    roots = (settings.media_dir.resolve(), settings.work_dir.resolve())
    if not any(resolved.is_relative_to(root) for root in roots):
        raise HTTPException(403, f"chemin hors des dossiers autorisés : {path}")
    if not resolved.exists():
        raise HTTPException(404, f"fichier introuvable : {path}")
    return resolved


def _upload_name(filename) -> str:
    """Nom d'un fichier envoyé : un simple nom, jamais un chemin.

    Sans ce contrôle, « ../../x » ou « /app/source/tasks.py » ferait écrire
    l'assemblage n'importe où — y compris par-dessus le code (VULN-01)."""
    if (not isinstance(filename, str) or not filename.strip()
            or filename in (".", "..") or len(filename.encode()) > 255
            or any(c in filename for c in "/\\\x00")):
        raise HTTPException(400, "nom de fichier invalide : un nom simple est attendu, sans chemin")
    return filename


def _declared_size(size) -> int:
    """Taille annoncée d'un upload : bornée par l'espace disque réellement libre.

    Les morceaux puis l'assemblage (un morceau de plus au pire) doivent tenir en
    laissant une marge : un disque plein ferait échouer tous les traitements."""
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise HTTPException(400, "'size' doit être un nombre d'octets positif")
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(settings.work_dir).free
    needed = size + (settings.upload_part_max_mb + settings.upload_free_margin_mb) * MO
    if needed > free:
        raise HTTPException(507, f"espace disque insuffisant : {size / MO:.0f} Mo annoncés, "
                                 f"{max(free - needed + size, 0) / MO:.0f} Mo utilisables")
    return size


def _folder_key(filename: str, subdir: str | None) -> tuple[str, str]:
    """Dossier de sortie d'un job : deux jobs de même clé écrivent dans les mêmes fichiers."""
    return Path(filename).stem, subdir or ""


def _busy_folders(session, exclude: str | None = None) -> set[tuple[str, str]]:
    """Dossiers de sortie d'un traitement en file ou en cours."""
    jobs = session.query(Job).filter(Job.status.notin_(("done", "failed", "uploading")))
    return {_folder_key(j.filename, (j.options or {}).get("subdir"))
            for j in jobs if j.id != exclude}


def _kept_files(session, removed: set[str]) -> set[Path]:
    """Fichiers qu'un job restant utilise encore, ou dossiers qu'il est en train
    de remplir : les supprimer viderait le résultat d'un autre traitement de
    la même vidéo."""
    kept: set[Path] = set()
    for job in session.query(Job).filter(Job.id.notin_(removed)):
        kept |= {Path(o).resolve() for o in job.outputs or []}
        if job.status not in ("done", "failed", "uploading"):
            kept |= set(_produced_files(job))
    return kept


def _queue(session, source: Path, mode: str, options: dict) -> Job:
    job = Job(
        filename=source.name, source_path=str(source), size=source.stat().st_size,
        status="queued", mode=mode, options=options,
    )
    session.add(job)
    session.commit()
    job.task_id = process_job.delay(job.id).id
    session.commit()
    return job


@app.post("/jobs", status_code=201)
def create_job(payload: dict = Body(...)) -> dict:
    """Crée un job, soit pour un fichier déjà présent dans /media, soit pour un upload.

    - `{"path": "/media/cours.mp4"}`        → traitement immédiat
    - `{"path": "/media/archives"}`         → un job par fichier média du dossier
    - `{"filename": "...", "size": 1234}`   → upload chunké, puis /complete
    """
    init_db()
    mode = payload.get("mode", "transcribe")
    if mode not in ("transcribe", "compress", "process"):
        raise HTTPException(400, f"mode inconnu : {mode}")

    options = payload.get("options", {}) or {}
    options.pop("subdir", None)                 # fixé par le serveur uniquement
    path = payload.get("path")

    with SessionLocal() as session:
        if path:
            source = _allowed(Path(path))
            if source.is_dir():
                files = list(media.iter_media(source))
                if not files:
                    raise HTTPException(404, f"aucun fichier audio/vidéo dans {source}")
                # Les sorties reprennent l'arborescence : deux « intro.mp4 » de
                # sous-dossiers différents ne s'écrasent pas.
                root = source.parent
                busy = _busy_folders(session)
                # « skip_done » : les fichiers déjà traités ne sont pas refaits.
                done_paths = _source_states(session)[0] if payload.get("skip_done") else set()
                jobs, skipped, already = [], 0, 0
                for f in files:
                    if str(f) in done_paths:
                        already += 1
                        continue
                    subdir = str(f.parent.relative_to(root))
                    # Déjà en cours : un second job mélangerait ses parties
                    # avec celles du premier dans le même dossier.
                    if _folder_key(f.name, subdir) in busy:
                        skipped += 1
                        continue
                    jobs.append(_queue(session, f, mode, {**options, "subdir": subdir}))
                return {"count": len(jobs), "skipped": skipped, "already_done": already,
                        "jobs": [j.as_dict() for j in jobs]}

            if _folder_key(source.name, None) in _busy_folders(session):
                raise HTTPException(409, f"« {source.name} » est déjà en cours de traitement")
            media.probe(source)                 # refuse tout de suite un fichier illisible
            return _queue(session, source, mode, options).as_dict()

        filename = payload.get("filename")
        if not filename:
            raise HTTPException(400, "fournir soit 'path', soit 'filename' + 'size'")
        filename = _upload_name(filename)
        size = _declared_size(payload.get("size"))

        job = Job(
            filename=filename, size=size,
            status="uploading", mode=mode, options=options,
        )
        session.add(job)
        session.commit()
        uploads_dir(job.id).mkdir(parents=True, exist_ok=True)
        return job.as_dict()


@app.put("/jobs/{job_id}/parts/{index}")
async def upload_part(job_id: str, index: int, request: Request) -> dict:
    """Reçoit un morceau d'upload en flux — jamais de fichier entier en mémoire.

    Le total reçu ne peut pas dépasser la taille annoncée à la création du job,
    elle-même bornée par l'espace libre (VULN-02)."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job introuvable")
        if job.status != "uploading":
            raise HTTPException(409, f"upload déjà terminé (statut : {job.status})")
        declared = job.size
        others = sum(p.size for p in job.parts if p.index != index)

    # Chaque morceau fait au moins un octet : jamais plus de morceaux que d'octets.
    if not 0 <= index < min(MAX_PARTS, declared):
        raise HTTPException(400, f"numéro de morceau invalide : {index}")
    allowed = min(settings.upload_part_max_mb * MO, declared - others)
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > allowed:
        raise HTTPException(413, f"morceau trop gros : {int(length)} octets, {max(allowed, 0)} permis")

    target = uploads_dir(job_id) / f"part_{index:06d}"
    target.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    # Nom temporaire : un morceau refusé en cours de route n'écrase pas un
    # morceau valide déjà reçu sous le même numéro.
    pending = target.with_name(target.name + ".encours")
    try:
        with pending.open("wb") as handle:
            async for block in request.stream():
                size += len(block)
                if size > allowed:        # en-tête absent ou mensonger
                    raise HTTPException(413, f"morceau trop gros : plus de {max(allowed, 0)} octets")
                handle.write(block)
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)

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
        if _folder_key(filename, (job.options or {}).get("subdir")) in _busy_folders(session, job_id):
            raise HTTPException(409, f"« {filename} » est déjà en cours de traitement")

    parts = sorted(p for p in uploads_dir(job_id).glob("part_*") if p.suffix != ".encours")
    if not parts:
        raise HTTPException(400, "aucun morceau reçu")
    received = sum(p.stat().st_size for p in parts)
    if received != job.size:
        raise HTTPException(400, f"envoi incomplet : {received} octets reçus sur {job.size}")

    # Revérifié ici : un job créé avant ce contrôle peut encore être en base.
    folder = (settings.work_dir / "sources" / job_id).resolve()
    destination = (folder / _upload_name(filename)).resolve()
    if destination.parent != folder:
        raise HTTPException(400, "nom de fichier invalide")
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
        job.task_id = process_job.delay(job_id).id
        session.commit()
        result = job.as_dict()

    return result


@app.get("/jobs")
def list_jobs(limit: int = Query(50, ge=1, le=10_000)) -> list[dict]:
    with SessionLocal() as session:
        jobs = session.query(Job).order_by(Job.created_at.desc()).limit(limit).all()
        return [_with_sizes(job) for job in jobs]


def _with_sizes(job: Job) -> dict:
    """Ajoute la taille de chaque sortie (ce que l'on vérifie avant dépôt) et le
    sous-dossier d'origine (deux « cours_1.mp4 » de modules différents)."""
    data = job.as_dict()
    data["folder"] = (job.options or {}).get("subdir")
    data["language"] = (job.options or {}).get("language")
    data["output_sizes"] = [
        Path(o).stat().st_size if Path(o).is_file() else None for o in data["outputs"]
    ]
    return data


@app.delete("/jobs")
def clear_jobs(status: str = Query(pattern="^(done|failed)$"), files: bool = False) -> dict:
    """Retire de la liste les jobs terminés ou en échec.

    `files=true` supprime aussi leurs fichiers produits ; sinon ils restent dans
    le dossier de sortie."""
    produced: list[Path] = []
    with SessionLocal() as session:
        jobs = session.query(Job).filter_by(status=status).all()
        kept = _kept_files(session, {j.id for j in jobs}) if files else set()
        for job in jobs:
            if files:
                produced.extend(p for p in _produced_files(job) if p.resolve() not in kept)
            shutil.rmtree(uploads_dir(job.id), ignore_errors=True)
            shutil.rmtree(settings.work_dir / "sources" / job.id, ignore_errors=True)
            session.delete(job)
        session.commit()
    count = sum(1 for p in produced if p.is_file())
    _remove(produced)
    return {"deleted": len(jobs), "files_deleted": count}


@app.get("/settings")
def public_settings() -> dict:
    """Dossiers de l'hôte, pour que l'interface dise où lire et où trouver les résultats."""
    return {
        "media_path": os.environ.get("MEDIA_PATH_HOST", "./media"),
        "output_path": os.environ.get("OUTPUT_PATH_HOST", "./output"),
    }


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job introuvable")
        return job.as_dict()


@app.get("/jobs/{job_id}/files/{index}")
def get_output(job_id: str, index: int) -> FileResponse:
    """Télécharge la n-ième sortie : indispensable quand une vidéo est découpée
    en plusieurs parties de même extension."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job introuvable")
        outputs = job.outputs or []
    if not 0 <= index < len(outputs) or not Path(outputs[index]).is_file():
        raise HTTPException(404, "sortie introuvable")
    return FileResponse(outputs[index], filename=Path(outputs[index]).name)


class _ZipStream:
    """Tampon en écriture seule : zipfile y écrit, le générateur le vide."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.position = 0

    def write(self, data: bytes) -> int:
        self.chunks.append(bytes(data))
        self.position += len(data)
        return len(data)

    def tell(self) -> int:
        return self.position

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data, self.chunks = b"".join(self.chunks), []
        return data


def _windows_name(title: str, limit: int = 40) -> str:
    """Titre utilisable sous Windows : sans caractère interdit (« : ? * … »), et
    court. Windows refuse d'extraire un chemin de plus de 260 caractères, et le
    titre y figure trois fois (zip, dossier, fichier)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > limit:
        cut = name[:limit + 1]
        name = cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else name[:limit]
    return name.rstrip(" .-_(") or "video"      # Windows refuse aussi un point final


def _zip_response(title: str, files: list[Path]) -> StreamingResponse:
    """Archive .zip contenant un dossier au nom de la vidéo, produite à la volée :
    ni fichier temporaire ni mémoire proportionnelle à la taille (vidéos déjà
    compressées, donc stockées sans recompression)."""
    files = [f for f in files if f.is_file()]
    if not files:
        raise HTTPException(404, "aucun fichier à télécharger")
    short = _windows_name(title)

    def entry(path: Path) -> str:
        # « <titre long>_compressed_2.mp4 » → « <titre court>_compressed_2.mp4 »
        name = short + path.name[len(title):] if path.name.startswith(title) else path.name
        return f"{short}/{_windows_name(Path(name).stem, 80)}{''.join(Path(name).suffixes[-1:])}"

    def generate():
        stream = _ZipStream()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
            for path in sorted(files, key=lambda f: [int(t) if t.isdigit() else t
                                                     for t in re.split(r"(\d+)", f.name)]):
                info = zipfile.ZipInfo.from_file(path, entry(path))
                with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                    while block := source.read(8 << 20):
                        target.write(block)
                        yield stream.take()
        yield stream.take()

    name = f"{short}.zip"
    return StreamingResponse(generate(), media_type="application/zip", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.get("/jobs/{job_id}/archive.zip")
def get_archive(job_id: str) -> StreamingResponse:
    """Toutes les sorties du job dans un .zip, rangées dans un dossier au nom de la vidéo."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job introuvable")
        outputs, filename, title = list(job.outputs or []), job.filename, job.title
    root = settings.output_dir.resolve()
    files = [Path(o) for o in outputs if Path(o).resolve().is_relative_to(root)]
    return _zip_response(title or Path(filename).stem, files)


@app.get("/jobs/{job_id}/result.{extension}")
def get_result(job_id: str, extension: str):
    """Télécharge un fichier produit (srt, vtt, json, txt, csv, jsonl, mp4)."""
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


# --- RAG ---------------------------------------------------------------------
# L'indexation tourne dans l'API et non dans un worker Celery : elle est courte,
# purement CPU, et n'entre donc pas en concurrence avec le GPU des transcriptions.

def _index_outputs(job_id: str, outputs: list[str]) -> None:
    from source import export, rag
    from source.transcribe import Segment

    # Tout est sous le try : sinon une erreur laisse « indexation en cours… » à vie.
    try:
        jsonl = next((Path(o) for o in outputs if o.endswith(".rag.jsonl")), None)
        if jsonl is None:
            # Pas d'export RAG demandé à l'origine : on le fabrique depuis le JSON.
            source = next((Path(o) for o in outputs if o.endswith(".json")), None)
            if source is None:
                _set_index_state(job_id, "aucune transcription à indexer")
                return
            payload = json.loads(source.read_text(encoding="utf-8"))
            segments = [Segment(**item) for item in payload["segments"]]
            jsonl = export.write(segments, source.with_suffix(""), ["rag"])[0]

        count = rag.index(jsonl)
        _set_index_state(job_id, f"{count} passages indexés")
    except Exception as error:                      # noqa: BLE001 - tracé en base
        log.exception("indexation du job %s en échec", job_id)
        _set_index_state(job_id, f"échec : {error}")


def _set_index_state(job_id: str, message: str) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is not None:
            job.indexed = message
            session.commit()


@app.post("/jobs/{job_id}/index")
def index_job(job_id: str, background: BackgroundTasks) -> dict:
    """Indexe la transcription d'un job dans Qdrant, en tâche de fond."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job introuvable")
        if job.status != "done":
            raise HTTPException(409, f"job non terminé (statut : {job.status})")
        outputs = list(job.outputs or [])
        job.indexed = "indexation en cours…"
        session.commit()

    background.add_task(_index_outputs, job_id, outputs)
    return {"job_id": job_id, "indexed": "indexation en cours…"}


@app.get("/search")
def search(
    q: str = Query(min_length=2),
    limit: int = Query(5, ge=1, le=50),
    source: str | None = None,
    kind: str | None = Query(None, pattern="^(speech|screen)$"),
    min_score: float | None = Query(None, ge=0, le=1),
) -> list[dict]:
    """Recherche sémantique, avec filtres : `source` (une vidéo), `kind`
    (« speech » : ce qui a été dit, « screen » : texte à l'écran) et
    `min_score` (pertinence minimale)."""
    from source import rag

    try:
        return rag.search(q, limit, source=source, kind=kind, min_score=min_score)
    except Exception as error:                      # noqa: BLE001
        raise HTTPException(503, f"recherche indisponible : {error}") from error


@app.get("/search/ready")
def search_ready() -> dict:
    """Le moteur de recherche est-il prêt ? Au premier démarrage, son modèle
    (~2 Go) se télécharge : l'interface le signale au lieu d'attendre en silence."""
    from source import rag

    return {"ready": rag.ready()}


@app.get("/frames/{name:path}")
def get_frame(name: str) -> FileResponse:
    """Sert une capture d'écran extraite d'une vidéo.

    Le nom est résolu sous le dossier de sortie uniquement : un chemin remontant
    ailleurs sur le disque est refusé.
    """
    target = (settings.output_dir / name).resolve()
    root = settings.output_dir.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "capture introuvable")
    return FileResponse(target)


@app.get("/sources")
def list_sources() -> list[str]:
    """Vidéos actuellement indexées dans Qdrant."""
    from source import rag

    try:
        return rag.sources()
    except Exception as error:                      # noqa: BLE001
        raise HTTPException(503, f"Qdrant indisponible : {error}") from error


def _outputs_of(filename: str, subdir: str | None, known: list[str] | None = None,
                title: str | None = None) -> list[Path]:
    """Fichiers produits pour une source (vidéos compressées et sous-titres),
    terminés ou non, limités au dossier de sortie.

    En cours de traitement, `outputs` est encore vide : les parties déjà écrites
    (`cours_compressed_3.mp4`, `….encours.mp4`) sont retrouvées par leur nom."""
    root = settings.output_dir.resolve()
    base = title or Path(filename).stem
    files = {Path(o).resolve() for o in known or []}
    stem = re.escape(base + "_compressed")
    # Fichiers finaux ou en cours d'écriture, et dossiers de travail cachés
    # (`.cours_compressed.decoupe`, `.cours_compressed_1.encours.morceaux`).
    output = re.compile(rf"{stem}(_\d+)?(\.encours)?\.(mp4|m4a)"
                        rf"|{re.escape(base)}\.(srt|vtt|json|txt|csv|rag\.jsonl)")
    work = re.compile(rf"\.{stem}(_\d+)?(\.encours)?\.(decoupe|morceaux)")
    # Dossier au nom de la vidéo (`out/cours/`), et l'ancien emplacement à
    # plat (`out/`) pour les fichiers produits avant ce rangement.
    parent = root / (subdir or "")
    for folder in (parent / base, parent):
        folder = folder.resolve()
        if folder.is_dir() and folder.is_relative_to(root):
            files |= {f for f in folder.iterdir()
                      if output.fullmatch(f.name) or (f.is_dir() and work.fullmatch(f.name))}
    return [f for f in files if f.is_relative_to(root)]


def _produced_files(job: Job) -> list[Path]:
    return _outputs_of(job.filename, (job.options or {}).get("subdir"), job.outputs,
                       title=job.title)


def _remove(paths: list[Path]) -> None:
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    # Le dossier au nom de la vidéo ne sert plus s'il est vide.
    root = settings.output_dir.resolve()
    for folder in {p.parent for p in paths}:
        if folder != root and folder.is_relative_to(root):
            try:
                folder.rmdir()
            except OSError:
                pass                                # pas vide ou déjà supprimé


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str, files: bool = False) -> JSONResponse:
    """Retire le job et arrête son traitement.

    - `files=false` (défaut) : les fichiers produits restent ; relancer le même
      fichier reprend là où il s'était arrêté (parties déjà faites conservées).
    - `files=true` : les fichiers produits sont aussi supprimés du disque."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job introuvable")
        task_id = job.task_id
        produced = []
        if files:
            kept = _kept_files(session, {job_id})
            produced = [p for p in _produced_files(job) if p.resolve() not in kept]
        session.delete(job)
        session.commit()

    # Sans cela, le worker continuerait à traiter un job qui n'existe plus,
    # bloquant la file pendant des heures.
    if task_id:
        celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
        log.info("tâche %s interrompue avec le job %s", task_id, job_id)

    for directory in (uploads_dir(job_id), settings.work_dir / "sources" / job_id):
        shutil.rmtree(directory, ignore_errors=True)
    _remove(produced)
    if produced:
        log.info("job %s : %d fichier(s) produit(s) supprimé(s)", job_id, len(produced))
    return JSONResponse({"deleted": job_id, "files_deleted": len(produced)})


# Nom de base d'un fichier produit : « cours » pour cours_compressed_3.mp4 ou cours.srt.
OUTPUT_NAME = re.compile(
    r"(?P<base>.+?)(?:_compressed(?:_\d+)?(?:\.encours)?\.(?:mp4|m4a)"
    r"|\.(?:srt|vtt|json|txt|csv|rag\.jsonl))"
)


def _output_groups() -> list[dict]:
    """Fichiers du dossier de sortie regroupés par vidéo d'origine, qu'un
    traitement existe encore ou non."""
    root = settings.output_dir.resolve()
    groups: dict[tuple[str, str], dict] = {}
    if not root.is_dir():
        return []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or any(part.startswith(".") for part in relative.parts):
            continue
        match = OUTPUT_NAME.fullmatch(path.name)
        if not match:
            continue
        parent = relative.parent
        if parent.name == match["base"]:
            parent = parent.parent                  # dossier au nom de la vidéo
        subdir = str(parent) if parent != Path(".") else ""
        group = groups.setdefault((subdir, match["base"]), {
            "subdir": subdir, "base": match["base"], "files": [], "size": 0})
        size = path.stat().st_size
        group["files"].append({"name": path.name, "size": size, "path": str(path)})
        group["size"] += size
    return list(groups.values())


def _delete_output_group(subdir: str, base: str) -> int:
    produced = _outputs_of(f"{base}.mp4", subdir or None)
    count = sum(1 for p in produced if p.is_file())
    _remove(produced)
    # Un traitement dont les fichiers n'existent plus n'a plus rien à proposer.
    gone = {str(p) for p in produced}
    with SessionLocal() as session:
        for job in session.query(Job).filter(Job.status.in_(("done", "failed"))).all():
            if job.outputs and all(o in gone or not Path(o).exists() for o in job.outputs):
                session.delete(job)
        session.commit()
    return count


@app.get("/outputs")
def list_outputs() -> list[dict]:
    """Fichiers produits (compressions, sous-titres), groupés par vidéo."""
    return _output_groups()


@app.get("/outputs/archive.zip")
def get_outputs_archive(base: str, subdir: str = "") -> StreamingResponse:
    """Fichiers produits d'une vidéo dans un .zip, dans un dossier à son nom."""
    files = [p for p in _outputs_of(f"{base}.mp4", subdir or None)
             if p.is_file() and not p.name.startswith(".") and ".encours." not in p.name]
    return _zip_response(base, files)


@app.delete("/outputs")
def delete_outputs(base: str | None = None, subdir: str = "", all: bool = False) -> dict:
    """Supprime les fichiers produits d'une vidéo (`base`, `subdir`), ou tous
    (`all=true`). Les vidéos d'origine ne sont pas touchées."""
    if all:
        targets = [(g["subdir"], g["base"]) for g in _output_groups()]
    elif base:
        targets = [(subdir, base)]
    else:
        raise HTTPException(400, "fournir 'base' ou 'all=true'")
    count = sum(_delete_output_group(d, b) for d, b in targets)
    log.info("fichiers produits supprimés : %d fichier(s), %d vidéo(s)", count, len(targets))
    return {"files_deleted": count, "groups_deleted": len(targets)}


@app.delete("/media")
def delete_media(path: str = Query(...)) -> dict:
    """Supprime définitivement une vidéo (ou un dossier) de MEDIA_PATH, avec tout
    ce qui en a été tiré : compressions, sous-titres, travaux et fichiers de
    travail. Irréversible."""
    media_root = settings.media_dir.resolve()
    target = Path(path).resolve()
    if target == media_root or not target.is_relative_to(media_root):
        raise HTTPException(403, f"chemin hors de MEDIA_PATH : {path}")
    if not target.exists():
        raise HTTPException(404, f"fichier introuvable : {path}")
    sources = list(media.iter_media(target)) if target.is_dir() else [target]

    removed = 0
    titles: dict[str, set[str]] = {}
    with SessionLocal() as session:
        jobs = session.query(Job).filter(Job.source_path.in_([str(f) for f in sources])).all()
        for job in jobs:
            # Titre tiré du contenu : sans lui, les fichiers produits d'une vidéo
            # au nom peu parlant resteraient sur le disque.
            if job.title:
                titles.setdefault(job.source_path, set()).add(job.title)
            if job.task_id and job.status not in ("done", "failed"):
                celery_app.control.revoke(job.task_id, terminate=True, signal="SIGTERM")
            session.delete(job)
        session.commit()

    for source in sources:
        # Les sorties d'un fichier de media/A/B/x.mp4 sont dans out/A/B/.
        subdir = str(source.parent.relative_to(media_root))
        subdir = None if subdir == "." else subdir
        produced = _outputs_of(source.name, subdir)
        for title in titles.get(str(source), ()):
            produced += _outputs_of(source.name, subdir, title=title)
        produced.append(settings.work_dir / (subdir or "") / source.stem)   # morceaux audio
        removed += sum(1 for p in produced if p.exists() and not p.name.startswith(".")
                       and p.is_file())
        _remove([p for p in produced if p.exists()])

    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as error:
        raise HTTPException(
            500, f"compressions supprimées, mais pas la source : {error} "
                 "(MEDIA_PATH monté en lecture seule ?)") from error

    log.info("source %s supprimée : %d fichier(s) produit(s), %d travail(aux)",
             target, removed, len(jobs))
    return {"deleted": str(target), "files_deleted": removed, "jobs_deleted": len(jobs)}
