"""자연어 조건 기반 이슈 탐색 (요구사항 F2011, 기획서 3.2절).

    1. 조건 추출: 저장소, 이슈 유형, 버전, 예외명은 필터로, 증상은 검색어로 쓴다.
       범주는 LLM이 추정하는 값이라 필터로 쓰면 맞는 이슈가 빠지기 쉬워 판정 참고로만 쓴다
    2. RAG 1 Hybrid 검색으로 증상이 비슷한 이슈 후보를 찾는다 (저장소 조건이 있으면 그 안에서만)
    3. 메타데이터 필터: 유형, 버전, 예외명이 맞지 않는 후보를 버린다
    4. LLM 판정: 남은 후보가 요청에 실제로 맞는지 다시 확인하고 통과한 이슈만 카드로 돌려준다
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from src.prompts.condition_search import Conditions, extract_conditions, judge_candidates
from src.prompts.llm import LLM
from src.retrieval.hybrid import HybridRetriever

JUDGE_CANDIDATES = 15  # LLM 판정에 보낼 최대 후보 수
MAX_RESULTS = 10


def _csv(value) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def version_matches(wanted: str, metadata: dict, text: str) -> bool:
    """'26'은 26, 26.0.1과 맞고 260이나 2.6과는 맞지 않는다. 메타데이터에 버전이 없으면 이슈 본문 요약에서 찾는다."""
    wanted = wanted.lower().lstrip("v")
    for version in _csv(metadata.get("versions_csv")):
        version = version.lower().lstrip("v")
        if version == wanted or version.startswith(wanted + "."):
            return True
    return re.search(rf"(?<![\w.])v?{re.escape(wanted)}(?:\.\d+)*(?![\w])", text, re.IGNORECASE) is not None


def filter_reasons(conditions: Conditions, metadata: dict, text: str) -> list[str]:
    """맞지 않는 조건 목록. 비어 있으면 통과."""
    failed = []
    if conditions.repos and metadata.get("repo") not in conditions.repos:
        failed.append("저장소")
    if conditions.issue_types and metadata.get("issue_type") not in conditions.issue_types:
        failed.append("유형")
    if conditions.versions and not any(version_matches(v, metadata, text) for v in conditions.versions):
        failed.append("버전")
    if conditions.error_types:  # 예외명을 지정했으면 메타데이터나 본문 요약에 그 이름이 있어야 한다
        haystack = f"{metadata.get('error_types_csv') or ''} {metadata.get('title') or ''} {text}".lower()
        if not any(name.lower() in haystack for name in conditions.error_types):
            failed.append("오류 유형")
    return failed


@dataclass
class ExploreResult:
    conditions: dict
    searched: int
    filtered_out: dict[str, int]  # 조건별로 걸러진 후보 수
    judged: dict | None
    issues: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)  # 메타데이터는 맞지만 판정에서 빠진 후보 (화면 확인용)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def issue_card(metadata: dict, reason: str) -> dict:
    """원본 메타데이터와 대조할 수 있게 필터에 쓴 필드를 그대로 보여 준다 (요구사항 정의서 4.4)."""
    return {
        "issue_id": metadata["issue_id"],
        "repo": metadata.get("repo"),
        "title": metadata.get("title"),
        "url": metadata.get("source_url"),
        "issue_type": metadata.get("issue_type"),
        "labels": _csv(metadata.get("labels_csv")),
        "versions": _csv(metadata.get("versions_csv")),
        "category": metadata.get("category"),
        "state": metadata.get("state"),
        "state_reason": metadata.get("state_reason"),
        "closed_at": metadata.get("closed_at"),
        "reason": reason,
    }


def explore_issues(llm: LLM | None, retriever: HybridRetriever, question: str, repositories: list[dict],
                   categories: dict[str, str], repo: str | None = None, max_results: int = MAX_RESULTS) -> ExploreResult:
    conditions = extract_conditions(llm, question, repositories, categories)
    if repo:  # 화면에서 고른 저장소가 우선한다
        conditions.repos = [repo]
    candidates, warnings = retriever.search_issues(conditions.symptom or question, conditions.repos or None)

    passed, filtered_out = [], {}
    for candidate in candidates:
        failed = filter_reasons(conditions, candidate["metadata"], candidate.get("text") or "")
        for name in failed:
            filtered_out[name] = filtered_out.get(name, 0) + 1
        if not failed:
            passed.append(candidate)

    judgement = judge_candidates(llm, question, conditions, passed[:JUDGE_CANDIDATES])
    by_id = {c["metadata"]["issue_id"]: c["metadata"] for c in passed}
    issues = [issue_card(by_id[i], reason) for i, reason in judgement.matches.items()][:max_results]
    rejected = [{"issue_id": c["metadata"]["issue_id"], "title": c["metadata"].get("title"),
                 "url": c["metadata"].get("source_url")}
                for c in passed[:JUDGE_CANDIDATES] if c["metadata"]["issue_id"] not in judgement.matches]
    if conditions.error:
        warnings.append(f"조건 추출을 규칙 기반으로 대체했습니다 ({conditions.error}).")
    if judgement.error and passed:
        warnings.append(f"LLM 판정 없이 메타데이터 조건만 적용했습니다 ({judgement.error}).")
    return ExploreResult(conditions.as_dict(), len(candidates), filtered_out, judgement.as_dict(), issues, rejected,
                         warnings)
