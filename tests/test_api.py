"""API FastAPI : routes, upload chunké, téléchargements, suppression."""

import io
import zipfile

import pytest
from conftest import add_job

from source import api, media
from source.db import Job, Part


@pytest.fixture
def probe_ok(monkeypatch):
    monkeypatch.setattr(api.media, "probe", lambda path: None)


def write(path, data=b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --- Routes simples ----------------------------------------------------------

def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_index_and_api_root(client):
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert client.get("/api").json()["docs"] == "/docs"


def test_icons(client):
    from PIL import Image

    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200 and favicon.headers["content-type"] == "image/x-icon"
    assert {(16, 16), (32, 32), (256, 256)} <= Image.open(io.BytesIO(favicon.content)).info["sizes"]
    for name, size in [("icon-32.png", 32), ("icon-180.png", 180), ("icon-512.png", 512)]:
        png = client.get(f"/static/{name}")
        assert png.status_code == 200 and Image.open(io.BytesIO(png.content)).size == (size, size)
    page = client.get("/").text
    assert 'rel="icon" href="/favicon.ico"' in page and 'href="/static/icon-32.png"' in page


def test_settings_show_host_paths(client, monkeypatch):
    monkeypatch.setenv("MEDIA_PATH_HOST", "/mnt/e/videos")
    assert client.get("/settings").json()["media_path"] == "/mnt/e/videos"


def test_list_media(client, dirs):
    write(dirs.media / "b.mp4", b"12345")
    write(dirs.media / "notes.txt")
    write(dirs.media / ".cache.mp4")
    write(dirs.media / "module" / "x.mkv", b"1")
    write(dirs.media / "module" / "y.mp3", b"123")
    (dirs.media / "vide").mkdir()
    assert client.get("/media").json() == [
        {"path": str(dirs.media / "b.mp4"), "name": "b.mp4", "kind": "file",
         "count": 1, "size": 5, "largest": 5, "done": 0, "active": 0},
        {"path": str(dirs.media / "module"), "name": "module", "kind": "folder",
         "count": 2, "size": 4, "largest": 3, "done": 0, "active": 0},
    ]


def test_list_media_without_folder(client, dirs, monkeypatch):
    monkeypatch.setattr(api.settings, "media_dir", dirs.media / "absent")
    assert client.get("/media").json() == []


# --- Création de jobs --------------------------------------------------------

def test_create_job_for_local_file(client, dirs, celery, probe_ok):
    source = write(dirs.media / "cours.mp4")
    response = client.post("/jobs", json={
        "path": str(source), "mode": "process",
        "options": {"max_mb": 300, "language": "fr", "subdir": "../../pirate"}})
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "queued" and job["size"] == 4
    assert celery.queued == [job["id"]]

    stored = client.get("/jobs").json()[0]
    assert stored["language"] == "fr"
    assert stored["folder"] is None                             # subdir client ignoré


@pytest.mark.parametrize("payload, status", [
    ({"path": "/x", "mode": "rm -rf"}, 400),
    ({"mode": "compress"}, 400),
])
def test_create_job_invalid(client, payload, status):
    assert client.post("/jobs", json=payload).status_code == status


def test_create_job_missing_file(client, dirs):
    assert client.post("/jobs", json={"path": str(dirs.media / "absent.mp4")}).status_code == 404


@pytest.mark.ffmpeg
def test_create_job_rejects_unreadable_media(client, dirs):
    source = write(dirs.media / "faux.mp4", b"pas une video" * 50)
    with pytest.raises(media.FFmpegError):
        client.post("/jobs", json={"path": str(source)})


def test_create_job_conflict_when_busy(client, dirs, probe_ok):
    source = write(dirs.media / "cours.mp4")
    assert client.post("/jobs", json={"path": str(source)}).status_code == 201
    assert client.post("/jobs", json={"path": str(source)}).status_code == 409


def test_create_jobs_for_folder(client, dirs, celery, session):
    write(dirs.media / "formation" / "intro.mp4")
    write(dirs.media / "formation" / "module1" / "intro.mp4")
    write(dirs.media / "formation" / "module2" / "cours.mkv")
    add_job(session, filename="cours.mkv", status="compressing",
            options={"subdir": "formation/module2"})

    data = client.post("/jobs", json={"path": str(dirs.media / "formation"),
                                      "mode": "compress"}).json()
    assert (data["count"], data["skipped"]) == (2, 1)
    folders = sorted(j["folder"] for j in client.get("/jobs").json() if j["status"] == "queued")
    assert folders == ["formation", "formation/module1"]


def test_create_jobs_for_empty_folder(client, dirs):
    write(dirs.media / "docs" / "notes.txt")
    assert client.post("/jobs", json={"path": str(dirs.media / "docs")}).status_code == 404


# --- Upload chunké -----------------------------------------------------------

def test_chunked_upload(client, dirs, celery):
    job = client.post("/jobs", json={"filename": "envoi.mp4", "size": 9,
                                      "mode": "compress"}).json()
    assert job["status"] == "uploading"

    # Morceaux dans le désordre, un renvoyé deux fois.
    for index, data in [(2, b"789"), (0, b"xxx"), (1, b"456"), (0, b"123")]:
        assert client.put(f"/jobs/{job['id']}/parts/{index}", content=data).json() == {
            "index": index, "size": 3}
    assert client.get(f"/jobs/{job['id']}/parts").json() == {"received": [0, 1, 2], "bytes": 9}

    done = client.post(f"/jobs/{job['id']}/complete").json()
    assert done["status"] == "queued" and done["size"] == 9
    assembled = dirs.work / "sources" / job["id"] / "envoi.mp4"
    assert assembled.read_bytes() == b"123456789"
    assert not list((dirs.work / "uploads" / job["id"]).iterdir())
    assert celery.queued == [job["id"]]


def test_upload_unknown_job(client):
    assert client.put("/jobs/nope/parts/0", content=b"x").status_code == 404
    assert client.post("/jobs/nope/complete").status_code == 404


def test_complete_without_parts(client):
    job = client.post("/jobs", json={"filename": "a.mp4", "size": 1}).json()
    assert client.post(f"/jobs/{job['id']}/complete").status_code == 400


# --- Consultation et téléchargements -----------------------------------------

def test_get_job(client, session):
    job = add_job(session, status="transcribing", progress=0.42)
    assert client.get(f"/jobs/{job.id}").json()["progress"] == 0.42
    assert client.get("/jobs/absent").status_code == 404


def test_list_jobs_limit_and_sizes(client, session, dirs):
    output = write(dirs.out / "cours" / "cours.srt", b"12")
    for _ in range(3):
        add_job(session, outputs=[str(output), str(dirs.out / "disparu.mp4")])
    jobs = client.get("/jobs", params={"limit": 2}).json()
    assert len(jobs) == 2 and jobs[0]["output_sizes"] == [2, None]


def test_download_outputs(client, session, dirs):
    srt = write(dirs.out / "cours" / "cours.srt", b"1\n00:00")
    part = write(dirs.out / "cours" / "cours_compressed_2.mp4", b"video")
    job = add_job(session, outputs=[str(srt), str(part)])

    assert client.get(f"/jobs/{job.id}/result.srt").content == b"1\n00:00"
    assert client.get(f"/jobs/{job.id}/files/1").content == b"video"
    assert client.get(f"/jobs/{job.id}/files/2").status_code == 404
    assert client.get(f"/jobs/{job.id}/files/-1").status_code == 404
    assert client.get(f"/jobs/{job.id}/result.vtt").status_code == 404
    assert client.get("/jobs/absent/files/0").status_code == 404


def zip_names(response):
    return zipfile.ZipFile(io.BytesIO(response.content)).namelist()


def test_job_archive(client, session, dirs):
    title = "Cours: les bases? de l'IA * partie <1> avec un titre vraiment très long"
    files = [write(dirs.out / title / f"{title}_compressed_{i}.mp4", b"v" * i) for i in (10, 2)]
    files.append(write(dirs.out / title / f"{title}.srt", b"s"))
    job = add_job(session, filename=f"{title}.mp4", outputs=[str(f) for f in files])

    response = client.get(f"/jobs/{job.id}/archive.zip")
    assert response.status_code == 200
    names = zip_names(response)
    short = api._windows_name(title)
    assert names == [f"{short}/{short}.srt", f"{short}/{short}_compressed_2.mp4",
                     f"{short}/{short}_compressed_10.mp4"]        # tri naturel
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    content = zipfile.ZipFile(io.BytesIO(response.content))
    assert content.read(names[2]) == b"v" * 10


def test_job_archive_empty(client, session):
    job = add_job(session, outputs=[])
    assert client.get(f"/jobs/{job.id}/archive.zip").status_code == 404


@pytest.mark.parametrize("title, expected", [
    ("cours", "cours"),
    ('a<b>c:d"e/f\\g|h?i*j', "abcdefghij"),
    ("  espaces   multiples  ", "espaces multiples"),
    ("fin avec point.", "fin avec point"),
    ("un titre qui dépasse largement la limite de quarante caractères",
     "un titre qui dépasse largement la limite"),
    ("", "video"),
    ("???", "video"),
])
def test_windows_name(title, expected):
    name = api._windows_name(title)
    assert name == expected and len(name) <= 40


# --- Suppression -------------------------------------------------------------

def test_delete_job_keeps_files_by_default(client, session, dirs, celery):
    srt = write(dirs.out / "cours" / "cours.srt")
    job = add_job(session, status="compressing", task_id="t1", outputs=[str(srt)])
    write(dirs.work / "uploads" / job.id / "part_000000")

    assert client.delete(f"/jobs/{job.id}").json() == {"deleted": job.id, "files_deleted": 0}
    assert celery.revoked == ["t1"]
    assert srt.exists() and not (dirs.work / "uploads" / job.id).exists()
    assert client.delete(f"/jobs/{job.id}").status_code == 404


def test_delete_job_with_files_spares_other_jobs(client, session, dirs):
    folder = dirs.out / "cours"
    srt = write(folder / "cours.srt")
    part = write(folder / "cours_compressed.mp4")
    other = write(dirs.out / "autre" / "autre.srt")
    job = add_job(session, outputs=[str(srt), str(part)])
    add_job(session, outputs=[str(part)])                       # partage la vidéo

    assert client.delete(f"/jobs/{job.id}", params={"files": True}).json()["files_deleted"] == 1
    assert not srt.exists() and part.exists() and other.exists()


def test_clear_jobs(client, session, dirs):
    srt = write(dirs.out / "a" / "a.srt")
    add_job(session, filename="a.mp4", status="done", outputs=[str(srt)])
    add_job(session, filename="b.mp4", status="failed")
    add_job(session, filename="c.mp4", status="compressing")

    assert client.delete("/jobs", params={"status": "done", "files": True}).json() == {
        "deleted": 1, "files_deleted": 1}
    assert not srt.exists() and not (dirs.out / "a").exists()   # dossier vide retiré
    assert [j["status"] for j in client.get("/jobs").json()] == ["compressing", "failed"]
    assert client.delete("/jobs", params={"status": "compressing"}).status_code == 422


def test_outputs_listing_archive_and_delete(client, session, dirs):
    write(dirs.out / "cours" / "cours_compressed_1.mp4", b"12")
    write(dirs.out / "cours" / "cours.srt", b"1")
    write(dirs.out / "cours" / "cours_compressed_2.encours.mp4", b"1")
    write(dirs.out / "cours" / ".cours_compressed.decoupe" / "p0000.mp4")
    write(dirs.out / "module" / "intro" / "intro.json", b"{}")
    write(dirs.out / "divers.pdf")
    job = add_job(session, outputs=[str(dirs.out / "cours" / "cours.srt")])

    groups = {(g["subdir"], g["base"]): g for g in client.get("/outputs").json()}
    assert set(groups) == {("", "cours"), ("module", "intro")}
    assert groups[("", "cours")]["size"] == 4

    names = zip_names(client.get("/outputs/archive.zip", params={"base": "cours"}))
    assert names == ["cours/cours.srt", "cours/cours_compressed_1.mp4"]  # sans .encours

    deleted = client.delete("/outputs", params={"base": "cours"}).json()
    assert deleted == {"files_deleted": 3, "groups_deleted": 1}
    assert not (dirs.out / "cours").exists()
    assert session.get(Job, job.id) is None or client.get(f"/jobs/{job.id}").status_code == 404

    assert client.delete("/outputs", params={"all": True}).json()["groups_deleted"] == 1
    assert (dirs.out / "divers.pdf").exists()                   # hors motif : conservé
    assert client.delete("/outputs").status_code == 400


def test_delete_media_removes_everything(client, session, dirs, celery):
    source = write(dirs.media / "module" / "cours.mp4")
    keep = write(dirs.media / "module" / "garder.mp4")
    compressed = write(dirs.out / "module" / "cours" / "cours_compressed.mp4")
    chunks = write(dirs.work / "module" / "cours" / "chunk_0000.wav")
    add_job(session, source_path=str(source), status="compressing", task_id="t9")

    data = client.delete("/media", params={"path": str(source)}).json()
    assert data["files_deleted"] == 1 and data["jobs_deleted"] == 1
    assert celery.revoked == ["t9"]
    assert not source.exists() and not compressed.exists() and not chunks.exists()
    assert keep.exists()
    assert client.delete("/media", params={"path": str(source)}).status_code == 404


# --- RAG ---------------------------------------------------------------------

def test_index_requires_finished_job(client, session):
    job = add_job(session, status="transcribing")
    assert client.post(f"/jobs/{job.id}/index").status_code == 409
    assert client.post("/jobs/absent/index").status_code == 404


def test_index_job_in_background(client, session, dirs, monkeypatch):
    from source import rag

    transcript = write(dirs.out / "c" / "c.json",
                       b'{"segments": [{"start": 0, "end": 1, "text": "bonjour"}]}')
    job = add_job(session, outputs=[str(transcript)])
    indexed = []
    monkeypatch.setattr(rag, "index", lambda path: indexed.append(path) or 7)

    assert client.post(f"/jobs/{job.id}/index").json()["indexed"].startswith("indexation")
    assert indexed == [dirs.out / "c" / "c.rag.jsonl"]          # fabriqué depuis le JSON
    assert client.get(f"/jobs/{job.id}").json()["indexed"] == "7 passages indexés"


def test_index_failure_is_reported(client, session, monkeypatch):
    job = add_job(session, outputs=["/nulle/part.mp4"])
    assert client.post(f"/jobs/{job.id}/index").status_code == 200
    assert client.get(f"/jobs/{job.id}").json()["indexed"] == "aucune transcription à indexer"


def test_search_and_sources_unavailable(client, monkeypatch):
    from source import rag

    def down(*args, **kwargs):
        raise ConnectionError("qdrant")

    monkeypatch.setattr(rag, "search", down)
    monkeypatch.setattr(rag, "sources", down)
    assert client.get("/search", params={"q": "agent"}).status_code == 503
    assert client.get("/sources").status_code == 503
    assert client.get("/search", params={"q": "a"}).status_code == 422


def test_search_ok(client, monkeypatch):
    from source import rag

    monkeypatch.setattr(rag, "search", lambda q, limit, source=None, **filtres: [{"q": q, "s": source}])
    assert client.get("/search", params={"q": "agent", "source": "cours"}).json() == [
        {"q": "agent", "s": "cours"}]


def test_frames_served(client, dirs):
    write(dirs.out / "cours" / "cours_frames" / "frame_000001s.jpg", b"jpeg")
    assert client.get("/frames/cours/cours_frames/frame_000001s.jpg").content == b"jpeg"
    assert client.get("/frames/cours/absent.jpg").status_code == 404


def test_part_model_tracks_upload(client, session):
    job = client.post("/jobs", json={"filename": "a.mp4", "size": 2}).json()
    client.put(f"/jobs/{job['id']}/parts/1", content=b"ab")
    assert session.query(Part).filter_by(job_id=job["id"]).one().size == 2


def test_outputs_are_found_through_the_title(client, session, dirs, celery):
    """Fichiers rangés sous le titre tiré du contenu : suppression et zip suivent."""
    folder = dirs.out / "Installer Docker sous Windows"
    srt = write(folder / "Installer Docker sous Windows.srt", b"1")
    part = write(folder / "Installer Docker sous Windows_compressed.mp4", b"video")
    job = add_job(session, filename="VID_20260918.mp4",
                  title="Installer Docker sous Windows", outputs=[str(srt)])

    listed = client.get("/jobs").json()[0]
    assert listed["title"] == "Installer Docker sous Windows"
    assert zip_names(client.get(f"/jobs/{job.id}/archive.zip")) == [
        "Installer Docker sous Windows/Installer Docker sous Windows.srt"]

    # La vidéo compressée n'est pas dans « outputs » : elle est retrouvée par le titre.
    assert client.delete(f"/jobs/{job.id}", params={"files": True}).json()["files_deleted"] == 2
    assert not srt.exists() and not part.exists()


def test_delete_media_removes_titled_outputs(client, session, dirs):
    source = write(dirs.media / "VID_20260918.mp4")
    titled = write(dirs.out / "Réunion budget" / "Réunion budget_compressed.mp4", b"v")
    add_job(session, filename="VID_20260918.mp4", source_path=str(source),
            title="Réunion budget")

    assert client.delete("/media", params={"path": str(source)}).json()["files_deleted"] == 1
    assert not titled.exists() and not source.exists()


def test_search_passes_filters(client, monkeypatch):
    from source import rag

    recu = {}
    monkeypatch.setattr(rag, "search", lambda q, limit, **kw: recu.update(q=q, limit=limit, **kw) or [])
    client.get("/search", params={"q": "agent", "limit": 20, "source": "cours",
                                  "kind": "speech", "min_score": 0.85})
    assert recu == {"q": "agent", "limit": 20, "source": "cours", "kind": "speech",
                    "min_score": 0.85}


@pytest.mark.parametrize("params", [
    {"q": "agent", "kind": "video"}, {"q": "agent", "min_score": 2},
    {"q": "agent", "limit": 0}, {"q": "agent", "limit": 500},
])
def test_search_rejects_invalid_filters(client, params):
    assert client.get("/search", params=params).status_code == 422


def test_search_ready(client, monkeypatch):
    from source import rag

    monkeypatch.setattr(rag, "ready", lambda: False)
    assert client.get("/search/ready").json() == {"ready": False}


def test_media_listing_reports_processed_files(client, dirs, session):
    """L'interface masque ce qui est déjà traité : l'API doit le dire."""
    fait = write(dirs.media / "fait.mp4")
    encours = write(dirs.media / "encours.mp4")
    write(dirs.media / "neuf.mp4")
    dossier_fait = write(dirs.media / "cours" / "a.mp4")
    write(dirs.media / "cours" / "b.mp4")
    add_job(session, source_path=str(fait), status="done")
    add_job(session, source_path=str(encours), status="compressing")
    add_job(session, source_path=str(dossier_fait), status="done")
    add_job(session, source_path=str(dirs.media / "neuf.mp4"), status="failed")  # échec : à refaire

    etat = {e["name"]: (e["done"], e["active"]) for e in client.get("/media").json()}
    assert etat == {"cours": (1, 0), "encours.mp4": (0, 1), "fait.mp4": (1, 0), "neuf.mp4": (0, 0)}


def test_folder_skips_already_processed_files(client, dirs, session, celery):
    fait = write(dirs.media / "cours" / "a.mp4")
    write(dirs.media / "cours" / "b.mp4")
    write(dirs.media / "cours" / "c.mp4")
    add_job(session, source_path=str(fait), status="done")

    data = client.post("/jobs", json={"path": str(dirs.media / "cours"), "skip_done": True}).json()
    assert (data["count"], data["already_done"]) == (2, 1)
    assert sorted(j["filename"] for j in data["jobs"]) == ["b.mp4", "c.mp4"]


def test_folder_without_skip_done_reprocesses_everything(client, dirs, session, celery):
    """Sans l'option, comportement d'origine : tout est remis en file."""
    fait = write(dirs.media / "cours" / "a.mp4")
    add_job(session, source_path=str(fait), status="done")
    data = client.post("/jobs", json={"path": str(dirs.media / "cours")}).json()
    assert (data["count"], data["already_done"]) == (1, 0)


def test_job_from_a_link(client, celery, session, monkeypatch):
    from source import download

    monkeypatch.setattr(download.socket, "getaddrinfo",
                        lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))])
    job = client.post("/jobs", json={"url": "https://framatube.org/w/9c9de5e8",
                                     "mode": "process", "options": {"language": "en"}})
    assert job.status_code == 201
    data = job.json()
    assert data["status"] == "queued" and data["filename"] == "framatube.org/w/9c9de5e8"
    assert celery.queued == [data["id"]]
    assert session.get(Job, data["id"]).options == {
        "language": "en", "url": "https://framatube.org/w/9c9de5e8"}


def test_link_refused_before_queueing(client, celery, monkeypatch):
    from source import download

    monkeypatch.setattr(download.socket, "getaddrinfo",
                        lambda host, port: [(None, None, None, None, ("127.0.0.1", 0))])
    response = client.post("/jobs", json={"url": "http://piege.exemple.org/"})
    assert response.status_code == 400 and "non publique" in response.json()["detail"]
    assert celery.queued == []


def test_url_cannot_be_smuggled_in_options(client, dirs, session, probe_ok):
    """Seul le champ « url » déclenche un téléchargement, jamais une option."""
    source = write(dirs.media / "cours.mp4")
    client.post("/jobs", json={"path": str(source), "options": {"url": "http://10.0.0.5/"}})
    assert "url" not in session.query(Job).one().options


def test_original_subtitles_are_grouped_and_deleted_with_the_video(client, session, dirs):
    dossier = dirs.out / "Cours"
    for nom in ("Cours.srt", "Cours.en.srt", "Cours_compressed.mp4"):
        write(dossier / nom)
    groupes = client.get("/outputs").json()
    assert [(g["base"], len(g["files"])) for g in groupes] == [("Cours", 3)]
    assert client.delete("/outputs", params={"base": "Cours"}).json()["files_deleted"] == 3
