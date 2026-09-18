"""Accès à l'API : protections du poste local, et jeton en option.

L'API peut effacer définitivement des vidéos (docs/SECURITE.md, SEC-10).

Mode local (défaut, `REQUIRE_TOKEN=false`) — l'API n'écoute que sur 127.0.0.1 :
- seuls les noms d'hôte de `ALLOWED_HOSTS` sont servis : une page web qui
  ferait pointer son propre domaine sur 127.0.0.1 (DNS rebinding) est refusée ;
- une requête émise par un autre site web (en-têtes `Origin`,
  `Sec-Fetch-Site`) est refusée : un site ouvert dans le navigateur ne peut
  rien déclencher. Seule la navigation vers la page (lien, favori) est permise.

Mode jeton (`REQUIRE_TOKEN=true`), en plus du mode local :
- ligne de commande : `Authorization: Bearer <jeton>` ;
- navigateur : `POST /login` pose un cookie HttpOnly `SameSite=Strict`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from fastapi import HTTPException, Request

from source.config import settings

log = logging.getLogger(__name__)

COOKIE = "transcription_session"
SESSION_SECONDS = 30 * 86400
# Page d'accueil (formulaire de connexion), son icône, sonde de vie, connexion.
PUBLIC_PATHS = {"/", "/favicon.ico", "/health", "/login", "/logout"}

_generated: str | None = None


def token() -> str:
    """Jeton configuré (API_TOKEN), ou créé une fois et conservé sur le volume."""
    global _generated
    if settings.api_token:
        return settings.api_token
    if _generated is None:
        path = settings.api_token_file
        stored = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        if not stored:
            stored = secrets.token_urlsafe(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(stored + "\n", encoding="utf-8")
            path.chmod(0o600)
            log.warning("API_TOKEN absent : jeton généré dans %s", path)
        _generated = stored
    return _generated


def session_value() -> str:
    """Valeur du cookie : dérivée du jeton, qui n'est donc jamais stocké tel quel."""
    return hmac.new(token().encode(), b"session", hashlib.sha256).hexdigest()


def check_token(given: str) -> bool:
    return hmac.compare_digest(given.encode(), token().encode())


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in settings.allowed_hosts.split(",") if h.strip()}


def _hostname(value: str) -> str:
    """« localhost:8100 » → « localhost » ; « [::1]:8100 » → « ::1 »."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else ""
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def check_local(request: Request) -> None:
    """Refuse ce qui ne vient pas d'une page de l'application ouverte sur ce poste."""
    hosts = allowed_hosts()
    if _hostname(request.headers.get("host", "")) not in hosts:
        raise HTTPException(403, "nom d'hôte non autorisé (ALLOWED_HOSTS)")

    site = request.headers.get("sec-fetch-site", "")
    navigation = (request.method == "GET"
                  and request.headers.get("sec-fetch-mode") == "navigate")
    if site == "cross-site" and not navigation:
        raise HTTPException(403, "requête venant d'un autre site refusée")

    origin = request.headers.get("origin")
    if origin is not None and request.method not in SAFE_METHODS:
        scheme, _, rest = origin.partition("://")
        if scheme not in ("http", "https") or _hostname(rest.split("/")[0]) not in hosts:
            raise HTTPException(403, "requête venant d'un autre site refusée")


def require(request: Request) -> None:
    """Dépendance appliquée à toutes les routes de l'application."""
    check_local(request)
    if not settings.require_token or request.url.path in PUBLIC_PATHS:
        return
    scheme, _, given = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and check_token(given.strip()):
        return
    cookie = request.cookies.get(COOKIE, "")
    if cookie and hmac.compare_digest(cookie.encode(), session_value().encode()):
        return
    raise HTTPException(401, "authentification requise", headers={"WWW-Authenticate": "Bearer"})
