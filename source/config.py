"""Configuration centralisée, lue depuis l'environnement / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Transcription ---
    whisper_model: str = "large-v3"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    language: str | None = "fr"
    vad_filter: bool = True
    beam_size: int = 5

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

    # --- Images explicatives ---
    enable_ocr: bool = False
    frame_interval: int = 2
    scene_threshold: float = 18.0

    # --- Services ---
    database_url: str = "postgresql://transcription:transcription@postgres:5432/transcription"
    redis_url: str = "redis://redis:6379/0"

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
