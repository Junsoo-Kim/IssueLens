"""Chroma Dense 검색. 질의 임베딩은 색인과 같은 임베딩 설정을 쓴다."""
from __future__ import annotations

import time
from typing import Callable

from chromadb.errors import InternalError

# 같은 프로세스에서 막 쓴 컬렉션을 바로 조회하면 HNSW 세그먼트 파일이 아직 없어
# "Error creating hnsw segment reader: Nothing found on disk"가 날 수 있다 (Windows에서 관찰됨)
RETRY_DELAYS = (0.2, 0.5, 1.0)


class DenseSearchUnavailable(RuntimeError):
    """재시도 후에도 Dense 검색이 실패했다. 호출한 쪽은 BM25만으로 계속할 수 있다."""


def in_filter(field: str, values: list) -> dict:
    return {field: values[0]} if len(values) == 1 else {field: {"$in": list(values)}}


class DenseRetriever:
    def __init__(self, collection, embed_query: Callable[[str], list[float]], sleep: Callable[[float], None] = time.sleep):
        self.collection = collection
        self.embed_query = embed_query
        self.sleep = sleep

    def search(self, query: str, top_k: int, where: dict | None = None) -> list[tuple[str, float, dict]]:
        """(id, 유사도, 메타데이터) 목록. 코사인 거리를 1 - 거리로 바꿔 유사도로 쓴다."""
        if top_k <= 0 or self.collection.count() == 0:
            return []
        embedding = self.embed_query(query)
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                result = self.collection.query(
                    query_embeddings=[embedding],
                    n_results=min(top_k, self.collection.count()),
                    where=where,
                    include=["metadatas", "distances"],
                )
                break
            except InternalError as exc:
                if attempt == len(RETRY_DELAYS):
                    raise DenseSearchUnavailable(f"Dense 검색 실패: {exc}") from exc
                self.sleep(RETRY_DELAYS[attempt])
        return [(i, 1 - d, m) for i, d, m in zip(result["ids"][0], result["distances"][0], result["metadatas"][0])]
