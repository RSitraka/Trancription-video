"""Traduction des sous-titres : modèle simulé, horodatages conservés."""

from types import SimpleNamespace

import pytest

from source import translate
from source.transcribe import Segment


class FauxDecoupeur:
    def encode(self, texte, out_type=str):
        return texte.split()

    def decode(self, mots):
        return " ".join(mots)


class FauxTraducteur:
    """« traduit » en préfixant chaque mot par la langue cible."""

    def __init__(self):
        self.lots = []

    def translate_batch(self, lots, target_prefix, beam_size, max_decoding_length):
        self.lots.append((lots, target_prefix))
        cible = target_prefix[0][0]
        return [SimpleNamespace(hypotheses=[[cible, *[f"{cible[2:4]}:{m}" for m in lot[1:-1]]]])
                for lot in lots]


@pytest.fixture
def modele(monkeypatch):
    traducteur = FauxTraducteur()
    monkeypatch.setattr(translate, "_MODEL", (traducteur, FauxDecoupeur()))
    return traducteur


def test_translate_texts_frames_language_tokens(modele):
    assert translate.translate_texts(["hello world"], "en", "fr") == ["fr:hello fr:world"]
    [(lots, prefixes)] = modele.lots
    assert lots == [["__en__", "hello", "world", "</s>"]] and prefixes == [["__fr__"]]


def test_same_language_is_untouched(modele):
    assert translate.translate_texts(["bonjour"], "fr", "fr") == ["bonjour"]
    assert modele.lots == []


def test_batches_and_progress(modele, monkeypatch):
    monkeypatch.setattr(translate, "BATCH", 2)
    progression = []
    textes = [f"phrase {i}" for i in range(5)]
    sortie = translate.translate_texts(textes, "en", "fr", progression.append)
    assert len(sortie) == 5 and sortie[4] == "fr:phrase fr:4"         # ordre conservé
    assert len(modele.lots) == 3 and progression == [0.4, 0.8, 1.0]


def test_unsupported_language(modele):
    with pytest.raises(translate.TranslationError, match="non prise en charge"):
        translate.translate_texts(["x"], "en", "klingon")


def test_segments_keep_their_timecodes(modele):
    segments = [Segment(1.5, 3.25, "hello", -0.2, ["écran"], "f.jpg", "en")]
    [traduit] = translate.translate_segments(segments, "en", "fr")
    assert (traduit.start, traduit.end, traduit.ocr, traduit.frame) == (1.5, 3.25, ["écran"], "f.jpg")
    assert traduit.text == "fr:hello"
    assert segments[0].text == "hello"                                # original intact


def test_default_model_is_mit_licensed():
    """NLLB-200 (CC-BY-NC) interdirait un usage commercial : M2M100 est sous MIT."""
    from source.config import Settings

    assert "m2m100" in Settings.model_fields["translation_model"].default
    assert str(Settings.model_fields["translation_cache_dir"].default).startswith("/models/")
