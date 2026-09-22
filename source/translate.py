"""Traduction des sous-titres (M2M100, 100 langues, dans tous les sens).

Whisper ne sait traduire que **vers l'anglais**. Pour une vidéo anglaise
sous-titrée en français, la transcription est faite dans la langue parlée,
puis chaque segment est traduit ici, avec ses horodatages inchangés.

Modèle : M2M100 1,2 milliard de paramètres de Meta, **licence MIT** (usage
commercial permis — contrairement à NLLB-200, en CC-BY-NC). Version CTranslate2
quantifiée : le même moteur que faster-whisper, sur GPU ou CPU. Environ 1,2 Go,
téléchargé une fois dans le volume « models ».

Mesuré sur RTX 3060 : 4 phrases en ~1 s, première phrase comprise.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Callable, Sequence

from source.config import settings

log = logging.getLogger(__name__)

Progress = Callable[[float], None] | None
BATCH = 24

_MODEL = None

# Langues que M2M100 et Whisper désignent par le même code à deux lettres.
LANGUAGES = {
    "af", "am", "ar", "ast", "az", "ba", "be", "bg", "bn", "br", "bs", "ca", "ceb", "cs",
    "cy", "da", "de", "el", "en", "es", "et", "fa", "ff", "fi", "fr", "fy", "ga", "gd",
    "gl", "gu", "ha", "he", "hi", "hr", "ht", "hu", "hy", "id", "ig", "ilo", "is", "it",
    "ja", "jv", "ka", "kk", "km", "kn", "ko", "lb", "lg", "ln", "lo", "lt", "lv", "mg",
    "mk", "ml", "mn", "mr", "ms", "my", "ne", "nl", "no", "ns", "oc", "or", "pa", "pl",
    "ps", "pt", "ro", "ru", "sd", "si", "sk", "sl", "so", "sq", "sr", "ss", "su", "sv",
    "sw", "ta", "th", "tl", "tn", "tr", "uk", "ur", "uz", "vi", "wo", "xh", "yi", "yo",
    "zh", "zu",
}


class TranslationError(RuntimeError):
    """Traduction impossible, avec un message compréhensible."""


def _load():
    """Modèle et découpeur chargés une fois par processus."""
    global _MODEL
    if _MODEL is None:
        import ctranslate2
        import sentencepiece
        from huggingface_hub import snapshot_download

        folder = snapshot_download(settings.translation_model,
                                   cache_dir=str(settings.translation_cache_dir))
        on_gpu = settings.whisper_device == "cuda"
        translator = ctranslate2.Translator(
            folder, device="cuda" if on_gpu else "cpu",
            compute_type="int8_float16" if on_gpu else "int8")
        pieces = sentencepiece.SentencePieceProcessor(
            model_file=f"{folder}/sentencepiece.bpe.model")
        _MODEL = (translator, pieces)
        log.info("modèle de traduction chargé : %s", settings.translation_model)
    return _MODEL


def translate_texts(texts: Sequence[str], source: str, target: str,
                    on_progress: Progress = None) -> list[str]:
    """Traduit une liste de phrases, par lots. L'ordre est conservé."""
    for code in (source, target):
        if code not in LANGUAGES:
            raise TranslationError(f"langue non prise en charge pour la traduction : {code}")
    if source == target or not texts:
        return list(texts)

    translator, pieces = _load()
    translated: list[str] = []
    for start in range(0, len(texts), BATCH):
        batch = texts[start:start + BATCH]
        tokens = [[f"__{source}__", *pieces.encode(t, out_type=str), "</s>"] for t in batch]
        results = translator.translate_batch(
            tokens, target_prefix=[[f"__{target}__"]] * len(batch),
            beam_size=4, max_decoding_length=512)
        for result in results:
            words = [w for w in result.hypotheses[0] if w != f"__{target}__"]
            translated.append(pieces.decode(words).strip())
        if on_progress:
            on_progress(min((start + len(batch)) / len(texts), 1.0))
    return translated


def translate_segments(segments: list, source: str, target: str,
                       on_progress: Progress = None) -> list:
    """Mêmes segments, mêmes horodatages, texte traduit."""
    texts = translate_texts([s.text for s in segments], source, target, on_progress)
    return [replace(s, text=text or s.text) for s, text in zip(segments, texts)]
