"""저장소 라우팅 (기획서 9.3절).

1. 사용자가 고른 저장소나 선택 이슈의 저장소가 있으면 고정한다.
2. 질문에 저장소가 명시되면 그 저장소(최대 max_repositories개)로 고정한다.
3. 둘 다 없으면 전체 RAG 1 이슈 검색 결과를 저장소별로 모아, 한 저장소의 점수 비중이
   confident_share 이상이면 1개, 아니면 상위 2개를 고른다. 설명문 대신 실제 색인 이슈로 판단한다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from src.retrieval.rrf_fusion import FusedHit

CONFIDENT_SHARE = 0.6
ROUTING_DEPTH = 20


@dataclass
class RouteResult:
    repos: list[str]
    confidence: float
    reason: str
    scores: dict[str, float] = field(default_factory=dict)


def repo_of_issue_record(record_id: str) -> str:
    """`issue:owner:repo:123` -> `owner/repo`."""
    _, owner, name, _ = record_id.split(":")
    return f"{owner}/{name}"


def issue_id_of_record(record_id: str) -> str:
    """`issue:owner:repo:123` -> `owner/repo#123`."""
    _, owner, name, number = record_id.split(":")
    return f"{owner}/{name}#{number}"


def fixed_route(repos: list[str], reason: str, max_repositories: int) -> RouteResult:
    return RouteResult(repos[:max_repositories], 1.0, reason)


def route_by_hits(hits: list[FusedHit], max_repositories: int = 2, confident_share: float = CONFIDENT_SHARE,
                  depth: int = ROUTING_DEPTH) -> RouteResult:
    scores: dict[str, float] = defaultdict(float)
    for hit in hits[:depth]:
        scores[repo_of_issue_record(hit.id)] += hit.score
    if not scores:
        return RouteResult([], 0.0, "검색 결과 없음")
    total = sum(scores.values())
    ranked = sorted(scores, key=lambda r: (-scores[r], r))
    share = scores[ranked[0]] / total
    count = 1 if share >= confident_share else max_repositories
    return RouteResult(
        ranked[:count],
        round(share, 3),
        f"상위 {depth}개 이슈 중 {ranked[0]} 비중 {share:.2f}" + ("" if count == 1 else f" < {confident_share}, 상위 {count}개 검색"),
        {r: round(scores[r] / total, 3) for r in ranked},
    )
