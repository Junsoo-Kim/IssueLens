"""근거 선별: 중복 제거, 이슈·문서·문서 유형 다양성 제한, 해결 근거 자리 확보, 근거 부족 판정, 근거 번호 부여
(기획서 6.4절, 9.6절, 9.8절)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

RESOLUTION_TYPES = {"pull_request", "pull_patch"}
MIN_EVIDENCE = 3
MIN_MEANINGFUL_CHARS = 40
NEAR_DUPLICATE_PREFIX = 200
NON_WORD = re.compile(r"[^0-9A-Za-z가-힣]+")


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
    resolution: bool = False


def chunk_body(text: str) -> str:
    """청크 앞의 저장소·제목 헤더를 뺀 본문."""
    return text.split("\n\n", 1)[1] if "\n\n" in text else text


def is_resolution(metadata: dict) -> bool:
    return metadata.get("document_type") in RESOLUTION_TYPES or metadata.get("author_role") == "maintainer"


def _near_duplicate_key(hit: dict) -> str:
    """반복 로그처럼 앞부분이 같은 청크를 같은 근거로 본다 (기획서 6.4절 4번: 유사도가 매우 높은 청크)."""
    words = NON_WORD.sub(" ", chunk_body(hit["text"])).split()
    return " ".join(words)[:NEAR_DUPLICATE_PREFIX].lower()


def _meaningful(hit: dict) -> bool:
    """코드 블록 경계에서 잘린 "```" 같은 빈 조각은 근거로 쓰지 않는다."""
    return len(NON_WORD.sub("", chunk_body(hit["text"]))) >= MIN_MEANINGFUL_CHARS


def select_evidence(
    hits: list[dict],
    limit: int = 8,
    max_per_issue: int = 3,
    max_per_type: int = 4,
    max_per_document: int = 2,
    forced: list[dict] | None = None,
    resolution_candidates: list[dict] | None = None,
    resolution_slots: int = 2,
) -> list[Evidence]:
    """hits: 결합 점수순 [{"id", "text", "metadata", "score", "ranks"}].

    - forced(선택 이슈의 메타데이터·본문)는 점수와 무관하게 먼저 넣는다.
    - 관련성 순으로 고른 근거에 해결 근거(병합 PR, 유지보수자 댓글)가 resolution_slots보다 적으면
      상위 이슈의 해결 문서(resolution_candidates)로 남은 자리를 채운다. 관련성이 낮은 해결 문서가
      관련 근거를 밀어내지 않도록 끝자리부터 교체한다.
    - 근거 번호는 선별이 끝난 뒤 1부터 매긴다.
    """
    selected: list[tuple[dict, bool]] = []
    seen_exact, seen_near = set(), set()
    per_issue, per_type, per_document = {}, {}, {}

    def try_add(hit: dict, is_forced: bool, ignore_caps: bool = False) -> bool:
        metadata = hit["metadata"]
        exact = metadata.get("content_hash") or f"{metadata.get('source_url')}|{hit['text'].strip()}"
        near = _near_duplicate_key(hit)
        if exact in seen_exact or (near and near in seen_near):
            return False
        if not is_forced and not _meaningful(hit):
            return False
        issue_id = metadata.get("issue_id", "")
        doc_type = metadata.get("document_type", "")
        document_id = metadata.get("document_id") or hit["id"]
        if not (is_forced or ignore_caps) and (
                per_issue.get(issue_id, 0) >= max_per_issue or per_type.get(doc_type, 0) >= max_per_type
                or per_document.get(document_id, 0) >= max_per_document):
            return False
        seen_exact.add(exact)
        seen_near.add(near)
        per_issue[issue_id] = per_issue.get(issue_id, 0) + 1
        per_type[doc_type] = per_type.get(doc_type, 0) + 1
        per_document[document_id] = per_document.get(document_id, 0) + 1
        selected.append((hit, is_forced))
        return True

    for hit in forced or []:
        try_add(hit, True)
    for hit in hits:
        if len(selected) >= limit:
            break
        try_add(hit, False)

    have = sum(is_resolution(h["metadata"]) for h, _ in selected)
    for candidate in resolution_candidates or []:
        if have >= resolution_slots:
            break
        if len(selected) >= limit:
            # 강제 근거가 아니고 해결 근거도 아닌 마지막 근거를 비운다
            drop = next((i for i in range(len(selected) - 1, -1, -1)
                         if not selected[i][1] and not is_resolution(selected[i][0]["metadata"])), None)
            if drop is None:
                break
            removed, _ = selected.pop(drop)
            m = removed["metadata"]
            per_issue[m.get("issue_id", "")] -= 1
            per_type[m.get("document_type", "")] -= 1
            per_document[m.get("document_id") or removed["id"]] -= 1
        if try_add(candidate, False, ignore_caps=True):
            have += 1

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
            resolution=is_resolution(hit["metadata"]),
        )
        for i, (hit, is_forced) in enumerate(selected, start=1)
    ]


def assess_evidence(evidence: list[Evidence], exact_tokens: list[str] | None = None,
                    min_evidence: int = MIN_EVIDENCE) -> dict:
    """근거 부족 판정의 초기 규칙. 임계값은 평가셋의 근거 없음 질문으로 조정한다 (기획서 9.8절).

    질문에 예외명·버전 같은 정확 토큰이 있는데 어느 근거에도 없으면, 비슷한 단어만 겹친 사례이므로 부족으로 본다.
    """
    resolution = [e for e in evidence if e.resolution]
    lowered = [e.text.lower() for e in evidence]
    matched_tokens = [t for t in exact_tokens or [] if any(t.lower() in text for text in lowered)]
    reasons = []
    if len(evidence) < min_evidence:
        reasons.append(f"근거 {len(evidence)}건 < {min_evidence}건")
    if not resolution:
        reasons.append("해결 근거(병합 PR, 유지보수자 댓글) 없음")
    if exact_tokens and not matched_tokens:
        reasons.append(f"질문의 정확 토큰({', '.join(exact_tokens)})을 포함한 근거 없음")
    return {
        "insufficient": bool(reasons),
        "reasons": reasons,
        "evidence_count": len(evidence),
        "resolution_evidence_count": len(resolution),
        "matched_exact_tokens": matched_tokens,
        "issues": len({e.issue_id for e in evidence}),
    }
