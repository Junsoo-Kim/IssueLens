"""프롬프트 1·2·3, 근거 판정, 인용 검증 테스트 (가짜 LLM 사용)."""
from __future__ import annotations

import json

import pytest

from src.prompts.answer_generator import NO_EVIDENCE_MESSAGE, format_evidence, source_label
from src.prompts.candidate_selector import select_candidates
from src.prompts.evidence_grader import grade_evidence
from src.prompts.llm import LLMUnavailable, parse_json_object
from src.prompts.query_extractor import extract_queries
from src.retrieval.evidence_selector import Evidence
from src.services.citation_validator import check_citations, remove_citations

REPOS = [{"repo": "keycloak/keycloak"}, {"repo": "airbytehq/airbyte"}]


def llm_returning(text):
    calls = []

    def llm(system, user):
        calls.append((system, user))
        if isinstance(text, Exception):
            raise text
        return text
    llm.calls = calls
    return llm


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('결과입니다:\n```json\n{"a": [1, 2]}\n```', {"a": [1, 2]}),
    ('{broken {"b": 2}', {"b": 2}),
    ("JSON 없음", None),
    ("[1, 2]", None),
])
def test_parse_json_object(text, expected):
    assert parse_json_object(text) == expected


# ---------------------------------------------------------------- 프롬프트 1


def test_extract_queries_validates_llm_output():
    question = "Keycloak 26.0.1 업그레이드 후 TokenVerificationException이 나요"
    llm = llm_returning(json.dumps({
        "query_type": "error_log", "candidate_repos": ["Keycloak/keycloak", "no/such"],
        "search_queries": ["TokenVerificationException after upgrade", "token validation fails 26.0.1", "x", "y"],
        "exact_tokens": ["TokenVerificationException", "26.0.1", "InventedToken"],
        "filters": {"versions": ["26.0.1"], "unknown": ["z"]},
    }, ensure_ascii=False))
    plan = extract_queries(llm, question, REPOS)
    assert plan.source == "llm" and plan.query_type == "error_log"
    assert plan.search_queries == ["TokenVerificationException after upgrade", "token validation fails 26.0.1", "x"]
    assert plan.exact_tokens == ["TokenVerificationException", "26.0.1"]  # 질문에 없는 토큰은 버린다
    assert plan.candidate_repos == ["keycloak/keycloak"]
    assert plan.filters == {"versions": ["26.0.1"]}
    assert "keycloak/keycloak" in llm.calls[0][1]


def test_extract_queries_keeps_selected_issue_query():
    llm = llm_returning('{"query_type": "natural_language", "search_queries": ["other version"], "candidate_repos": []}')
    selected = {"issue_id": "keycloak/keycloak#5", "repo": "keycloak/keycloak", "title": "Login loop after upgrade"}
    plan = extract_queries(llm, "다른 버전에도 있나요?", REPOS, selected)
    assert plan.search_queries[0] == "Login loop after upgrade"
    assert plan.query_type == "selected_issue" and plan.candidate_repos == ["keycloak/keycloak"]


@pytest.mark.parametrize("llm,error", [
    (None, "LLM 없음"),
    (llm_returning("검색어는 token입니다"), "JSON 파싱 실패"),
    (llm_returning('{"search_queries": []}'), "검색어 없음"),
    (llm_returning(LLMUnavailable("boom")), "boom"),
])
def test_extract_queries_falls_back_to_rules(llm, error):
    plan = extract_queries(llm, "Keycloak 로그인 실패 TokenVerificationException", REPOS)
    assert plan.source == "rule" and plan.error == error
    assert plan.search_queries and plan.candidate_repos == ["keycloak/keycloak"]


# ---------------------------------------------------------------- 프롬프트 2


CANDIDATES = [
    {"issue_id": "keycloak/keycloak#1", "title": "Token refresh fails", "body": "x" * 1000, "labels": ["kind/bug"],
     "state_reason": "completed", "comments": 3},
    {"issue_id": "keycloak/keycloak#2", "title": "Docs typo", "body": "", "labels": [], "state_reason": "completed",
     "comments": 0},
    {"issue_id": "keycloak/keycloak#3", "title": "Token expired early", "body": "", "labels": [],
     "state_reason": "not_planned", "comments": 2},
]


def test_select_candidates_validates_ids():
    llm = llm_returning(json.dumps({"selected_ids": ["keycloak/keycloak#1", "invented#9", "keycloak/keycloak#1"],
                                    "reasons": {"keycloak/keycloak#1": "같은 증상", "invented#9": "?"}}))
    selection = select_candidates(llm, "토큰 갱신 실패", CANDIDATES, max_selected=2)
    assert selection.selected_ids == ["keycloak/keycloak#1"]
    assert selection.rejected_ids == ["keycloak/keycloak#2", "keycloak/keycloak#3"]
    assert selection.reasons == {"keycloak/keycloak#1": "같은 증상"} and selection.source == "llm"
    assert len(llm.calls[0][1].split("summary: ")[1].split("\n")[0]) <= 300  # 본문은 요약만 보낸다


def test_select_candidates_rule_fallback():
    selection = select_candidates(llm_returning("모르겠습니다"), "q", CANDIDATES, max_selected=3)
    assert selection.source == "rule" and selection.selected_ids == ["keycloak/keycloak#1"]
    assert select_candidates(None, "q", []).selected_ids == []


# ---------------------------------------------------------------- 근거 판정, 프롬프트 3, 인용


def evidence(n, doc_type="comment", role="maintainer", text="h\n\nFixed in 26.0.2 by updating the validator"):
    return Evidence(number=n, id=f"c{n}", issue_id="keycloak/keycloak#1", document_type=doc_type, title="Token refresh",
                    text=text, source_url=f"https://github.com/keycloak/keycloak/issues/1#c{n}", author_role=role)


def test_grade_evidence_hard_failure_skips_llm():
    llm = llm_returning('{"sufficient": true, "relevant_numbers": [1]}')
    grade = grade_evidence(llm, "q", [evidence(1)], {"evidence_count": 1, "reasons": ["질문의 정확 토큰(X)을 포함한 근거 없음"]})
    assert not grade.sufficient and grade.source == "rule" and llm.calls == []


@pytest.mark.parametrize("response,sufficient,relevant", [
    ('{"sufficient": true, "relevant_numbers": [1, 7], "reason": "같은 증상"}', True, [1]),
    ('{"sufficient": true, "relevant_numbers": [], "reason": "?"}', False, []),
    ('{"sufficient": false, "relevant_numbers": [1], "reason": "단어만 겹침"}', False, [1]),
])
def test_grade_evidence_llm(response, sufficient, relevant):
    grade = grade_evidence(llm_returning(response), "q", [evidence(1), evidence(2)], {"evidence_count": 2, "reasons": []})
    assert (grade.sufficient, grade.relevant_numbers, grade.source) == (sufficient, relevant, "llm")


def test_grade_evidence_falls_back_to_rule_assessment():
    grade = grade_evidence(llm_returning("응답 깨짐"), "q", [evidence(1)], {"evidence_count": 1, "insufficient": False})
    assert grade.sufficient and grade.source == "rule"


def test_format_evidence_labels_and_truncates():
    text = format_evidence([evidence(1), evidence(2, "pull_request", "contributor", "h\n\n" + "y" * 50)], max_chars=20)
    assert "[근거 1] (이슈 댓글, 유지보수자) keycloak/keycloak#1" in text
    assert "[근거 2] (PR 설명)" in text and "y" * 20 + "…" in text
    assert source_label(evidence(3, "issue_metadata", "")) == "이슈 메타데이터"


def test_check_and_remove_citations():
    answer = "원인은 검증기입니다 [근거 1]. 26.0.2에서 고쳐졌습니다 [근거 3][근거 1]. 다른 설명 [근거 9]."
    check = check_citations(answer, {1, 2, 3})
    assert (check.cited, check.invalid, check.valid) == ([1, 3], [9], False)
    assert "[근거 9]" not in remove_citations(answer, [9]) and "[근거 3]" in remove_citations(answer, [9])
    assert not check_citations(NO_EVIDENCE_MESSAGE, {1}).has_citations
