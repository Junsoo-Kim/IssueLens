"""LangGraph retrieve/generate와 재시도·답변 거부·일반 답변·질문 시점 수집 테스트.

실제 색인 파이프라인(가짜 임베딩)으로 작은 색인을 만들고, 가짜 LLM과 가짜 GitHub 응답을 쓴다.
"""
from __future__ import annotations

import json
import re
import shutil

import pytest
import yaml

from src.collectors.cache import read_jsonl, write_jsonl_atomic
from src.config import PROJECT_ROOT
from src.prompts.answer_generator import NO_EVIDENCE_MESSAGE
from tests.conftest import FakeResponse
from tests.test_indexing import FakeEmbedder
from tests.test_retrieval import index_dirs  # noqa: F401  (픽스처)

KC = "keycloak/keycloak"
NEW_ISSUE = 77
EVAL_ISSUE = 90


class FakeLLM:
    """시스템 프롬프트로 어떤 프롬프트인지 구분해 미리 정한 응답을 돌려준다."""

    def __init__(self, relevant_issue=f"{KC}#2", answers=None, plan=None):
        self.relevant_issue = relevant_issue
        self.answers = list(answers or ["원인은 토큰 검증기입니다 [근거 1]."])
        self.plan = plan or {"query_type": "error_log", "candidate_repos": [KC],
                             "search_queries": ["token refresh fails after realm import"], "exact_tokens": []}
        self.calls = []

    def __call__(self, system, user):
        if "영어 검색어" in system:
            self.calls.append("plan")
            return json.dumps(self.plan)
        if "검색 결과를 검수" in system:
            self.calls.append("grade")
            numbers = [int(n) for n, issue in re.findall(r"\[근거 (\d+)\] \([^)]*\) (\S+)", user) if issue == self.relevant_issue]
            return json.dumps({"sufficient": bool(numbers), "relevant_numbers": numbers, "reason": "테스트 판정"})
        if "후보 목록에서 고릅니다" in system:
            self.calls.append("select")
            return json.dumps({"selected_ids": [f"{KC}#{NEW_ISSUE}"], "reasons": {f"{KC}#{NEW_ISSUE}": "같은 증상"}})
        if "[근거]만 사용해" in system:
            self.calls.append("answer")
            return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if "일반 지식으로만" in system:
            self.calls.append("general")
            return "일반적으로 토큰 만료 설정을 확인합니다."
        raise AssertionError("알 수 없는 프롬프트")


def github_routes():
    base = f"/repos/{KC}"
    item = lambda n, title: {  # noqa: E731
        "number": n, "title": title, "body": "### Describe the bug\nToken refresh fails after realm import.",
        "labels": [{"name": "kind/bug"}], "state": "closed", "state_reason": "completed", "comments": 1,
        "html_url": f"https://github.com/{KC}/issues/{n}", "user": {"login": "reporter", "avatar_url": "x"}}
    return {
        "/search/issues": [FakeResponse(json_data={"total_count": 3, "incomplete_results": False, "items": [
            item(NEW_ISSUE, "Token refresh fails after realm import"),
            item(EVAL_ISSUE, "Token refresh broken (평가용 이슈)"),
            item(1, "Admin console theme not refreshed"),
        ]})],
        f"{base}/issues/{NEW_ISSUE}": [FakeResponse(json_data=item(NEW_ISSUE, "Token refresh fails after realm import"))],
        f"{base}/issues/{NEW_ISSUE}/comments": [FakeResponse(json_data=[{
            "id": 7701, "body": "Fixed in 26.1.0: realm import now rebuilds the token signing keys.",
            "author_association": "MEMBER", "user": {"login": "core-dev", "type": "User", "avatar_url": "x"},
            "html_url": f"https://github.com/{KC}/issues/{NEW_ISSUE}#issuecomment-7701"}])],
        f"{base}/issues/{NEW_ISSUE}/timeline": [FakeResponse(json_data=[])],
    }


@pytest.fixture
def rag_root(index_dirs, tmp_path):  # noqa: F811
    """색인 픽스처 위에 configs와 최종 분리 manifest를 갖춘 프로젝트 루트."""
    root = tmp_path / "root"
    shutil.copytree(PROJECT_ROOT / "configs", root / "configs")
    app = yaml.safe_load((root / "configs/app.yaml").read_text(encoding="utf-8"))
    app["paths"] = {"raw_dir": str(index_dirs / "raw"), "processed_dir": str(index_dirs / "p"),
                    "cache_dir": str(tmp_path / "cache"), "manifest_dir": str(tmp_path / "manifests"),
                    "chroma_dir": str(index_dirs / "chroma"), "sparse_index_dir": str(index_dirs / "sparse")}
    (root / "configs/app.yaml").write_text(yaml.safe_dump(app, allow_unicode=True), encoding="utf-8")
    final = [{"issue_id": f"{KC}#{n}", "final_split": "index"} for n in (1, 2, 3)]
    final.append({"issue_id": f"{KC}#{EVAL_ISSUE}", "final_split": "evaluation"})
    write_jsonl_atomic(tmp_path / "manifests" / "final_selection.jsonl", final)
    return root


def make_assistant(root, llm, make_client=None, collect=False):
    from src.services.assistant import build_assistant

    client = make_client(github_routes())[0] if make_client else None
    return build_assistant(root, llm=llm, embedder=FakeEmbedder(), github_client=client, enable_collection=collect)


def test_graph_has_retrieve_and_generate_nodes(rag_root):
    from src.graph.workflow import graph_diagram

    diagram = graph_diagram(make_assistant(rag_root, FakeLLM()).graph)
    assert "retrieve" in diagram and "generate" in diagram


def test_answers_from_evidence_with_valid_citations(rag_root):
    llm = FakeLLM()
    state = make_assistant(rag_root, llm).ask("TokenVerificationException after upgrade")
    assert state["sufficient"] and state["answered_from_evidence"]
    assert state["attempts"][0]["stage"] == "original" and len(state["attempts"]) == 1
    assert [e.number for e in state["cited_evidence"]] == [1]
    assert state["citation_check"] == {"cited": [1], "invalid": []}
    assert state["query_plan"]["source"] == "llm"
    assert llm.calls[:3] == ["plan", "grade", "answer"]
    assert set(state["timings"]) >= {"query_plan", "retrieve_total", "generate"}


def test_invalid_citation_is_regenerated_then_removed(rag_root):
    retried = make_assistant(rag_root, FakeLLM(answers=["잘못된 번호 [근거 42].", "고친 답변 [근거 1]."])).ask(
        "TokenVerificationException after upgrade")
    assert retried["answer"] == "고친 답변 [근거 1]." and not retried["warnings"]

    stubborn = make_assistant(rag_root, FakeLLM(answers=["계속 틀림 [근거 42] [근거 1]."])).ask(
        "TokenVerificationException after upgrade")
    assert "[근거 42]" not in stubborn["answer"] and "[근거 1]" in stubborn["answer"]
    assert stubborn["warnings"] == ["존재하지 않는 근거 번호 [42]를 답변에서 지웠습니다."]


def test_abstains_after_retries_and_general_answer_is_opt_in(rag_root):
    llm = FakeLLM(relevant_issue="no/such#1")
    assistant = make_assistant(rag_root, llm)
    state = assistant.ask("Kubernetes 노드가 갑자기 NotReady가 돼요")
    assert not state["sufficient"] and state["answer"] == NO_EVIDENCE_MESSAGE
    assert [a["stage"] for a in state["attempts"]] == ["original", "rewritten"]  # 수집기 꺼짐
    assert state["cited_evidence"] == [] and state["general_answer"] is None
    assert state["nearest_issues"] and state["nearest_issues"][0]["url"].startswith("https://github.com/")
    assert "answer" not in llm.calls

    allowed = assistant.ask("Kubernetes 노드가 갑자기 NotReady가 돼요", allow_general_answer=True)
    assert allowed["answer"] == NO_EVIDENCE_MESSAGE  # 거부 문구는 항상 먼저
    assert allowed["general_answer"] == "일반적으로 토큰 만료 설정을 확인합니다."


def test_without_llm_uses_rules_and_reports_missing_key(rag_root, monkeypatch):
    from src.services.assistant import build_assistant

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assistant = build_assistant(rag_root, embedder=FakeEmbedder(), enable_collection=False)
    state = assistant.ask("TokenVerificationException after upgrade")
    assert state["query_plan"]["source"] == "rule"
    assert [a["stage"] for a in state["attempts"]] == ["original"]  # 규칙 기반 재작성은 같은 검색이라 건너뜀
    assert state["answer"] == NO_EVIDENCE_MESSAGE
    assert any("OPENAI_API_KEY" in w for w in state["warnings"])


def test_query_time_collection_indexes_new_issue_and_caches(rag_root, make_client):
    llm = FakeLLM(relevant_issue=f"{KC}#{NEW_ISSUE}")
    assistant = make_assistant(rag_root, llm, make_client, collect=True)
    state = assistant.ask("realm import 후에 토큰 갱신이 실패해요")

    assert [a["stage"] for a in state["attempts"]] == ["original", "rewritten", "collected"]
    collection = state["collection"]
    assert collection["candidates"] == [{"issue_id": f"{KC}#{NEW_ISSUE}", "title": "Token refresh fails after realm import"}]
    assert f"{KC}#{EVAL_ISSUE}" not in json.dumps(collection["candidates"])  # 평가용 이슈는 후보에서 제외
    assert collection["selection"]["selected_ids"] == [f"{KC}#{NEW_ISSUE}"]
    assert collection["collected"] == [f"{KC}#{NEW_ISSUE}"] and collection["indexed_chunks"] >= 2
    assert f"{KC}#1" in collection["available"]  # 이미 색인된 이슈는 수집 없이 검색 범위에만 넣는다
    assert state["sufficient"] and any(e.issue_id == f"{KC}#{NEW_ISSUE}" for e in state["evidence"])

    root_paths = yaml.safe_load((rag_root / "configs/app.yaml").read_text(encoding="utf-8"))["paths"]
    from pathlib import Path
    processed = Path(root_paths["processed_dir"])
    documents = read_jsonl(processed / "query_time_documents.jsonl")
    assert {d["collection_source"] for d in documents} == {"query_time"}
    log = read_jsonl(processed / "query_collection_log.jsonl")
    assert log[-1]["selection"]["selected_ids"] == [f"{KC}#{NEW_ISSUE}"]

    # 같은 질문을 다시 하면 GitHub 검색을 다시 하지 않는다 (NF2002)
    again_assistant = make_assistant(rag_root, FakeLLM(relevant_issue="no/such#1"), make_client, collect=True)
    session = again_assistant.pipeline.collector.client.session
    again = again_assistant.ask("realm import 후에 토큰 갱신이 실패해요")
    assert again["collection"]["cache_hits"] and again["collection"]["searched"] == []
    assert session.calls == []


def test_rebuild_keeps_query_time_documents(rag_root, make_client, index_dirs):  # noqa: F811
    from scripts import build_indexes

    make_assistant(rag_root, FakeLLM(relevant_issue=f"{KC}#{NEW_ISSUE}"), make_client, collect=True).ask(
        "realm import 후에 토큰 갱신이 실패해요")
    args = ["--selection", str(index_dirs / "final.jsonl"), "--deep-selection", str(index_dirs / "final.jsonl"),
            "--tagged", str(index_dirs / "tagged.jsonl"), "--deep-issues", str(index_dirs / "deep.jsonl"),
            "--raw-dir", str(index_dirs / "raw"), "--processed-dir", str(index_dirs / "p"),
            "--manifest-dir", str(index_dirs / "m"), "--chroma-dir", str(index_dirs / "chroma"),
            "--sparse-dir", str(index_dirs / "sparse")]
    code, report = build_indexes.build(args, embedder=FakeEmbedder())
    assert code == 0
    assert report["counts"]["query_time_documents"] >= 2
    assert report["sync"]["issue_documents"]["deleted"] == 0 and report["sync"]["issue_documents"]["added"] == 0
    assert report["verification"]["issue_documents_ids_match"] is True


def test_ask_cli_prints_stages_answer_and_evidence(rag_root, capsys):
    from scripts.ask import main

    assert main(["TokenVerificationException after upgrade", "--no-collect"], root=rag_root, llm=FakeLLM(),
                embedder=FakeEmbedder()) == 0
    out = capsys.readouterr().out
    for expected in ["== 프롬프트 1 (검색어 추출)", "1. 원래 질문:", "판정 충분", "원인은 토큰 검증기입니다 [근거 1].",
                     "== 답변에 사용한 근거", "[근거 1]", "처리 시간:"]:
        assert expected in out, expected

    assert main(["Kubernetes NotReady", "--no-collect", "--allow-general"], root=rag_root,
                llm=FakeLLM(relevant_issue="no/such#1"), embedder=FakeEmbedder()) == 0
    out = capsys.readouterr().out
    assert NO_EVIDENCE_MESSAGE in out and "가장 가까운 사례" in out and "RAG 미사용 일반 답변" in out

    assert main(["--graph"], root=rag_root, llm=FakeLLM(), embedder=FakeEmbedder()) == 0
    assert "retrieve" in capsys.readouterr().out


def test_dense_failure_warning_reaches_answer(rag_root):
    from src.retrieval.dense_retriever import DenseRetriever
    from tests.test_retrieval import FlakyCollection

    assistant = make_assistant(rag_root, FakeLLM())
    for name in ("issue_dense", "document_dense"):
        setattr(assistant.retriever, name, DenseRetriever(FlakyCollection(99), lambda q: [0.0], sleep=lambda s: None))
    state = assistant.ask("TokenVerificationException after upgrade")
    assert state["answered_from_evidence"]
    assert any("BM25 결과만" in w for w in state["warnings"])
