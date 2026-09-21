"""Titre d'une vidéo dont le nom de fichier ne dit rien.

« VID_20260918_141233.mp4 », « WhatsApp Video 2026-09-18 at 14.12.33.mp4 » ou
« enregistrement (2).mp4 » ne donnent aucune idée du contenu : le dossier de
sortie, les sous-titres et les parties compressées héritent pourtant de ce nom.

Quand le nom est jugé vague, un titre est tiré de la **transcription** : la
phrase d'annonce si le locuteur en fait une (« dans cette vidéo, on va voir
comment… »), sinon les mots les plus présents au début. Un nom de fichier déjà
parlant est gardé tel quel : c'est l'utilisateur qui l'a choisi.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

MAX_LENGTH = 60
# Début de la transcription analysé : de quoi couvrir une introduction.
LEAD_CHARS = 8000

# Noms produits par les appareils et les applications : aucun rapport avec le
# contenu. Comparés sur le nom en minuscules, sans accents.
DEVICE_PREFIXES = (
    "vid", "video", "vid_", "img", "mov", "mvi", "dsc", "dji", "gx", "gopro", "gh",
    "pxl", "rec", "record", "recording", "enregistrement", "capture", "screencast",
    "screenrecord", "screen recording", "whatsapp", "telegram", "signal", "zoom",
    "teams", "meet", "movie", "clip", "sequence", "film", "camera", "cam",
    "untitled", "sans titre", "nouveau", "new", "temp", "tmp", "final", "output",
    "download", "telechargement", "copie de", "copy of", "duplicate",
)
VAGUE_EXACT = {"video", "audio", "film", "clip", "reunion", "meeting", "cours", "test"}

STOPWORDS = {
    # français
    "alors", "après", "aussi", "autre", "avant", "avec", "avoir", "beaucoup", "bien",
    "c'est", "cela", "cette", "ceux", "chaque", "chose", "comme", "d'un", "d'une",
    "dans", "déjà", "donc", "dont", "elle", "elles", "encore", "entre", "être",
    "faire", "fait", "faut", "gens", "ici", "j'ai", "juste", "leur", "leurs", "mais",
    "même", "merci", "mettre", "moins", "monde", "nous", "parce", "parler", "pass",
    "peut", "peux", "plus", "pour", "pourquoi", "pouvez", "prendre", "puis", "quand",
    "que", "quel", "quelle", "quelque", "qui", "quoi", "sans", "sera", "seulement",
    "sont", "sous", "super", "sur", "toujours", "tous", "tout", "toute", "toutes",
    "très", "trop", "vais", "vers", "veux", "voilà", "voir", "vont", "votre", "vous",
    "était", "êtes", "peu", "bon", "donne", "aller", "avez", "avait", "chez", "cas",
    # anglais
    "about", "actually", "after", "again", "also", "another", "because", "been",
    "before", "being", "between", "could", "doing", "done", "down", "each", "every",
    "from", "gonna", "going", "have", "here", "into", "just", "know", "like", "make",
    "many", "more", "most", "much", "need", "only", "other", "over", "really",
    "right", "same", "should", "some", "such", "than", "that", "their", "them",
    "then", "there", "these", "they", "thing", "things", "think", "this", "those",
    "through", "time", "using", "very", "want", "well", "what", "when", "where",
    "which", "while", "will", "with", "would", "your",
}

# Phrases d'annonce : ce qui suit décrit le sujet.
ANNOUNCEMENTS = re.compile(
    r"(?:dans cette (?:vid[ée]o|formation|s[ée]ance)"
    r"|aujourd['’]hui"
    r"|il s['’]agit (?:ici )?de"
    r"|le sujet (?:du jour|d['’]aujourd['’]hui) (?:c['’]est|est)"
    r"|bienvenue (?:dans|sur) (?:cette|ce)"
    r"|in this (?:video|tutorial|session)"
    r"|today"
    r"|on va|nous allons|je vais)"
    r"[\s,:]*(?P<sujet>[^.!?\n]{12,140})",
    re.IGNORECASE,
)
# Amorces sans intérêt en tête du sujet : pronoms, auxiliaires, verbes
# d'annonce (voir, montrer…) et articles. Les verbes d'action (créer,
# installer, build…) sont gardés : ce sont eux qui font le titre.
LEADING_FILLER = re.compile(
    r"^(?:"
    r"on|nous|je|j['’]|vous|tu|we|i|you"
    r"|va|vais|vas|allons|allez|vont|veux|voudrais|verra|verrons"
    r"|will|are|am|is|going to|gonna|['’]?ll|let['’]s"
    r"|voir|revoir|apprendre|d[ée]couvrir|montrer|expliquer|pr[ée]senter"
    r"|parler de|parler|aborder|traiter de|see|show you|show|learn|look at|talk about"
    r"|comment|how to|how|que|qu['’]|de|d['’]|[àa]|le|la|les|un|une|des|du"
    r"|the|to|ce|cette|ces|donc|ensemble|ici|maintenant"
    r")\b[\s,:']*",
    re.IGNORECASE,
)


def _strip_filler(text: str) -> str:
    """Retire les amorces successives : « on va voir comment créer… » → « créer… »."""
    previous = None
    while previous != text:
        previous = text
        text = LEADING_FILLER.sub("", text.strip(), count=1)
    return text


WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _plain(text: str) -> str:
    """Minuscules sans accents : pour comparer des noms, pas pour afficher."""
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower()


def is_vague(name: str) -> bool:
    """Le nom de fichier dit-il quelque chose du contenu ?"""
    plain = _plain(name).strip()
    letters = re.sub(r"[^a-z]", "", plain)
    if len(letters) < 4:                                   # « 20260918_1412 », « a1b2 »
        return True
    if plain in VAGUE_EXACT:
        return True
    # Nom majoritairement numérique : horodatage, compteur d'appareil.
    if len(re.sub(r"\D", "", plain)) >= len(plain.replace(" ", "")) * 0.5:
        return True
    if any(plain.startswith(prefix) for prefix in DEVICE_PREFIXES):
        return True
    # Identifiant : hexadécimal long, ou UUID.
    if re.fullmatch(r"[0-9a-f]{8,}(?:-[0-9a-f]{4,}){0,4}", plain):
        return True
    return False


def clean(title: str, limit: int = MAX_LENGTH, keep_case: bool = False) -> str:
    """Titre utilisable comme nom de dossier : sans caractère interdit, court.

    `keep_case` garde la casse d'origine : un nom de fichier choisi par
    l'utilisateur n'a pas à être retouché."""
    title = FORBIDDEN.sub(" ", title)
    title = re.sub(r"\s+", " ", title).strip(" -–—·,;:.'\"")
    if len(title) > limit:
        cut = title[:limit + 1]
        title = cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else title[:limit]
    title = title.rstrip(" .-_(").strip()
    if not title or keep_case:
        return title
    return title[:1].upper() + title[1:]


def _keywords(text: str, count: int = 5) -> str:
    """Mots les plus présents, dans l'ordre de leur première apparition."""
    words = [w.lower() for w in WORD.findall(text)]
    frequencies = Counter(w for w in words if w not in STOPWORDS)
    best = {word for word, times in frequencies.most_common(count) if times > 1}
    if len(best) < 2:
        return ""
    ordered, seen = [], set()
    for word in words:
        if word in best and word not in seen:
            seen.add(word)
            ordered.append(word)
    return ", ".join(ordered)


def from_segments(segments: Iterable) -> str:
    """Titre déduit de la parole : phrase d'annonce, sinon mots-clés."""
    lead = " ".join(getattr(s, "text", "") for s in segments)[:LEAD_CHARS].strip()
    if not lead:
        return ""

    match = ANNOUNCEMENTS.search(lead)
    if match:
        title = clean(_strip_filler(match["sujet"]))
        if len(title) >= 12:
            return title

    return clean(_keywords(lead))


def choose(source: Path, segments: Iterable = ()) -> str:
    """Nom des fichiers produits : celui du fichier s'il parle, sinon un titre
    tiré du contenu. Toujours un nom non vide, utilisable tel quel."""
    stem = source.stem
    if not is_vague(stem):
        return clean(stem, limit=120, keep_case=True) or "video"
    title = from_segments(segments)
    if title:
        log.info("Nom peu parlant « %s » : titre tiré du contenu « %s »", stem, title)
        return title
    return clean(stem, limit=120, keep_case=True) or "video"
