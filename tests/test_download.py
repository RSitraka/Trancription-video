"""Téléchargement par lien : liens acceptés, protection du réseau interne."""

import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from source import download


def resout(monkeypatch, *adresses):
    """Fait résoudre n'importe quel nom d'hôte vers ces adresses."""
    monkeypatch.setattr(download.socket, "getaddrinfo",
                        lambda host, port: [(None, None, None, None, (a, 0)) for a in adresses])


@pytest.mark.parametrize("url", [
    "https://framatube.org/w/9c9de5e8", "http://archive.org/details/x",
    "  https://exemple.org/video.mp4  ",
])
def test_public_links_accepted(monkeypatch, url):
    resout(monkeypatch, "93.184.216.34")
    assert download.check_url(url) == url.strip()


@pytest.mark.parametrize("url", ["ftp://exemple.org/a.mp4", "file:///etc/passwd", "exemple.org",
                                 "https://", "", "javascript:alert(1)"])
def test_non_web_links_refused(url):
    with pytest.raises(ValueError, match="lien invalide"):
        download.check_url(url)


@pytest.mark.parametrize("adresse", [
    "127.0.0.1", "192.168.1.113", "10.0.0.5", "172.20.0.3", "169.254.169.254", "::1",
    "fe80::1", "100.64.0.1", "0.0.0.0",
])
def test_internal_addresses_refused(monkeypatch, adresse):
    """Sans ce contrôle, un lien ferait lire au serveur ses propres services
    (Qdrant, PostgreSQL, métadonnées du cloud…)."""
    resout(monkeypatch, adresse)
    with pytest.raises(ValueError, match="non publique"):
        download.check_url("http://piege.exemple.org/")


def test_mixed_resolution_refused(monkeypatch):
    """Un nom qui résout aussi vers une adresse interne est refusé."""
    resout(monkeypatch, "93.184.216.34", "127.0.0.1")
    with pytest.raises(ValueError, match="non publique"):
        download.check_url("http://double.exemple.org/")


def test_unknown_host(monkeypatch):
    def introuvable(host, port):
        raise socket.gaierror("inconnu")
    monkeypatch.setattr(download.socket, "getaddrinfo", introuvable)
    with pytest.raises(ValueError, match="site introuvable"):
        download.check_url("http://postgres/")


@pytest.mark.parametrize("brut, attendu", [
    ("ERROR: [youtube] x: Sign in to confirm you’re not a bot.", "YouTube bloque"),
    ("ERROR: [vimeo] x: The web client only works when logged-in.", "compte connecté"),
    ("ERROR: Unsupported URL: https://exemple.org", "aucune vidéo reconnue"),
    ("ERROR: HTTP Error 404: Not Found", "introuvable"),
    ("ERROR: File is larger than max-filesize", "trop grosse"),
])
def test_site_refusals_are_explained(brut, attendu):
    assert attendu in download._explain(brut)


@pytest.fixture
def faux_ytdlp(monkeypatch, tmp_path):
    """Remplace yt-dlp : écrit un fichier et appelle la progression."""
    appels = {}

    class YoutubeDL:
        def __init__(self, options):
            appels["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            if "refus" in url:
                raise Erreur("ERROR: [youtube] x: Sign in to confirm you’re not a bot")
            fichier = tmp_path / "dl" / "What is PeerTube？.mp4"
            fichier.write_bytes(b"video")
            for hook in appels["options"]["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
            return {"requested_downloads": [{"filepath": str(fichier)}]}

    class Erreur(Exception):
        pass

    module = SimpleNamespace(YoutubeDL=YoutubeDL, utils=SimpleNamespace(DownloadError=Erreur))
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    resout(monkeypatch, "93.184.216.34")
    return appels


def test_download_returns_the_titled_file(faux_ytdlp, tmp_path):
    progression = []
    chemin = download.download("https://framatube.org/w/x", tmp_path / "dl", progression.append)
    assert chemin.name == "What is PeerTube？.mp4"
    assert progression == [0.5]
    options = faux_ytdlp["options"]
    assert "+ba" in options["format"]                      # image ET son
    assert options["merge_output_format"] == "mp4" and options["noplaylist"]
    assert options["windowsfilenames"] and 0 < options["max_filesize"]


def test_download_translates_site_refusal(faux_ytdlp, tmp_path):
    with pytest.raises(download.DownloadError, match="YouTube bloque"):
        download.download("https://youtube.exemple.org/refus", tmp_path / "dl")


def test_download_rechecks_the_link(faux_ytdlp, tmp_path, monkeypatch):
    """Revérifié au moment du téléchargement, pas seulement à la création."""
    resout(monkeypatch, "10.0.0.5")
    with pytest.raises(ValueError, match="non publique"):
        download.download("https://framatube.org/w/x", tmp_path / "dl")
