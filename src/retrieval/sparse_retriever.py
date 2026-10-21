"""BM25 Sparse 검색. 저장소별 인덱스를 필요할 때 불러온다."""
from __future__ import annotations

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
        """저장소별로 top_k를 구한 뒤 점수순으로 합친다.

        BM25 통계는 저장소별이지만, 선별 단계에서 저장소별 색인 규모를 비슷하게 맞춰 점수를 직접 비교해도
        왜곡이 작다. 순위를 번갈아 섞으면 매치 강도가 사라져 약한 매치가 저장소 순서만으로 앞설 수 있다.
        """
        tokens = tokenize(query)
        hits = []
        for repo in repos:
            index = self._index(repo)
            if index is None or index._bm25 is None:
                continue
            scores = index._bm25.get_scores(tokens)
            ranked = sorted(
                (i for i in range(len(index.ids)) if scores[i] > 0 and (allow is None or allow(index.ids[i]))),
                key=lambda i: (-scores[i], index.ids[i]),
            )
            hits.extend((index.ids[i], float(scores[i])) for i in ranked[:top_k])
        return sorted(hits, key=lambda h: (-h[1], h[0]))[:top_k]
