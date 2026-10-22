"""조건 탐색(F2011), 로그 진단(F2010), 진행 상태(F2012) 테스트. 작은 색인과 가짜 LLM을 쓴다."""
from __future__ import annotations

import json

from src.prompts.condition_search import extract_conditions, rule_based_conditions
from src.services.incident_diagnoser import diagnose, diagnosis_question, compact_log, extract_signals
from src.services.issue_explorer import explore_issues, filter_reasons, version_matches
from tests.test_graph import KC, FakeLLM, make_assistant, rag_root  # noqa: F401  (픽스처)
from tests.test_retrieval import index_dirs, make_retriever  # noqa: F401  (픽스처)

CATEGORIES = {"auth": "인증 및 권한", "database_search": "데이터베이스 및 검색"}
REPOS = [{"repo": KC, "aliases": ["keycloak"]}, {"repo": "airbytehq/airbyte", "aliases": ["airbyte"]}]


class ExploreLLM(FakeLLM):
    def __init__(self, conditions, matches=None, **kwargs):
        super().__init__(**kwargs)
        self.conditions = conditions
        self.matches = matches

    def __call__(self, system, user):
        if "검색 조건을 정리" in system:
            self.calls.append("conditions")
            return json.dumps(self.conditions)
        if "조건에 맞는지 판정" in system:
            self.calls.append("judge")
            self.judged_user = user
            return json.dumps({"matches": self.matches})
        return super().__call__(system, user)


# ---------------------------------------------------------------- 조건 탐색


def test_rule_conditions_extract_repo_type_and_major_version():
    conditions = rule_based_conditions("Keycloak 26 버전 업그레이드 후 토큰 검증이 실패한 종료 버그를 찾아줘", REPOS)
    assert conditions.repos == [KC] and conditions.issue_types == ["bug"] and conditions.versions == ["26"]


def test_llm_conditions_drop_invented_values():
    llm = ExploreLLM({"repos": ["Keycloak/keycloak", "no/such"], "issue_types": ["bug", "epic"],
                      "versions": ["26", "25.0.1"], "categories": ["auth", "unknown_area"], "symptom": "token validation fails"})
    conditions = extract_conditions(llm, "keycloak 26 업그레이드 후 토큰 검증 실패 버그", REPOS, CATEGORIES)
    assert conditions.source == "llm"
    assert conditions.repos == [KC] and conditions.issue_types == ["bug"]
    assert conditions.versions == ["26"] and conditions.categories == ["auth"]


def test_version_match_uses_prefix_boundaries():
    assert version_matches("26", {"versions_csv": "26.0.1"}, "")
    assert not version_matches("26", {"versions_csv": "260.1,2.6"}, "")
    assert version_matches("26.0", {}, "After upgrade to 26.0.1 login fails")
    assert not version_matches("26", {}, "build 1260 failed")


def test_filter_reasons_lists_each_failed_condition():
    conditions = rule_based_conditions("airbyte 질문", REPOS)
    conditions.versions = ["9"]
    metadata = {"repo": KC, "issue_type": "bug", "versions_csv": ""}
    assert filter_reasons(conditions, metadata, "") == ["저장소", "유형", "버전"]


def test_explore_keeps_only_matching_and_judged_issues(index_dirs):  # noqa: F811
    retriever = make_retriever(index_dirs)
    llm = ExploreLLM({"repos": [KC], "issue_types": ["bug"], "versions": ["26"], "categories": [],
                      "symptom": "login fails token verification after upgrade"},
                     matches=[{"issue_id": f"{KC}#2", "reason": "26.0.1 업그레이드 후 토큰 검증 실패"},
                              {"issue_id": "no/such#1", "reason": "지어낸 이슈"}])
    result = explore_issues(llm, retriever, "keycloak 26 업그레이드 후 로그인 토큰 검증 실패 버그 찾아줘", REPOS, CATEGORIES)

    assert [card["issue_id"] for card in result.issues] == [f"{KC}#2"]  # 후보에 없는 id는 버린다
    card = result.issues[0]
    assert card["repo"] == KC and card["issue_type"] == "bug" and card["url"].endswith("/issues/2")
    assert card["reason"].startswith("26.0.1")
    assert result.filtered_out.get("버전", 0) >= 1  # 버전이 없는 keycloak 이슈는 메타데이터 필터에서 빠진다
    assert f"{KC}#1" not in llm.judged_user and "airbyte" not in llm.judged_user  # 필터를 통과한 후보만 판정


def test_explore_without_llm_falls_back_to_rules(index_dirs):  # noqa: F811
    retriever = make_retriever(index_dirs)
    result = explore_issues(None, retriever, "airbyte timeout 버그", REPOS, CATEGORIES)
    assert result.issues and all(card["repo"] == "airbytehq/airbyte" for card in result.issues)
    assert result.judged["source"] == "rule" and any("규칙 기반" in w for w in result.warnings)


# ---------------------------------------------------------------- 로그 진단


JAVA_LOG = """2026-01-02 10:00:00 ERROR [org.keycloak.services] (executor-thread-1) Uncaught server error
org.keycloak.exceptions.TokenVerificationException: Invalid token issuer
    at org.keycloak.TokenVerifier.verify(TokenVerifier.java:123)
    at org.keycloak.services.managers.AuthenticationManager.verifyIdentityToken(AuthenticationManager.java:1450)
Caused by: java.lang.IllegalStateException: Keycloak 26.0.1 running on Kubernetes with PostgreSQL 15.4
"""


def test_extract_signals_from_java_log():
    signals = extract_signals(JAVA_LOG)
    assert signals["exceptions"][:2] == ["TokenVerificationException", "IllegalStateException"]
    assert "org.keycloak" in signals["libraries"]
    assert {"26.0.1", "15.4"} <= set(signals["versions"])
    assert {"Kubernetes", "PostgreSQL", "Java"} <= set(signals["environments"])
    assert any(line.startswith("Caused by") for line in signals["error_lines"])


def test_trim_long_log_keeps_head_and_error_lines():
    log = "\n".join([f"INFO line {i}" for i in range(400)] + ["ERROR final failure TokenVerificationException"])
    trimmed = compact_log(log)
    assert len(trimmed) <= 3000 and trimmed.startswith("INFO line 0") and "ERROR final failure" in trimmed
    assert "상황 설명: 업그레이드 직후" in diagnosis_question(JAVA_LOG, "업그레이드 직후")


def test_diagnose_links_evidence_and_similar_issues(rag_root):  # noqa: F811
    assistant = make_assistant(rag_root, FakeLLM())
    stages = []
    result = diagnose(assistant, JAVA_LOG, progress=stages.append)
    assert result.state["answered_from_evidence"] and result.state["cited_evidence"]
    assert result.similar_issues[0] == {"issue_id": f"{KC}#2", "title": "Login fails with TokenVerificationException",
                                        "url": f"https://github.com/{KC}/issues/2", "used_as_evidence": True}
    assert stages[0] == "검색 조건 분석 중" and "답변 생성 중" in stages


def test_diagnose_without_evidence_marks_similar_as_reference(rag_root):  # noqa: F811
    assistant = make_assistant(rag_root, FakeLLM(relevant_issue="no/such#1"))
    result = diagnose(assistant, "ERROR NullPointerException in scheduler on Windows")
    assert not result.state["answered_from_evidence"]
    assert result.similar_issues and not any(i["used_as_evidence"] for i in result.similar_issues)


def test_sample_logs_finds_stack_trace_blocks():
    from scripts.sample_logs import log_blocks

    body = f"Steps\n```\nshort\n```\n\n```text\n{JAVA_LOG}```\n```yaml\nkey: value\nother: 1\nmore: 2\n```"
    blocks = log_blocks(body)
    assert len(blocks) == 1 and blocks[0].startswith("2026-01-02 10:00:00 ERROR")


def test_explore_reports_rejected_candidates(index_dirs):  # noqa: F811
    retriever = make_retriever(index_dirs)
    llm = ExploreLLM({"repos": [KC], "issue_types": ["bug"], "versions": [], "categories": [], "symptom": "login"},
                     matches=[])
    result = explore_issues(llm, retriever, "keycloak 로그인 버그", REPOS, CATEGORIES)
    assert not result.issues and result.rejected and all(r["issue_id"].startswith(KC) for r in result.rejected)


def test_snippet_escapes_markdown():
    from src.ui.sources import snippet

    text = snippet("header\n\n## Environment - **bold** [link](http://x) `code`")
    assert "\\#\\# Environment" in text and "\\*\\*bold" in text and "\\[link\\]\\(" in text


KOTLIN_LOG = """2026-03-27 ERROR main Failed. io.airbyte.cdk.ConfigErrorException: Incumbent CDC state is invalid
        at io.airbyte.cdk.read.cdc.CdcPartitionsCreator.run(CdcPartitionsCreator.kt:85) ~[bulk-cdk-toolkit-extract-cdc-0.1.77.jar:?]
        at kotlin.coroutines.jvm.internal.BaseContinuationImpl.resumeWith(ContinuationImpl.kt:33) ~[kotlin-stdlib-2.1.10.jar:2.1.10-release-473]
        at kotlinx.coroutines.DispatchedTask.run(DispatchedTask.kt:104) ~[kotlinx-coroutines-core-jvm-1.8.1.jar:?]
Stack Trace: io.airbyte.cdk.ConfigErrorException: Incumbent CDC state is invalid
        at io.airbyte.cdk.read.cdc.CdcPartitionsCreator.run(CdcPartitionsCreator.kt:85)
""" + "\n".join(f"        at io.airbyte.cdk.read.Step{i}.run(Step.kt:{i})" for i in range(10))


def test_compact_log_drops_runtime_frames_and_jar_versions():
    text = compact_log(KOTLIN_LOG)
    assert "ConfigErrorException" in text and "CdcPartitionsCreator" in text
    assert "kotlin" not in text and ".jar" not in text
    assert sum(line.lstrip().startswith("at ") for line in text.splitlines()) == 5  # 프레임은 앞 5개만
    assert text.count("CdcPartitionsCreator.run") == 1  # 반복된 줄은 한 번만
    assert extract_signals(KOTLIN_LOG)["versions"] == []  # jar 안의 라이브러리 버전은 제품 버전이 아니다


def test_explore_requires_exception_name_when_given(index_dirs):  # noqa: F811
    retriever = make_retriever(index_dirs)
    llm = ExploreLLM({"repos": [], "issue_types": [], "versions": [], "categories": [], "symptom": "sync times out"},
                     matches=[{"issue_id": "airbytehq/airbyte#4", "reason": "타임아웃"},
                              {"issue_id": "airbytehq/airbyte#5", "reason": "지어낸 일치"}])
    result = explore_issues(llm, retriever, "SocketTimeoutException 나는 동기화 이슈 찾아줘", REPOS, CATEGORIES)
    assert result.conditions["error_types"] == ["SocketTimeoutException"]
    assert [c["issue_id"] for c in result.issues] == ["airbytehq/airbyte#4"]
    assert result.filtered_out.get("오류 유형", 0) >= 1
