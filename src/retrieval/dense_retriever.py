"""Chroma Dense 검색. 질의 임베딩은 색인과 같은 임베딩 설정을 쓴다."""
from __future__ import annotations

from typing import Callable


def in_filter(field: str, values: list) -> dict:
    return {field: values[0]} if len(values) == 1 else {field: {"$in": list(values)}}


class DenseRetriever:
    def __init__(self, collection, embed_query: Callable[[str], list[float]]):
        self.collection = collection
        self.embed_query = embed_query

    def search(self, query: str, top_k: int, where: dict | None = None) -> list[tuple[str, float, dict]]:
        """(id, 유사도, 메타데이터) 목록. 코사인 거리를 1 - 거리로 바꿔 유사도로 쓴다."""
        if top_k <= 0 or self.collection.count() == 0:
            return []
        result = self.collection.query(
            query_embeddings=[self.embed_query(query)],
            n_results=min(top_k, self.collection.count()),
            where=where,
            include=["metadatas", "distances"],
        )
        return [(i, 1 - d, m) for i, d, m in zip(result["ids"][0], result["distances"][0], result["metadatas"][0])]
