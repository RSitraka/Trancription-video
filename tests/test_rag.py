"""Indexation et recherche Qdrant, avec client et modèle d'embedding simulés."""

import json
import uuid
from types import SimpleNamespace

import numpy as np
import pytest

from source import rag
from source.config import settings


class FakeEmbedder:
    def __init__(self):
        self.texts = []

    def embed(self, texts):
        self.texts.extend(texts)
        return (np.array([len(t), 1.0, 0.0]) for t in texts)


class FakeQdrant:
    def __init__(self, exists=False, pages=None):
        self.exists = exists
        self.created = []
        self.upserts = []
        self.queries = []
        self.pages = pages or []

    def collection_exists(self, name):
        return self.exists

    def create_collection(self, name, vectors_config):
        self.created.append((name, vectors_config))
        self.exists = True

    def upsert(self, name, points):
        self.upserts.append((name, points))

    def query_points(self, name, query, limit, query_filter):
        self.queries.append((name, query, limit, query_filter))
        hit = SimpleNamespace(score=0.876543, payload={
            "timecode": "00:01:00.000", "start": 60, "end": 70, "source": "cours",
            "text": "réponse"})
        return SimpleNamespace(points=[hit])

    def scroll(self, name, limit, offset, with_payload):
        points, next_offset = self.pages[offset or 0]
        return [SimpleNamespace(payload=p) for p in points], next_offset


@pytest.fixture
def fakes(monkeypatch):
    client, embedder = FakeQdrant(), FakeEmbedder()
    monkeypatch.setattr(rag, "_client", lambda: client)
    monkeypatch.setattr(rag, "_embedder", lambda: embedder)
    monkeypatch.setattr(settings, "embedding_model", "intfloat/multilingual-e5-large")
    return SimpleNamespace(client=client, embedder=embedder)


def jsonl(tmp_path, records):
    path = tmp_path / "cours.rag.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n\n", encoding="utf-8")
    return path


def test_read_passages_skips_blank_lines(tmp_path):
    path = jsonl(tmp_path, [{"id": "a"}, {"id": "b"}])
    assert [p["id"] for p in rag.read_passages(path)] == ["a", "b"]


def test_index_creates_collection_and_upserts_in_batches(tmp_path, fakes):
    records = [{"id": f"cours#{i:04d}", "text": f"passage {i}", "source": "cours"}
               for i in range(5)]
    assert rag.index(jsonl(tmp_path, records), collection="test", batch=2) == 5

    [(name, config)] = fakes.client.created
    assert name == "test" and config.size == 3
    assert [len(points) for _, points in fakes.client.upserts] == [2, 2, 1]
    assert all(t.startswith("passage: ") for t in fakes.embedder.texts)
    first = fakes.client.upserts[0][1][0]
    assert first.id == str(uuid.uuid5(uuid.NAMESPACE_URL, "cours#0000"))
    assert first.payload == records[0]


def test_reindex_is_idempotent(tmp_path, fakes):
    path = jsonl(tmp_path, [{"id": "cours#0000", "text": "x"}])
    rag.index(path)
    rag.index(path)
    ids = [points[0].id for _, points in fakes.client.upserts]
    assert ids[0] == ids[1] and len(fakes.client.created) == 1


def test_index_empty_file(tmp_path, fakes):
    path = tmp_path / "vide.rag.jsonl"
    path.write_text("")
    assert rag.index(path) == 0 and not fakes.client.upserts


def test_search_prefix_filter_and_mapping(fakes, monkeypatch):
    [hit] = rag.search("comment créer un agent", limit=3, source="cours")
    name, _, limit, condition = fakes.client.queries[0]
    assert fakes.embedder.texts == ["query: comment créer un agent"]
    assert name == settings.qdrant_collection and limit == 3
    assert condition.must[0].key == "source" and condition.must[0].match.value == "cours"
    assert hit == {"score": 0.8765, "timecode": "00:01:00.000", "start": 60,
                   "source": "cours", "end": 70, "text": "réponse", "frames": [],
                   "screen_text": []}

    monkeypatch.setattr(settings, "embedding_model", "BAAI/bge-small-en")
    rag.search("sans prefixe")
    assert fakes.embedder.texts[-1] == "sans prefixe"
    assert fakes.client.queries[-1][3] is None


def test_sources_paginates(fakes):
    fakes.client.exists = True
    fakes.client.pages = {0: ([{"source": "b"}, {"source": "a"}], 2),
                          2: ([{"source": "a"}, {}], None)}
    assert rag.sources() == ["a", "b"]


def test_sources_without_collection(fakes):
    assert rag.sources() == []
