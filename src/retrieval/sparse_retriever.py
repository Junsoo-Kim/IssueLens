"""BM25 Sparse 검색. 저장소별 인덱스를 필요할 때 불러온다."""
from __future__ import annotations

from itertools import zip_longest
from pathlib import Path
from typing import Callable

from src.collectors.cache import repo_slug
from src.indexing.bm25_indexer import BM25Index, tokenize


class SparseRetriever:
    def __init__(self, index_dir: Path):
        self.index_dir = Path(index_dir)
        self._indexes: dict[str, BM25Index | None] = {}

    def _index(self, repo: str) -> BM25Index | None:
        if repo not in self._indexes:
            path = self.index_dir / f"{repo_slug(repo)}.json"
            self._indexes[repo] = BM25Index.load(path) if path.exists() else None
        return self._indexes[repo]

    def search(self, query: str, repos: list[str], top_k: int,
               allow: Callable[[str], bool] | None = None) -> list[tuple[str, float]]:
        """저장소별로 top_k를 구한 뒤 순위를 번갈아 합친다.

        BM25 점수는 저장소별 통계로 계산돼 저장소 사이에 직접 비교할 수 없으므로 점수가 아니라 순위로 섞는다.
        """
        tokens = tokenize(query)
        per_repo = []
        for repo in repos:
            index = self._index(repo)
            if index is None or index._bm25 is None:
                continue
            scores = index._bm25.get_scores(tokens)
            ranked = sorted(
                (i for i in range(len(index.ids)) if scores[i] > 0 and (allow is None or allow(index.ids[i]))),
                key=lambda i: (-scores[i], index.ids[i]),
            )
            per_repo.append([(index.ids[i], float(scores[i])) for i in ranked[:top_k]])
        merged = [hit for group in zip_longest(*per_repo) for hit in group if hit is not None]
        return merged[:top_k]
