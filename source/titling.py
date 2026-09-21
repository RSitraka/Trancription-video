"""Titre d'une vidéo dont le nom de fichier ne dit rien.

« VID_20260918_141233.mp4 », « WhatsApp Video 2026-09-18 at 14.12.33.mp4 » ou
« enregistrement (2).mp4 » ne donnent aucune idée du contenu : le dossier de
sortie, les sous-titres et les parties compressées héritent pourtant de ce nom.

Quand le nom est jugé vague, le titre est cherché dans l'ordre :

1. les **métadonnées** du fichier (balise « title ») ;
2. la **parole** : phrase d'annonce (« dans cette vidéo, on va voir comment… »),
   sinon les mots les plus présents au début ;
3. le **texte affiché** à l'écran, lu par OCR sur quelques images — seul recours
   pour une vidéo sans son (flux vidéo seul, écran filmé, diaporama muet) ;
4. à défaut, le nom du fichier, jamais un titre inventé.

Un nom de fichier déjà parlant est gardé tel quel : c'est l'utilisateur qui l'a
choisi.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
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


# Instants où chercher un titre à l'écran : générique de début, puis quelques
# points répartis. Les premières images portent souvent le titre.
FRAME_TIMES = (3, 12, 45, 150, 420, 1200)
FRAME_SHARE = (0.05, 0.25, 0.5)
# Lignes d'OCR sans intérêt : interface, navigateur, horloges.
OCR_NOISE = re.compile(
    r"https?://|www\.|\b(?:console|elements|network|performance|memory|sources"
    r"|fichier|edition|affichage|onglet|signets|outils|aide|file|edit|view|help"
    r"|search|recherche|menu|localhost|copyright)\b", re.IGNORECASE)
# Petits mots gardés en tête ou en fin de ligne : tout le reste (« lia », « JS »
# lu sur un logo) est du bruit d'OCR.
SMALL_WORDS = {"de", "du", "des", "la", "le", "les", "un", "une", "et", "en", "à", "au",
               "aux", "sur", "pour", "avec", "the", "a", "an", "of", "to", "for", "in",
               "on", "and", "with", "ia", "ai"}


def from_metadata(source: Path) -> str:
    """Balise « title » du fichier, quand l'outil d'export l'a écrite."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=title",
             "-of", "json", str(source)],
            capture_output=True, text=True, timeout=60, check=True).stdout
        tag = json.loads(out).get("format", {}).get("tags", {}).get("title", "")
    except Exception as error:                      # noqa: BLE001 - simple repli
        log.debug("métadonnées illisibles : %s", error)
        return ""
    return "" if is_vague(tag) else clean(tag, keep_case=True)


def _trim_edges(line: str) -> str:
    """Retire les jetons douteux aux extrémités d'une ligne lue par OCR :
    « lia JavaScript Course — » → « JavaScript Course »."""
    tokens = [t for t in re.split(r"\s+", line) if t]
    keep = lambda t: (len(re.sub(r"[^^\w]", "", t, flags=re.UNICODE)) >= 4
                      or t.lower() in SMALL_WORDS or t.isdigit())
    while tokens and not keep(tokens[0]):
        tokens.pop(0)
    while tokens and not keep(tokens[-1]):
        tokens.pop()
    return " ".join(tokens)


def _ocr_candidate(lines: list[tuple[str, float, int]]) -> str:
    """Meilleure ligne parmi celles lues à l'écran.

    Le critère principal est la **hauteur des caractères** : sur une capture,
    le plus gros texte est presque toujours le titre, alors que la ligne la
    plus longue n'est qu'une phrase de texte courant."""
    best, best_score = "", 0.0
    for text, height, rank in lines:
        line = _trim_edges(re.sub(r"[^\w\s'’,:&+.-]", " ", text, flags=re.UNICODE))
        line = re.sub(r"\s+", " ", line).strip(" .,:;-")
        if not 10 <= len(line) <= 70 or OCR_NOISE.search(line):
            continue
        words = [w for w in line.split() if len(w) >= 3]
        letters = sum(c.isalpha() or c.isspace() for c in line) / len(line)
        if len(words) < 2 or letters < 0.8:
            continue
        # Hauteur relative des caractères, puis mise en forme de titre.
        score = height * 100
        score += 3 if line.istitle() or line.isupper() else 0
        score += 2 if rank == 0 else 0                # image du générique
        score -= 3 if len(line.split()) > 8 else 0    # phrase, pas un titre
        if score > best_score:
            best, best_score = line, score
    return clean(best, keep_case=True)


def from_screen(video: Path, duration: float = 0.0) -> str:
    """Titre lu à l'écran : quelques images extraites, puis OCR.

    Dernier recours, pour les vidéos sans parole exploitable : cours filmés,
    diaporamas, captures d'écran."""
    times = [t for t in FRAME_TIMES if not duration or t < duration]
    times += [round(duration * share) for share in FRAME_SHARE if duration]
    if not times:
        return ""

    lines: list[tuple[str, float, int]] = []
    words: list[str] = []
    with tempfile.TemporaryDirectory(prefix="titre-") as work:
        for rank, moment in enumerate(sorted(set(times))[:8]):
            image = Path(work) / f"{moment}.jpg"
            try:
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-ss", str(moment), "-i", str(video), "-frames:v", "1",
                     "-vf", "scale=1280:-2", str(image)],
                    capture_output=True, timeout=180, check=True)
                read = _read_image(image)
            except Exception as error:              # noqa: BLE001 - image sautée
                log.debug("image %s s illisible : %s", moment, error)
                continue
            lines += [(text, height, rank) for text, height in read]
            words += [text for text, _ in read]

    return _ocr_candidate(lines) or clean(_keywords(" ".join(words)))


def _read_image(image: Path) -> list[tuple[str, float]]:
    """Lignes lues, avec la hauteur des caractères rapportée à celle de l'image."""
    import pytesseract
    from PIL import Image

    picture = Image.open(image)
    data = pytesseract.image_to_data(picture, lang="fra+eng",
                                     output_type=pytesseract.Output.DICT)
    grouped: dict[tuple, list[tuple[str, int]]] = {}
    for i, word in enumerate(data["text"]):
        if word.strip() and int(data["conf"][i]) >= 45:
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            grouped.setdefault(key, []).append((word, data["height"][i]))
    return [(" ".join(w for w, _ in line),
             sum(h for _, h in line) / len(line) / picture.height)
            for line in grouped.values()]


def choose(source: Path, segments: Iterable = (), video: Path | None = None,
           duration: float = 0.0) -> str:
    """Nom des fichiers produits : celui du fichier s'il parle, sinon un titre
    tiré du contenu. Toujours un nom non vide, utilisable tel quel.

    `video` autorise la lecture du texte à l'écran (OCR), utile quand il n'y a
    pas de parole ; c'est la seule étape coûteuse."""
    stem = source.stem
    if not is_vague(stem):
        return clean(stem, limit=120, keep_case=True) or "video"

    # Sources essayées dans l'ordre, et seulement si la précédente n'a rien
    # donné : lire le texte à l'écran coûte une extraction d'images et un OCR.
    sources = (
        ("métadonnées", lambda: from_metadata(source)),
        ("contenu parlé", lambda: from_segments(segments)),
        ("texte à l'écran", lambda: from_screen(video, duration) if video else ""),
    )
    for origin, chercher in sources:
        title = chercher()
        if title:
            log.info("Nom peu parlant « %s » : titre tiré des %s — « %s »",
                     stem, origin, title)
            return title
    return clean(stem, limit=120, keep_case=True) or "video"
