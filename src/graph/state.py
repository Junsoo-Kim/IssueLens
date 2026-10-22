"""LangGraph RAGState 정의 (기획서 8.1절)."""
from __future__ import annotations

from typing import Any, TypedDict


class RAGState(TypedDict, total=False):
    # 입력
    question: str
    selected_issue_id: str | None
    repo: str | None
    allow_general_answer: bool
    # retrieve 노드
    query_plan: dict  # 프롬프트 1 결과
    attempts: list[dict]  # 시도별 검색어, 라우팅, 상위 이슈, 근거 수, 판정
    collection: dict | None  # 질문 시점 외부 수집 결과
    evidence: list[Any]  # evidence_selector.Evidence
    sufficient: bool
    grade: dict
    nearest_issues: list[dict]  # 근거 부족 시 보여 줄 가장 가까운 사례 (주장 없이 링크만)
    retrieval_warnings: list[str]  # 예: Dense 검색 실패로 BM25만 사용
    # generate 노드
    answer: str
    answered_from_evidence: bool
    cited_evidence: list[Any]
    citation_check: dict
    general_answer: str | None
    warnings: list[str]
    timings: dict[str, float]
