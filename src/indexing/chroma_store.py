"""Chroma 컬렉션 생성과 증분 동기화. 바뀐 레코드만 임베딩하고, 사라진 레코드는 삭제한다."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import chromadb
from chromadb.config import Settings

from src.indexing.ids import record_hash

Embedder = Callable[[list[str]], list[list[float]]]
EMBED_BATCH_SIZE = 100


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    total: int = 0
    dimensions: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def open_client(path: Path) -> chromadb.api.ClientAPI:
    return chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))


def get_collection(client, name: str, distance: str = "cosine"):
    # 임베딩은 항상 직접 계산해서 넘기므로 Chroma 기본 임베딩 함수(모델 다운로드)를 쓰지 않는다
    return client.get_or_create_collection(name, embedding_function=None, configuration={"hnsw": {"space": distance}})


def scalar_metadata(metadata: dict) -> dict:
    """Chroma 메타데이터는 str/int/float/bool만 안전하다. 리스트는 쉼표 문자열로, None은 빈 문자열로 바꾼다."""
    clean = {}
    for key, value in metadata.items():
        if isinstance(value, (list, tuple, set)):
            clean[key] = ",".join(str(v) for v in value)
        elif value is None:
            clean[key] = ""
        elif isinstance(value, (str, int, float, bool)):
            clean[key] = value
        else:
            clean[key] = str(value)
    return clean


def sync_collection(collection, records: list[dict], embed: Embedder | None, batch_size: int = EMBED_BATCH_SIZE) -> SyncResult:
    """records: [{"id", "text", "metadata"}]. embed가 None이면 바뀐 레코드가 있을 때 오류를 낸다."""
    existing = collection.get(include=["metadatas"])
    existing_hash = {i: (m or {}).get("record_hash") for i, m in zip(existing["ids"], existing["metadatas"])}

    prepared, changed = [], []
    for record in records:
        metadata = scalar_metadata(record["metadata"])
        metadata["record_hash"] = record_hash(record["text"], metadata)
        item = {"id": record["id"], "text": record["text"], "metadata": metadata}
        prepared.append(item)
        if existing_hash.get(item["id"]) != metadata["record_hash"]:
            changed.append(item)

    ids = [item["id"] for item in prepared]
    if len(ids) != len(set(ids)):
        raise ValueError("레코드 ID가 중복됩니다")

    result = SyncResult(total=len(prepared))
    if changed and embed is None:
        raise RuntimeError(f"임베딩이 필요한 레코드가 {len(changed)}건 있지만 임베딩 함수가 없습니다")
    for start in range(0, len(changed), batch_size):
        batch = changed[start:start + batch_size]
        vectors = embed([item["text"] for item in batch])
        result.dimensions = len(vectors[0]) if vectors else result.dimensions
        collection.upsert(
            ids=[item["id"] for item in batch],
            embeddings=vectors,
            documents=[item["text"] for item in batch],
            metadatas=[item["metadata"] for item in batch],
        )
    result.added = sum(1 for item in changed if item["id"] not in existing_hash)
    result.updated = len(changed) - result.added
    result.unchanged = len(prepared) - len(changed)

    stale = sorted(set(existing_hash) - set(ids))
    for start in range(0, len(stale), 1000):
        collection.delete(ids=stale[start:start + 1000])
    result.deleted = len(stale)
    if result.dimensions is None:
        result.dimensions = stored_dimensions(collection)
    return result


def stored_dimensions(collection) -> int | None:
    """새로 임베딩한 레코드가 없을 때 저장된 벡터에서 차원을 읽는다."""
    sample = collection.get(limit=1, include=["embeddings"])
    embeddings = sample.get("embeddings")
    if embeddings is None or len(embeddings) == 0:
        return None
    return len(embeddings[0])


def collection_ids(collection) -> set[str]:
    return set(collection.get(include=[])["ids"])
