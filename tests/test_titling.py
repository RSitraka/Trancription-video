"""Titre déduit du contenu quand le nom du fichier ne dit rien."""

from pathlib import Path

import pytest

from source import titling
from source.transcribe import Segment


def parle(*phrases):
    return [Segment(i * 5, i * 5 + 5, text) for i, text in enumerate(phrases)]


@pytest.mark.parametrize("name", [
    "VID_20260918_141233", "IMG_0042", "MVI_1234", "DSC00019", "GX010123",
    "PXL_20260918_141233456", "WhatsApp Video 2026-09-18 at 14.12.33",
    "Zoom_0", "enregistrement (2)", "Nouveau fichier", "untitled", "Sans titre",
    "20260918_1412", "a1b2c3d4e5f6", "video", "VIDEO", "clip", "1", "tmp",
    "Screen Recording 2026-09-18", "copie de 12345678", "final",
])
def test_vague_names(name):
    assert titling.is_vague(name)


@pytest.mark.parametrize("name", [
    "Comment créer un agent IA", "Réunion budget 2026", "cours-python-partie1",
    "Formation Docker", "Entretien Dupont", "soutenance_these_marie",
    "5 Automatisations IA à Implémenter", "Conseil municipal du 12 mars",
])
def test_meaningful_names_are_kept(name):
    assert not titling.is_vague(name)
    assert titling.choose(Path(f"/media/{name}.mp4")) == name        # gardé tel quel


def test_title_from_announcement():
    segments = parle(
        "Salut à tous et bienvenue sur la chaîne.",
        "Dans cette vidéo, on va voir comment créer un agent IA avec n8n.",
        "On commencera par l'installation.",
    )
    assert titling.from_segments(segments) == "Créer un agent IA avec n8n"


@pytest.mark.parametrize("phrase, attendu", [
    ("Aujourd'hui, je vais vous montrer comment sauvegarder une base de données",
     "Sauvegarder une base de données"),
    ("In this video we are going to build a small weather station with Arduino",
     "Build a small weather station with Arduino"),
    ("Il s'agit ici de préparer le budget prévisionnel de l'association",
     "Préparer le budget prévisionnel de l'association"),
])
def test_other_announcements(phrase, attendu):
    assert titling.from_segments(parle(phrase)) == attendu


def test_title_from_keywords_without_announcement():
    segments = parle(
        "Bon, le budget de la commune reste le point difficile.",
        "Le budget doit financer la cantine, et la cantine coûte cher.",
        "La commune votera le budget pour la cantine la semaine prochaine.",
    )
    title = titling.from_segments(segments)
    assert "budget" in title.lower() and "cantine" in title.lower()
    assert len(title) <= titling.MAX_LENGTH


def test_no_title_without_speech():
    assert titling.from_segments([]) == ""
    assert titling.from_segments(parle("   ")) == ""


def test_vague_name_without_speech_keeps_the_file_name():
    """Une vidéo muette garde son nom : mieux qu'un titre inventé."""
    assert titling.choose(Path("/media/VID_20260918.mp4"), []) == "VID_20260918"


def test_choose_prefers_content_for_vague_names():
    segments = parle("Dans cette vidéo, on va voir comment installer Docker sous Windows.")
    assert titling.choose(Path("/media/VID_20260918.mp4"), segments) == \
        "Installer Docker sous Windows"


@pytest.mark.parametrize("brut, propre", [
    ('re:union du 12/03 <urgent>', "Re union du 12 03 urgent"),
    ("  plusieurs   espaces  ", "Plusieurs espaces"),
    ("titre finissant par un point.", "Titre finissant par un point"),
    ("x" * 90, "X" + "x" * 59),
    ("", ""),
    ("///", ""),
])
def test_clean(brut, propre):
    assert titling.clean(brut) == propre


def test_file_name_keeps_its_case():
    assert titling.choose(Path("/media/cours-python-partie1.mp4")) == "cours-python-partie1"


def test_clean_cuts_on_a_word():
    long = "comment préparer une sauvegarde complète de la base de données du service"
    title = titling.clean(long)
    assert len(title) <= titling.MAX_LENGTH and not title.endswith(" ")
    assert long.lower().startswith(title.lower())      # coupé, jamais déformé


def test_title_is_usable_as_a_folder_name():
    segments = parle("Dans cette vidéo on va voir comment gérer C:/dossiers ? *étranges*")
    title = titling.choose(Path("/media/VID_1.mp4"), segments)
    assert not set(title) & set('<>:"/\\|?*')
    assert title == title.strip() and not title.endswith(".")


# --- Titre lu à l'écran (OCR) ------------------------------------------------

def ecran(*lignes):
    """Lignes lues sur une image : (texte, hauteur relative des caractères)."""
    return [(texte, hauteur) for texte, hauteur in lignes]


@pytest.fixture
def images(monkeypatch):
    """Remplace l'extraction d'images et l'OCR : une liste de lectures."""
    def poser(*lectures):
        pages = list(lectures)
        monkeypatch.setattr(titling.subprocess, "run", lambda *a, **k: None)
        monkeypatch.setattr(titling, "_read_image", lambda image: pages.pop(0) if pages else [])
    return poser


def test_screen_title_is_the_biggest_text(images):
    """Le plus gros texte fait le titre, pas la phrase la plus longue."""
    images(ecran(
        ("This is a simple website for practicing the basics of JavaScript", 0.02),
        ("Complete JavaScript Course", 0.09),
        ("supersimple.dev", 0.03),
    ))
    assert titling.from_screen(Path("/media/x.mp4"), 600) == "Complete JavaScript Course"


def test_screen_title_ignores_browser_and_short_noise(images):
    images(ecran(("Console Elements Network", 0.12), ("ab cd", 0.11),
                 ("Budget prévisionnel 2026", 0.08)))
    assert titling.from_screen(Path("/media/x.mp4"), 600) == "Budget prévisionnel 2026"


def test_screen_title_drops_ocr_artefacts_at_the_edges(images):
    """« lia », « JS » d'un logo : bruit collé au titre."""
    images(ecran(("lia JavaScript Course —", 0.10)))
    assert titling.from_screen(Path("/media/x.mp4"), 600) == "JavaScript Course"


def test_screen_falls_back_to_keywords(images):
    """Aucune ligne assez longue pour faire un titre : les mots répétés servent."""
    images(ecran(("budget", 0.05), ("cantine", 0.05)),
           ecran(("cantine", 0.05), ("budget", 0.04)))
    assert titling.from_screen(Path("/media/x.mp4"), 600) == "Budget, cantine"


def test_screen_without_readable_text(images):
    images(ecran(("", 0.1)))
    assert titling.from_screen(Path("/media/x.mp4"), 600) == ""


def test_metadata_title_wins_over_the_file_name(monkeypatch):
    monkeypatch.setattr(titling, "from_metadata", lambda source: "Conseil du 12 mars")
    assert titling.choose(Path("/media/VID_2026.mp4")) == "Conseil du 12 mars"


def test_metadata_title_ignored_when_vague(monkeypatch, tmp_path):
    sortie = type("R", (), {"stdout": '{"format": {"tags": {"title": "videoplayback"}}}'})
    monkeypatch.setattr(titling.subprocess, "run", lambda *a, **k: sortie)
    assert titling.from_metadata(tmp_path / "x.mp4") == ""


def test_screen_is_the_last_resort(monkeypatch):
    """L'OCR ne tourne que si le nom, les métadonnées et la parole échouent."""
    appels = []
    monkeypatch.setattr(titling, "from_metadata", lambda source: "")
    monkeypatch.setattr(titling, "from_screen", lambda v, d=0: appels.append(v) or "Vu à l'écran")

    segments = parle("Dans cette vidéo, on va voir comment installer Docker sous Windows.")
    assert titling.choose(Path("/media/VID_1.mp4"), segments, video=Path("/media/VID_1.mp4")) \
        == "Installer Docker sous Windows"
    assert appels == []                                    # parole suffisante : pas d'OCR

    assert titling.choose(Path("/media/VID_1.mp4"), [], video=Path("/media/VID_1.mp4")) \
        == "Vu à l'écran"
    assert appels == [Path("/media/VID_1.mp4")]
