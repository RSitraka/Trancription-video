# syntax=docker/dockerfile:1.7
#
# Image unique pour l'API, les workers et la CLI.
#   GPU : BASE_IMAGE=nvidia/cuda:12.9.2-cudnn-runtime-ubuntu24.04  (défaut)
#   CPU : BASE_IMAGE=ubuntu:24.04
# Les deux bases sont des Ubuntu 24.04 : le corps du Dockerfile est identique.

ARG BASE_IMAGE=nvidia/cuda:12.9.2-cudnn-runtime-ubuntu24.04
FROM ${BASE_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH

# ffmpeg + ffprobe : coeur du pipeline média
# libgl1 / libglib2.0-0 : dépendances natives d'OpenCV
# tesseract : OCR des diapositives (ENABLE_OCR)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev \
        ffmpeg \
        libgl1 libglib2.0-0 \
        tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng \
        build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv && pip install --no-cache-dir --upgrade pip wheel

WORKDIR /app

# Couche dépendances séparée : le code change souvent, pas les libs
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

# Téléchargement par lien (source/download.py). Couche à part : yt-dlp suit les
# changements des sites et se met à jour souvent, sans toucher aux autres libs.
# curl-cffi : certains sites (Dailymotion…) n'acceptent qu'un navigateur imité.
# sentencepiece : découpage des phrases pour la traduction (source/translate.py).
RUN --mount=type=cache,target=/root/.cache/pip pip install "yt-dlp[default,curl-cffi]" sentencepiece

COPY . .

# Exécution sans privilèges ; /data et /models sont montés en volume
# Ubuntu 24.04 fournit déjà un compte "ubuntu" en UID 1000 : on le libère pour
# que l'UID du conteneur corresponde à celui de l'hôte sur les bind mounts.
RUN userdel -r ubuntu 2>/dev/null || true \
    && useradd -m -u 1000 app \
    && mkdir -p /data/tmp /data/out /models \
    && chown -R app:app /app /data /models
USER app

# Cache des modèles Whisper hors de l'image (sinon ~3 Go retéléchargés à chaque run)
ENV HF_HOME=/models \
    XDG_CACHE_HOME=/models

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "source.api:app", "--host", "0.0.0.0", "--port", "8000"]

# --- Tests -------------------------------------------------------------------
# Étape à part : pytest n'alourdit pas l'image de production. Les services
# api / worker / cli ciblent « runtime » ; `make test` construit celle-ci.
FROM runtime AS test
USER root
COPY requirements-dev.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements-dev.txt
USER app
CMD ["pytest"]
