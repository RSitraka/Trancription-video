"""Tests de sécurité : voir docs/SECURITE.md.

Chaque protection décrite dans le document (SEC-01 à SEC-14) a ses tests. Une
vulnérabilité découverte et pas encore corrigée s'ajoute ici marquée
`xfail(strict=True)` : le test décrit le comportement attendu, et passera au
rouge le jour où le correctif est livré — retirer alors le marqueur.
"""

import io
import os
import re
import zipfile
from pathlib import Path

import pytest
from conftest import add_job

from source import api, auth

ROOT = Path(__file__).resolve().parent.parent


def write(path, data=b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def probe_ok(monkeypatch):
    monkeypatch.setattr(api.media, "probe", lambda path: None)


# --- SEC-01 : confinement des chemins sources --------------------------------

@pytest.mark.parametrize("path", ["/etc/passwd", "/app/source/api.py", "../../etc/passwd"])
def test_job_path_outside_allowed_roots(client, path, celery):
    assert client.post("/jobs", json={"path": path}).status_code == 403
    assert celery.queued == []


def test_job_path_dotdot_escape(client, dirs, celery):
    secret = write(dirs.outside / "secret.mp4")
    escape = f"{dirs.media}/../outside/secret.mp4"
    assert client.post("/jobs", json={"path": escape}).status_code == 403
    assert secret.exists() and celery.queued == []


def test_job_path_symlink_escape(client, dirs, celery):
    write(dirs.outside / "secret.mp4")
    (dirs.media / "lien.mp4").symlink_to(dirs.outside / "secret.mp4")
    (dirs.media / "dossier").symlink_to(dirs.outside, target_is_directory=True)
    assert client.post("/jobs", json={"path": str(dirs.media / "lien.mp4")}).status_code == 403
    assert client.post("/jobs", json={"path": str(dirs.media / "dossier")}).status_code == 403


def test_error_on_forbidden_path_does_not_reveal_existence(client, dirs):
    absent = client.post("/jobs", json={"path": "/nexiste/pas.mp4"})
    present = client.post("/jobs", json={"path": "/etc/hostname"})
    assert absent.status_code == present.status_code == 403


# --- SEC-02 : sous-dossier de sortie fixé par le serveur ---------------------

def test_client_cannot_choose_output_subdir(client, dirs, probe_ok, session):
    source = write(dirs.media / "cours.mp4")
    client.post("/jobs", json={"path": str(source), "options": {"subdir": "../../../tmp"}})
    job = session.query(api.Job).one()
    assert "subdir" not in job.options


def test_pipeline_refuses_escaping_subdir(tmp_path):
    from source import pipeline

    with pytest.raises(ValueError):
        pipeline.title_dir(tmp_path / "out", Path("x.mp4"), "../../etc")


# --- SEC-03 : service des fichiers limité au dossier de sortie ---------------

@pytest.mark.parametrize("name", ["../outside/secret.jpg", "..%2Foutside%2Fsecret.jpg",
                                  "cours/../../outside/secret.jpg", "/etc/passwd"])
def test_frames_traversal(client, dirs, name):
    write(dirs.outside / "secret.jpg", b"SECRET")
    response = client.get(f"/frames/{name}")
    assert response.status_code == 404 and b"SECRET" not in response.content


def test_frames_symlink_escape(client, dirs):
    write(dirs.outside / "secret.jpg", b"SECRET")
    (dirs.out / "lien.jpg").symlink_to(dirs.outside / "secret.jpg")
    assert client.get("/frames/lien.jpg").status_code == 404


def test_job_archive_ignores_outputs_outside_output_dir(client, session, dirs):
    inside = write(dirs.out / "cours" / "cours.srt", b"ok")
    outside = write(dirs.outside / "cours_compressed.mp4", b"SECRET")
    job = add_job(session, outputs=[str(inside), str(outside), "/etc/passwd"])
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"/jobs/{job.id}/archive.zip").content))
    assert archive.namelist() == ["cours/cours.srt"]


@pytest.mark.parametrize("params", [
    {"base": "../outside/cours"}, {"base": "cours", "subdir": "../outside"},
    {"base": "..", "subdir": ".."}, {"base": "cours", "subdir": "/"},
])
def test_outputs_archive_traversal(client, dirs, params):
    write(dirs.outside / "cours.srt", b"SECRET")
    write(dirs.outside / "cours" / "cours.srt", b"SECRET")
    response = client.get("/outputs/archive.zip", params=params)
    assert response.status_code == 404 and b"SECRET" not in response.content


@pytest.mark.parametrize("params", [
    {"base": "../outside/cours"}, {"base": "cours", "subdir": "../outside"},
    {"base": "cours", "subdir": "../../"}, {"base": ".*", "subdir": ""},
])
def test_delete_outputs_cannot_escape(client, dirs, params):
    victims = [write(dirs.outside / "cours.srt"), write(dirs.outside / "cours" / "cours.srt"),
               write(dirs.media / "cours.mp4"), write(dirs.out / "autre" / "autre.srt")]
    client.delete("/outputs", params=params)
    assert all(v.exists() for v in victims)


# --- SEC-04 : suppression des sources confinée à MEDIA_PATH -----------------

@pytest.mark.parametrize("target", ["root", "parent", "outside", "work", "dotdot", "relative"])
def test_delete_media_confinement(client, dirs, target):
    secret = write(dirs.outside / "secret.mp4")
    work = write(dirs.work / "sources" / "x.mp4")
    path = {"root": str(dirs.media), "parent": str(dirs.media.parent),
            "outside": str(secret), "work": str(work),
            "dotdot": f"{dirs.media}/../outside/secret.mp4",
            "relative": "../outside/secret.mp4"}[target]
    assert client.delete("/media", params={"path": path}).status_code == 403
    assert secret.exists() and work.exists() and dirs.media.exists()


def test_delete_media_symlink_does_not_follow_outside(client, dirs):
    secret = write(dirs.outside / "secret.mp4")
    (dirs.media / "lien.mp4").symlink_to(secret)
    assert client.delete("/media", params={"path": str(dirs.media / "lien.mp4")}).status_code == 403
    assert secret.exists()


# --- SEC-05 : archives .zip sûres à l'extraction -----------------------------

@pytest.mark.parametrize("title", ["../../evil", "..\\..\\evil", "C:evil", "a/b/c",
                                   "\x00nul\x1f", "con?*<>|\""])
def test_zip_entries_cannot_escape_on_extraction(client, session, dirs, title):
    folder = dirs.out / "x"
    safe = re.sub(r"[/\\\x00]", "_", title)
    part = write(folder / f"{safe}_compressed.mp4", b"v")
    job = add_job(session, filename=f"{title}.mp4", outputs=[str(part)])
    response = client.get(f"/jobs/{job.id}/archive.zip")
    for name in zipfile.ZipFile(io.BytesIO(response.content)).namelist():
        parts = name.split("/")
        assert len(parts) == 2 and ".." not in parts
        assert not re.search(r'[<>:"\\|?*\x00-\x1f]', name)
        assert not name.startswith("/")
    assert re.fullmatch(r"attachment; filename\*=UTF-8''[\w.%\-~]+",
                        response.headers["content-disposition"])


# --- SEC-06 : validation des entrées -----------------------------------------

def test_clear_jobs_status_is_whitelisted(client, session):
    add_job(session, status="compressing")
    for status in ["compressing", "done' OR '1'='1", "*"]:
        assert client.delete("/jobs", params={"status": status}).status_code == 422
    assert len(client.get("/jobs").json()) == 1


def test_mode_is_whitelisted(client, dirs):
    source = write(dirs.media / "a.mp4")
    assert client.post("/jobs", json={"path": str(source), "mode": "__class__"}).status_code == 400


def test_job_id_injection_is_harmless(client, session):
    add_job(session)
    for job_id in ["' OR 1=1 --", "../../etc", "%00"]:
        assert client.get(f"/jobs/{job_id}").status_code == 404
        assert client.delete(f"/jobs/{job_id}").status_code == 404
    assert len(client.get("/jobs").json()) == 1


def test_json_body_required_blocks_simple_csrf(client, dirs, celery):
    """Un formulaire d'un site tiers envoie du text/plain : il doit être refusé."""
    source = write(dirs.media / "a.mp4")
    response = client.post("/jobs", content=f'{{"path": "{source}"}}',
                           headers={"Content-Type": "text/plain"})
    assert response.status_code == 422 and celery.queued == []


def test_no_permissive_cors(client):
    response = client.options("/jobs", headers={
        "Origin": "https://malveillant.example", "Access-Control-Request-Method": "DELETE"})
    assert "access-control-allow-origin" not in response.headers


# --- SEC-07 : conteneurs et configuration ------------------------------------

def test_dockerfile_runs_as_non_root():
    dockerfile = (ROOT / "Dockerfile").read_text()
    runtime = dockerfile.split("AS test")[0]
    users = re.findall(r"^USER\s+(\S+)", runtime, re.M)
    assert users and users[-1] not in ("root", "0")


def compose():
    import yaml

    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]


def test_worker_and_cli_mount_media_read_only():
    services = compose()
    for name in ("worker", "cli"):
        mounts = [v for v in services[name]["volumes"] if v.endswith(":/media:ro")
                  or ":/media" in v]
        assert mounts and all(v.endswith(":ro") for v in mounts), name


def test_internal_services_not_published():
    services = compose()
    for name in ("postgres", "redis"):
        assert "ports" not in services[name], f"{name} ne doit pas être exposé sur l'hôte"


def test_env_file_never_shipped():
    assert re.search(r"^\.env$", (ROOT / ".gitignore").read_text(), re.M)
    assert re.search(r"^\.env$", (ROOT / ".dockerignore").read_text(), re.M)


def test_ui_escapes_server_data():
    html = (ROOT / "source" / "static" / "index.html").read_text(encoding="utf-8")
    assert re.search(r"const esc\s*=|function esc\s*\(", html)
    fields = "filename|error|name|base|subdir|folder|path|language|indexed"
    # Une ligne qui fabrique du HTML (balise) n'insère de donnée serveur que via
    # esc(). Les autres (textContent, toast, confirmation) sont sûres par nature.
    raw = [line.strip() for line in html.splitlines()
           if re.search(r"<[a-z]", line) and re.search(rf"\$\{{\w+\.({fields})\}}", line)]
    assert not raw, f"donnée insérée en HTML sans échappement : {raw}"


# --- SEC-08 : nom de fichier d'upload (ex-VULN-01) ---------------------------

@pytest.mark.parametrize("kind", ["relatif", "absolu"])
def test_upload_filename_cannot_escape(client, dirs, celery, kind):
    target = dirs.work / "evil.mp4" if kind == "relatif" else dirs.outside / "evil.mp4"
    filename = "../../evil.mp4" if kind == "relatif" else str(target)
    assert client.post("/jobs", json={"filename": filename, "size": 4}).status_code == 400
    assert not target.exists() and celery.queued == []


@pytest.mark.parametrize("filename", ["..", ".", "a/b.mp4", "..\\..\\x.mp4", "x\x00.mp4",
                                      " ", "x" * 256, 42, ["a.mp4"]])
def test_upload_filename_rejected(client, filename):
    assert client.post("/jobs", json={"filename": filename, "size": 1}).status_code == 400


@pytest.mark.parametrize("filename", ["Cours n°1 : l'IA (partie 2).mp4", "vidéo..finale.mkv",
                                      ".cache.mp4", "日本語.mp4"])
def test_upload_filename_simple_names_accepted(client, dirs, filename):
    job = client.post("/jobs", json={"filename": filename, "size": 1}).json()
    client.put(f"/jobs/{job['id']}/parts/0", content=b"x")
    assert client.post(f"/jobs/{job['id']}/complete").status_code == 200
    assert (dirs.work / "sources" / job["id"] / filename).read_bytes() == b"x"


def test_complete_rechecks_legacy_job_filename(client, session, dirs, celery):
    """Job créé avant le correctif, avec un nom dangereux déjà en base."""
    job = add_job(session, filename="../../evil.mp4", status="uploading", size=4)
    write(dirs.work / "uploads" / job.id / "part_000000", b"EVIL")
    assert client.post(f"/jobs/{job.id}/complete").status_code == 400
    assert not (dirs.work / "evil.mp4").exists() and celery.queued == []


# --- SEC-09 : ports publiés sur la boucle locale (ex-VULN-05) ---------------

def default_host(mapping: str) -> str:
    """« ${BIND_ADDRESS:-127.0.0.1}:${API_PORT:-8000}:8000 » → « 127.0.0.1 »."""
    resolved = re.sub(r"\$\{\w+:-([^}]*)\}", r"\1", mapping)
    parts = resolved.split(":")
    return parts[0] if len(parts) == 3 else "0.0.0.0"


@pytest.mark.parametrize("service", ["api", "qdrant"])
def test_ports_bound_to_localhost_by_default(service):
    ports = compose()[service]["ports"]
    assert ports and all(default_host(p) == "127.0.0.1" for p in ports), ports


# --- SEC-10 : jeton d'accès (ex-VULN-03) -------------------------------------

PROTECTED = [
    ("GET", "/api"), ("GET", "/media"), ("GET", "/jobs"), ("GET", "/jobs/x"),
    ("GET", "/jobs/x/parts"), ("GET", "/jobs/x/files/0"), ("GET", "/jobs/x/archive.zip"),
    ("GET", "/jobs/x/result.srt"), ("GET", "/outputs"), ("GET", "/outputs/archive.zip?base=a"),
    ("GET", "/settings"), ("GET", "/search?q=agent"), ("GET", "/search/ready"),
    ("GET", "/sources"),
    ("GET", "/frames/a.jpg"), ("POST", "/jobs"), ("PUT", "/jobs/x/parts/0"),
    ("POST", "/jobs/x/complete"), ("POST", "/jobs/x/index"), ("DELETE", "/jobs/x"),
    ("DELETE", "/jobs?status=done"), ("DELETE", "/outputs?all=true"), ("DELETE", "/media?path=/x"),
]


@pytest.mark.parametrize("method, url", PROTECTED)
def test_routes_require_token(anonymous, method, url):
    response = anonymous.request(method, url, json={"path": "/x"} if method == "POST" else None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_every_route_is_protected_or_listed_public(anonymous):
    """Une route ajoutée plus tard est protégée d'office : la dépendance est globale."""
    from fastapi.routing import APIRoute

    routes = [r for r in api.app.routes if isinstance(r, APIRoute)]
    assert routes and all(r.path in auth.PUBLIC_PATHS or
                          any(d.call is auth.require for d in r.dependant.dependencies)
                          for r in routes)
    assert auth.PUBLIC_PATHS == {"/", "/favicon.ico", "/health", "/login", "/logout"}


def test_destructive_route_without_token_deletes_nothing(anonymous, dirs, session, celery):
    source = write(dirs.media / "precieux.mp4")
    produced = write(dirs.out / "precieux" / "precieux.srt")
    add_job(session, source_path=str(source), status="compressing", task_id="t1")
    assert anonymous.delete("/media", params={"path": str(source)}).status_code == 401
    assert anonymous.delete("/outputs", params={"all": True}).status_code == 401
    assert source.exists() and produced.exists() and celery.revoked == []


def test_public_routes(anonymous):
    assert anonymous.get("/health").status_code == 200
    assert anonymous.get("/").status_code == 200              # formulaire de connexion
    assert anonymous.get("/favicon.ico").status_code == 200   # icône de l'onglet
    assert anonymous.get("/static/icon-32.png").status_code == 200


@pytest.mark.parametrize("header", ["Bearer mauvais", "Basic amV0b24=", "Bearer", "jeton-de-test-0123456789",
                                    "Bearer jeton-de-test-012345678", "Bearer jeton-de-test-0123456789x"])
def test_wrong_credentials(anonymous, header):
    assert anonymous.get("/jobs", headers={"Authorization": header}).status_code == 401


def test_token_in_query_string_is_ignored(anonymous, api_token):
    """Un jeton dans l'URL finirait dans les journaux et l'historique."""
    assert anonymous.get(f"/jobs?token={api_token}").status_code == 401


def test_bearer_scheme_is_case_insensitive(anonymous, api_token):
    assert anonymous.get("/jobs", headers={"Authorization": f"bearer {api_token}"}).status_code == 200


def test_login_sets_strict_http_only_cookie(anonymous, api_token):
    assert anonymous.post("/login", json={"token": "mauvais"}).status_code == 401
    assert not anonymous.cookies

    response = anonymous.post("/login", json={"token": api_token})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert api_token not in cookie                              # jamais le jeton lui-même
    assert anonymous.get("/jobs").status_code == 200            # la session suffit

    anonymous.post("/logout")
    assert anonymous.get("/jobs").status_code == 401


@pytest.mark.parametrize("payload", [{}, {"token": None}, {"token": ["x"]}, {"token": ""}])
def test_login_rejects_malformed(anonymous, payload):
    assert anonymous.post("/login", json=payload).status_code == 401


def test_login_requires_json_body(anonymous, api_token):
    response = anonymous.post("/login", content=f'{{"token": "{api_token}"}}',
                              headers={"Content-Type": "text/plain"})
    assert response.status_code == 422 and "set-cookie" not in response.headers


def test_forged_cookie_rejected(anonymous):
    anonymous.cookies.set(auth.COOKIE, "0" * 64)
    assert anonymous.get("/jobs").status_code == 401


def test_changing_token_revokes_sessions(anonymous, api_token, monkeypatch):
    anonymous.post("/login", json={"token": api_token})
    monkeypatch.setattr(api.settings, "api_token", "nouveau-jeton")
    assert anonymous.get("/jobs").status_code == 401


def test_generated_token_is_persistent_and_private(tmp_path, monkeypatch):
    path = tmp_path / "data" / "api_token"
    monkeypatch.setattr(api.settings, "api_token", None)
    monkeypatch.setattr(api.settings, "api_token_file", path)
    first = auth.token()
    assert len(first) >= 40 and path.read_text().strip() == first
    assert path.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(auth, "_generated", None)                # redémarrage de l'API
    assert auth.token() == first


def test_ui_auto_login_clears_token_from_address_bar():
    html = (ROOT / "source" / "static" / "index.html").read_text(encoding="utf-8")
    auto = html[html.index("async function autoLogin"):]
    # L'URL est nettoyée avant la requête de connexion, et avant tout autre appel.
    assert auto.index("history.replaceState") < auto.index("await login(token)")
    assert html.index("autoLogin().then") > html.index("async function autoLogin")
    launcher = (ROOT / "scripts" / "lancer.sh").read_text()
    assert "#jeton=" in launcher and "?jeton=" not in launcher      # fragment, jamais la requête


def test_ui_asks_for_login_on_401():
    html = (ROOT / "source" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="loginDialog"' in html and 'type="password"' in html
    assert "response.status === 401" in html and "fetch('/login'" in html


# --- SEC-14 : mode local sans connexion (défaut) -----------------------------

@pytest.fixture
def local(monkeypatch, celery):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api.settings, "require_token", False)
    return TestClient(api.app)


def test_local_mode_is_the_default():
    from source.config import Settings

    assert Settings.model_fields["require_token"].default is False
    assert Settings.model_fields["allowed_hosts"].default == "localhost,127.0.0.1,::1"
    assert re.search(r"^REQUIRE_TOKEN=false$", (ROOT / ".env.example").read_text(), re.M)


def test_local_mode_needs_no_login(local):
    assert local.get("/jobs").status_code == 200
    assert local.post("/jobs", json={"filename": "a.mp4", "size": 1}).status_code == 201


@pytest.mark.parametrize("host", ["malveillant.example", "malveillant.example:8100",
                                  "192.168.1.20:8100", "localhost.malveillant.example", ""])
def test_local_mode_rejects_foreign_host(local, host):
    """DNS rebinding : un domaine tiers qui pointe sur 127.0.0.1 garde son propre Host."""
    response = local.get("/jobs", headers={"Host": host})
    assert response.status_code == 403


@pytest.mark.parametrize("host", ["localhost:8100", "127.0.0.1:8100", "[::1]:8100", "LOCALHOST"])
def test_local_mode_accepts_local_hosts(local, monkeypatch, host):
    monkeypatch.setattr(api.settings, "allowed_hosts", "localhost,127.0.0.1,::1")
    assert local.get("/jobs", headers={"Host": host}).status_code == 200


def test_allowed_hosts_can_be_extended(local, monkeypatch):
    monkeypatch.setattr(api.settings, "allowed_hosts", "localhost,poste.bureau.lan")
    assert local.get("/jobs", headers={"Host": "poste.bureau.lan:8100"}).status_code == 200


@pytest.mark.parametrize("origin", ["https://malveillant.example", "null", "http://localhost.evil",
                                    "file://", "http://127.0.0.2:8100"])
def test_local_mode_rejects_cross_site_writes(local, dirs, origin):
    source = write(dirs.media / "precieux.mp4")
    response = local.delete("/media", params={"path": str(source)}, headers={"Origin": origin})
    assert response.status_code == 403 and source.exists()


def test_local_mode_accepts_same_origin_writes(local, dirs):
    source = write(dirs.media / "a.mp4")
    headers = {"Origin": "http://localhost:8100", "Sec-Fetch-Site": "same-origin"}
    assert local.delete("/media", params={"path": str(source)}, headers=headers).status_code == 200


def test_local_mode_rejects_cross_site_reads(local):
    """fetch() ou <img> depuis un autre site : Sec-Fetch-Site le trahit."""
    headers = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}
    assert local.get("/jobs", headers=headers).status_code == 403
    assert local.get("/frames/a.jpg", headers=headers).status_code == 403


def test_local_mode_allows_navigation_from_elsewhere(local):
    """Un lien ou un favori vers l'interface doit s'ouvrir normalement."""
    headers = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
    assert local.get("/", headers=headers).status_code == 200


def test_token_mode_still_checks_host(client):
    assert client.get("/jobs", headers={"Host": "malveillant.example"}).status_code == 403


def test_launcher_passes_token_only_in_token_mode():
    launcher = (ROOT / "scripts" / "lancer.sh").read_text()
    assert "REQUIRE_TOKEN=(true" in launcher


# --- SEC-11 : uploads bornés (ex-VULN-02) ------------------------------------

def upload(client, size=10):
    return client.post("/jobs", json={"filename": "a.mp4", "size": size}).json()["id"]


@pytest.mark.parametrize("size", [0, -1, "10", True, None, 1.5, [10]])
def test_declared_size_must_be_positive_integer(client, size):
    assert client.post("/jobs", json={"filename": "a.mp4", "size": size}).status_code == 400


def test_declared_size_bounded_by_free_disk(client, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(api.shutil, "disk_usage",
                        lambda path: SimpleNamespace(total=10**12, used=0, free=5 * 10**9))
    response = client.post("/jobs", json={"filename": "a.mp4", "size": 5 * 10**9})
    assert response.status_code == 507 and "espace disque" in response.json()["detail"]
    assert client.get("/jobs").json() == []
    assert client.post("/jobs", json={"filename": "a.mp4", "size": 10**9}).status_code == 201


def test_part_larger_than_declared(client, dirs):
    job = upload(client, size=10)
    assert client.put(f"/jobs/{job}/parts/0", content=b"x" * 10_000).status_code == 413
    assert not list((dirs.work / "uploads" / job).iterdir())
    assert client.get(f"/jobs/{job}/parts").json() == {"received": [], "bytes": 0}


def test_total_of_parts_bounded(client):
    job = upload(client, size=10)
    assert client.put(f"/jobs/{job}/parts/0", content=b"x" * 6).status_code == 200
    assert client.put(f"/jobs/{job}/parts/1", content=b"x" * 5).status_code == 413
    assert client.put(f"/jobs/{job}/parts/1", content=b"x" * 4).status_code == 200
    # Renvoyer un morceau ne compte pas deux fois.
    assert client.put(f"/jobs/{job}/parts/0", content=b"y" * 6).status_code == 200


def test_streamed_part_without_length_is_cut(client, dirs):
    """Sans Content-Length (envoi « chunked »), la limite s'applique pendant la réception."""
    job = upload(client, size=10)
    client.put(f"/jobs/{job}/parts/0", content=b"ok")

    def body():
        for _ in range(100):
            yield b"z" * 1000

    assert client.put(f"/jobs/{job}/parts/0", content=body()).status_code == 413
    assert (dirs.work / "uploads" / job / "part_000000").read_bytes() == b"ok"  # intact
    assert [p.name for p in (dirs.work / "uploads" / job).iterdir()] == ["part_000000"]


def test_part_bounded_by_max_part_size(client, monkeypatch):
    monkeypatch.setattr(api, "MO", 10)                          # morceau max : 128 × 10 octets
    job = upload(client, size=10_000)
    assert client.put(f"/jobs/{job}/parts/0", content=b"x" * 1281).status_code == 413
    assert client.put(f"/jobs/{job}/parts/0", content=b"x" * 1280).status_code == 200


@pytest.mark.parametrize("index", [-1, 10, 10**9])
def test_part_index_bounded(client, index):
    job = upload(client, size=10)
    assert client.put(f"/jobs/{job}/parts/{index}", content=b"x").status_code == 400


def test_no_part_after_completion(client):
    job = upload(client, size=2)
    client.put(f"/jobs/{job}/parts/0", content=b"ab")
    assert client.post(f"/jobs/{job}/complete").status_code == 200
    assert client.put(f"/jobs/{job}/parts/0", content=b"cd").status_code == 409


def test_incomplete_upload_is_not_queued(client, celery):
    job = upload(client, size=10)
    client.put(f"/jobs/{job}/parts/0", content=b"x" * 4)
    response = client.post(f"/jobs/{job}/complete")
    assert response.status_code == 400 and "4 octets reçus sur 10" in response.json()["detail"]
    assert celery.queued == [] and client.get(f"/jobs/{job}").json()["status"] == "uploading"


# --- SEC-12 : pagination bornée (ex-VULN-04) ---------------------------------

@pytest.mark.parametrize("limit, status", [(0, 422), (-5, 422), (10_001, 422), (10**9, 422),
                                           (1, 200), (10_000, 200)])
def test_jobs_limit_bounded(client, limit, status):
    assert client.get("/jobs", params={"limit": limit}).status_code == status


def test_ui_limit_within_bound():
    html = (ROOT / "source" / "static" / "index.html").read_text(encoding="utf-8")
    assert all(int(n) <= 10_000 for n in re.findall(r"/jobs\?limit=(\d+)", html))


# --- SEC-13 : aucun mot de passe par défaut (ex-VULN-06) ---------------------

def test_postgres_password_required_from_env():
    raw = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r"POSTGRES_PASSWORD: \$\{POSTGRES_PASSWORD:\?", raw)
    assert re.search(r"DATABASE_URL: postgresql://transcription:\$\{POSTGRES_PASSWORD:\?[^}]*\}@", raw)
    assert "transcription:transcription@" not in raw


def test_no_default_secrets_in_code_or_example():
    from source.config import Settings

    fields = Settings.model_fields
    assert "transcription:transcription@" not in fields["database_url"].default
    assert fields["api_token"].default is None
    example = (ROOT / ".env.example").read_text()
    assert re.search(r"^POSTGRES_PASSWORD=\s*$", example, re.M)
    assert re.search(r"^API_TOKEN=\s*$", example, re.M)
    assert "transcription:transcription@" not in example


def test_suite_documented():
    doc = ROOT / "docs" / "SECURITE.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    for ident in ["SEC-01", "SEC-14", "VULN-01", "VULN-06"]:
        assert ident in text
    assert os.path.basename(__file__) in text


def test_work_path_defaults_to_docker_volume():
    """Sans WORK_PATH, les fichiers temporaires restent dans le volume « data »."""
    raw = (ROOT / "docker-compose.yml").read_text()
    assert raw.count("${WORK_PATH:-data}:/data") == 2                  # x-app et api
    assert re.search(r"^# WORK_PATH=", (ROOT / ".env.example").read_text(), re.M)


def test_ui_exposes_rag_search_and_indexing():
    """Le serveur sait indexer et chercher : l'interface doit le proposer."""
    html = (ROOT / "source" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="view-recherche"' in html and "'recherche'" in html
    assert "json('/search?' + params)" in html and "json('/sources')" in html
    assert "/index`" in html or "/index'" in html                      # bouton Indexer


def test_ui_search_can_be_opened_from_url():
    html = (ROOT / "source" / "static" / "index.html").read_text(encoding="utf-8")
    assert "searchFromUrl" in html and "URLSearchParams(location.search)" in html


def test_network_access_script_requires_token_and_firewall():
    """Ouvrir l'API au réseau reste une action explicite et réversible."""
    script = (ROOT / "scripts" / "windows" / "acces-reseau.ps1").read_text(encoding="utf-8")
    assert "New-NetFirewallRule" in script and "portproxy" in script
    assert "-Retirer" in script                                        # refermable
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    exposition = readme[readme.index("### Accès depuis un autre PC"):][:1200]
    assert "REQUIRE_TOKEN=true" in exposition and "ALLOWED_HOSTS" in exposition


def test_ui_search_filters_and_list_filters():
    html = (ROOT / "source" / "static" / "index.html").read_text(encoding="utf-8")
    for element in ('id="searchKind"', 'id="searchScore"', 'id="searchLimit"',
                    'id="mediaFilter"', 'id="outputsFilter"', "/search/ready"):
        assert element in html, element


def test_ui_hides_processed_files():
    html = (ROOT / "source" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="showDone"' in html and "skip_done: true" in html
    assert "entries.filter((e) => remaining(e) > 0)" in html      # « Tout traiter »
