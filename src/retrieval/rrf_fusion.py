"""질의 유형별 가중 Reciprocal Rank Fusion (기획서 9.5절).

    RRF_score(d) = Σ w_r / (k + rank_r(d)),  rank는 1부터
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FusedHit:
    id: str
    score: float
    ranks: dict[str, int] = field(default_factory=dict)


def reciprocal_rank_fusion(rankings: dict[str, list[str]], weights: dict[str, float], k: int = 60) -> list[FusedHit]:
    """rankings: {"sparse": [id, ...], "dense": [id, ...]} 순위순 ID 목록. 같은 입력이면 같은 순서를 낸다."""
    hits: dict[str, FusedHit] = {}
    for name, ids in rankings.items():
        weight = weights.get(name, 1.0)
        for rank, doc_id in enumerate(ids, start=1):
            hit = hits.setdefault(doc_id, FusedHit(doc_id, 0.0))
            if name in hit.ranks:
                continue
            hit.ranks[name] = rank
            hit.score += weight / (k + rank)
    return sorted(hits.values(), key=lambda h: (-h.score, min(h.ranks.values()), h.id))
