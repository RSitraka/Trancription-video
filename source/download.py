"""Téléchargement d'une vidéo à partir d'un lien (yt-dlp).

Pratique pour tester sans avoir de fichier sous la main : un lien PeerTube,
Internet Archive, Dailymotion ou un lien direct vers un .mp4. La vidéo est
récupérée **avec sa piste audio** (image et son fusionnés), sous son vrai
titre, puis traitée comme un fichier envoyé.

Sécurité : le serveur va chercher l'adresse donnée. Un lien vers le réseau
interne (127.0.0.1, 192.168.x.x, services Docker…) permettrait de lire des
services qui ne sont pas exposés : seuls les hôtes publics sont acceptés.
"""

from __future__ import annotations

import ipaddress
import logging
import shutil
import socket
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from source.config import settings

log = logging.getLogger(__name__)

Progress = Callable[[float], None] | None

# Qualité suffisante pour transcrire et compresser, sans télécharger de la 4K.
FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
# Marge laissée libre sur le disque de travail.
SPACE_MARGIN = 2 * 1024**3


class DownloadError(RuntimeError):
    """Téléchargement impossible, avec un message compréhensible."""


def check_url(url: str) -> str:
    """Refuse ce qui n'est pas un lien web vers un hôte public."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("lien invalide : une adresse http:// ou https:// est attendue")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror as error:
        raise ValueError(f"site introuvable : {parsed.hostname}") from error
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            raise ValueError(f"adresse non publique refusée : {parsed.hostname} ({address})")
    return url


def _explain(message: str) -> str:
    """Traduit les refus les plus fréquents des sites en consigne claire."""
    lowered = message.lower()
    if "not a bot" in lowered or "sign in to confirm" in lowered:
        return ("YouTube bloque les téléchargements depuis ce serveur (vérification "
                "anti-robot). Télécharge la vidéo sur ton PC et envoie le fichier, ou "
                "essaie un autre lien (PeerTube, Internet Archive, lien direct .mp4).")
    if "logged-in" in lowered or "login" in lowered or "cookies" in lowered:
        return "ce site exige un compte connecté : envoie plutôt le fichier téléchargé."
    if "unsupported url" in lowered:
        return "aucune vidéo reconnue à cette adresse."
    if "404" in lowered:
        return "vidéo introuvable à cette adresse (erreur 404)."
    if "larger than max-filesize" in lowered or "max-filesize" in lowered:
        return "vidéo trop grosse pour l'espace disque disponible."
    return message.splitlines()[0][:300]


def download(url: str, destination: Path, on_progress: Progress = None) -> Path:
    """Télécharge la vidéo dans `destination` et renvoie le chemin du fichier."""
    import yt_dlp

    check_url(url)
    destination.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination).free - SPACE_MARGIN
    if free <= 0:
        raise DownloadError("espace disque insuffisant pour télécharger une vidéo")

    def hook(state: dict) -> None:
        if on_progress and state.get("status") == "downloading":
            total = state.get("total_bytes") or state.get("total_bytes_estimate")
            if total:
                on_progress(min(state.get("downloaded_bytes", 0) / total, 1.0))

    options = {
        "format": FORMAT,
        "merge_output_format": "mp4",
        # Vrai titre de la vidéo, sans caractère refusé par Windows.
        "outtmpl": str(destination / "%(title).120B.%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": True,
        "max_filesize": free,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as error:
        raise DownloadError(_explain(str(error))) from error

    files = [Path(d["filepath"]) for d in info.get("requested_downloads", []) if d.get("filepath")]
    files = [f for f in files if f.is_file()] or sorted(
        (f for f in destination.iterdir() if f.is_file() and not f.name.endswith(".part")),
        key=lambda f: f.stat().st_size, reverse=True)
    if not files:
        raise DownloadError("aucune vidéo n'a été récupérée à cette adresse")
    log.info("téléchargé : %s (%.0f Mo)", files[0].name, files[0].stat().st_size / 1e6)
    return files[0]
