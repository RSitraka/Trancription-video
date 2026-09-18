"""Configuration centralisée, lue depuis l'environnement / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Transcription ---
    # large-v3-turbo : ~6x plus rapide que large-v3, qualité très proche en fr/en.
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    language: str | None = "fr"
    vad_filter: bool = True
    beam_size: int = 5
    # Passages transcrits en parallèle sur la carte (0 = un par un). Sur GPU,
    # 16 remplit une carte de 12 Go et divise encore le temps par ~3.
    whisper_batch_size: int = 0

    # --- Découpage ---
    chunk_duration: int = 1800
    chunk_overlap: int = 2
    delete_chunks_after: bool = True

    # --- Chemins ---
    media_dir: Path = Path("/media")
    work_dir: Path = Path("/data/tmp")
    output_dir: Path = Path("/data/out")

    # --- Compression ---
    target_size_gb: float | None = None
    video_codec: str = "libx265"
    crf: int = 24
    preset: str = "medium"
    audio_bitrate: str = "128k"
    # Débit total (kbps) sous lequel une vidéo est découpée en parties plutôt
    # que compressée en un seul fichier : 1000 kbps ≈ 720p lisible.
    split_min_kbps: int = 1000
    # Encodages NVENC simultanés pour un même fichier (RTX grand public : 8 max).
    encode_jobs: int = 4

    # --- Export RAG ---
    rag_chunk_chars: int = 1200        # ~300 mots : taille de passage usuelle
    rag_overlap_segments: int = 1      # recouvrement pour ne pas couper une idée

    # --- Images explicatives ---
    enable_ocr: bool = False
    frame_interval: int = 2
    scene_threshold: float = 18.0

    # --- Accès à l'API ---
    # Mode local (défaut) : pas de connexion, mais seuls les noms d'hôte ci-dessous
    # sont servis et les requêtes venant d'un autre site web sont refusées.
    # REQUIRE_TOKEN=true : jeton exigé en plus — indispensable si l'API est
    # ouverte au réseau (BIND_ADDRESS=0.0.0.0).
    require_token: bool = False
    allowed_hosts: str = "localhost,127.0.0.1,::1"
    # Jeton du mode REQUIRE_TOKEN. Vide : un jeton aléatoire est créé une fois
    # dans `api_token_file` et journalisé.
    api_token: str | None = None
    api_token_file: Path = Path("/data/api_token")

    # --- Upload ---
    upload_part_max_mb: int = 128          # l'interface envoie des morceaux de 64 Mo
    upload_free_margin_mb: int = 1024      # espace disque laissé libre après un upload

    # --- Services ---
    # Sans mot de passe par défaut : il vient de POSTGRES_PASSWORD (docker-compose.yml).
    database_url: str = "postgresql://transcription@postgres:5432/transcription"
    redis_url: str = "redis://redis:6379/0"
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "transcriptions"
    embedding_model: str = "intfloat/multilingual-e5-large"

    @property
    def sqlalchemy_url(self) -> str:
        """SQLAlchemy exige le driver explicite ; psycopg3 ici."""
        url = self.database_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url

    def ensure_dirs(self) -> None:
        for path in (self.work_dir, self.output_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
