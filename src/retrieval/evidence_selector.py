"""근거 선별: 중복 제거, 이슈·문서 유형 다양성 제한, 근거 부족 판정, 근거 번호 부여 (기획서 6.4절, 9.6절, 9.8절)."""
from __future__ import annotations

from dataclasses import dataclass, field

RESOLUTION_TYPES = {"pull_request", "pull_patch"}
MIN_EVIDENCE = 3


@dataclass
class Evidence:
    number: int
    id: str
    issue_id: str
    document_type: str
    title: str
    text: str
    source_url: str
    author_role: str = ""
    score: float = 0.0
    ranks: dict[str, int] = field(default_factory=dict)
    forced: bool = False


def _dedupe_key(hit: dict) -> str:
    metadata = hit["metadata"]
    return metadata.get("content_hash") or f"{metadata.get('source_url')}|{hit['text'].strip()}"


def select_evidence(
    hits: list[dict],
    limit: int = 8,
    max_per_issue: int = 3,
    max_per_type: int = 4,
    forced: list[dict] | None = None,
) -> list[Evidence]:
    """hits: 결합 점수순 [{"id", "text", "metadata", "score", "ranks"}]. forced는 점수와 무관하게 먼저 넣는다.

    근거 번호는 중복 제거와 선별이 끝난 뒤 1부터 매긴다.
    """
    selected, seen, per_issue, per_type = [], set(), {}, {}
    for hit, is_forced in [(h, True) for h in forced or []] + [(h, False) for h in hits]:
        if len(selected) >= limit:
            break
        key = _dedupe_key(hit)
        if key in seen:
            continue
        metadata = hit["metadata"]
        issue_id, doc_type = metadata.get("issue_id", ""), metadata.get("document_type", "")
        if not is_forced and (per_issue.get(issue_id, 0) >= max_per_issue or per_type.get(doc_type, 0) >= max_per_type):
            continue
        seen.add(key)
        per_issue[issue_id] = per_issue.get(issue_id, 0) + 1
        per_type[doc_type] = per_type.get(doc_type, 0) + 1
        selected.append((hit, is_forced))

    return [
        Evidence(
            number=i,
            id=hit["id"],
            issue_id=hit["metadata"].get("issue_id", ""),
            document_type=hit["metadata"].get("document_type", ""),
            title=hit["metadata"].get("document_title") or hit["metadata"].get("title", ""),
            text=hit["text"],
            source_url=hit["metadata"].get("source_url", ""),
            author_role=hit["metadata"].get("author_role", ""),
            score=hit.get("score", 0.0),
            ranks=hit.get("ranks", {}),
            forced=is_forced,
        )
        for i, (hit, is_forced) in enumerate(selected, start=1)
    ]


def assess_evidence(evidence: list[Evidence], min_evidence: int = MIN_EVIDENCE) -> dict:
    """근거 부족 판정의 초기 규칙. 임계값은 평가셋의 근거 없음 질문으로 조정한다 (기획서 9.8절)."""
    resolution = [e for e in evidence if e.document_type in RESOLUTION_TYPES or e.author_role == "maintainer"]
    reasons = []
    if len(evidence) < min_evidence:
        reasons.append(f"근거 {len(evidence)}건 < {min_evidence}건")
    if not resolution:
        reasons.append("해결 근거(병합 PR, 유지보수자 댓글) 없음")
    return {
        "insufficient": bool(reasons),
        "reasons": reasons,
        "evidence_count": len(evidence),
        "resolution_evidence_count": len(resolution),
        "issues": len({e.issue_id for e in evidence}),
    }
