"""Indexation et recherche vectorielle dans Qdrant.

Le fichier `.rag.jsonl` produit par l'export sert d'entrée : une ligne = un
passage horodaté. Chaque passage devient un point Qdrant dont la charge utile
conserve le timecode, ce qui permet à une réponse de citer la minute exacte de
la vidéo.
"""

from __future__ import annotations

import json
import logging
import uuid
from functools import cache
from pathlib import Path
from typing import Iterator

from source.config import settings

log = logging.getLogger(__name__)

# e5 attend des préfixes explicites : sans eux, la qualité chute nettement.
E5_PASSAGE = "passage: "
E5_QUERY = "query: "


@cache
def _client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=settings.qdrant_url, timeout=120)


def _is_e5() -> bool:
    return "e5" in settings.embedding_model.lower()


# Charger le modèle (~2 Go) coûte plusieurs secondes : une seule fois par processus.
@cache
def _embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=settings.embedding_model)


def read_passages(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def index(path: Path, collection: str | None = None, batch: int = 64) -> int:
    """Indexe un fichier .rag.jsonl. Renvoie le nombre de passages écrits."""
    from qdrant_client.models import Distance, PointStruct, VectorParams

    collection = collection or settings.qdrant_collection
    client, embedder = _client(), _embedder()
    passages = list(read_passages(path))
    if not passages:
        log.warning("%s est vide", path)
        return 0

    texts = [(E5_PASSAGE if _is_e5() else "") + p["text"] for p in passages]
    log.info("Calcul des vecteurs pour %d passages (%s)…",
             len(texts), settings.embedding_model)
    vectors = list(embedder.embed(texts))
    size = len(vectors[0])

    if not client.collection_exists(collection):
        client.create_collection(
            collection,
            vectors_config=VectorParams(size=size, distance=Distance.COSINE),
        )
        log.info("Collection '%s' créée (%d dimensions)", collection, size)

    points = [
        PointStruct(
            # Identifiant déterministe : réindexer met à jour au lieu de dupliquer.
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, p["id"])),
            vector=vector.tolist(),
            payload=p,
        )
        for p, vector in zip(passages, vectors)
    ]
    for start in range(0, len(points), batch):
        client.upsert(collection, points=points[start:start + batch])

    log.info("%d passages indexés dans '%s'", len(points), collection)
    return len(points)


def search(
    question: str,
    limit: int = 5,
    collection: str | None = None,
    source: str | None = None,
) -> list[dict]:
    """Recherche sémantique. Renvoie les passages avec leur timecode.

    `source` restreint la recherche à une vidéo : la collection est partagée
    entre toutes les transcriptions.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    collection = collection or settings.qdrant_collection
    client, embedder = _client(), _embedder()
    query = (E5_QUERY if _is_e5() else "") + question
    vector = next(iter(embedder.embed([query]))).tolist()

    condition = None
    if source:
        condition = Filter(
            must=[FieldCondition(key="source", match=MatchValue(value=source))]
        )

    hits = client.query_points(
        collection, query=vector, limit=limit, query_filter=condition
    ).points
    return [
        {
            "score": round(hit.score, 4),
            "timecode": hit.payload.get("timecode"),
            "start": hit.payload.get("start"),
            "source": hit.payload.get("source"),
            "end": hit.payload.get("end"),
            "text": hit.payload.get("text", ""),
            "frames": hit.payload.get("frames", []),
            "screen_text": hit.payload.get("screen_text", []),
        }
        for hit in hits
    ]


def sources(collection: str | None = None) -> list[str]:
    """Liste les vidéos présentes dans la collection."""
    client = _client()
    collection = collection or settings.qdrant_collection
    if not client.collection_exists(collection):
        return []

    found, offset = set(), None
    while True:
        points, offset = client.scroll(
            collection, limit=256, offset=offset, with_payload=["source"]
        )
        found.update(p.payload.get("source", "") for p in points)
        if offset is None:
            break
    return sorted(s for s in found if s)
