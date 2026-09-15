"""Persistance des jobs : état, progression, reprise après redémarrage."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from source.config import settings

engine = create_engine(settings.sqlalchemy_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String(512))
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)  # 500 Go > 2^31

    # pending | uploading | queued | extracting | transcribing | compressing | done | failed
    status: Mapped[str] = mapped_column(String(32), default="pending")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    mode: Mapped[str] = mapped_column(String(16), default="transcribe")
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    outputs: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    indexed: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    parts: Mapped[list["Part"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "filename": self.filename,
            "size": self.size,
            "status": self.status,
            "progress": round(self.progress, 3),
            "mode": self.mode,
            "outputs": self.outputs or [],
            "indexed": self.indexed,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Part(Base):
    """Morceau d'upload reçu — permet de reprendre un transfert interrompu."""

    __tablename__ = "parts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer)
    size: Mapped[int] = mapped_column(BigInteger, default=0)

    job: Mapped[Job] = relationship(back_populates="parts")


def init_db() -> None:
    Base.metadata.create_all(engine)
    # Ajout de colonne sur une base existante, sans outil de migration.
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS task_id VARCHAR(64)")
        )
        connection.execute(
            text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS indexed VARCHAR(256)")
        )
