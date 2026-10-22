"""에이전트 단계 E: 리뷰 후보 추출(프롬프트 4), 후보별 근거 검색·판정, 해결 방법 초안."""
from __future__ import annotations

import json
import re
from pathlib import Path

from agent.diff_reader import parse_unified_diff
from agent.library_corpus import LibraryCorpusBuilder, LibraryIndex
from agent.registry import Library, Registry, load_corpus
from agent.researcher import Researcher, build_researcher
from agent.reviewer import Finding, format_patch, parse_findings, review, rule_findings
from agent.signals import extract_signals, format_signals
from tests.test_agent import DIFF, FAKE_KEY, SETTINGS
from tests.test_indexing import FakeEmbedder
from tests.test_library_corpus import DISCUSSION, REPO, RETRIEVAL, TAGGING, fake_post, routes

ROOT = Path(__file__).resolve().parents[1]
CORPUS = load_corpus(ROOT / "configs/agent_corpus.yaml")


def registry(tmp_path):
    return Registry(CORPUS, tmp_path, fetch=lambda url: None)


def bundle():
    return parse_unified_diff(DIFF, "local.patch", SETTINGS)


class FakeLLM:
    """시스템 프롬프트로 프롬프트를 구분한다. relevant: 근거 판정에서 관련 있다고 볼 이슈."""

    def __init__(self, findings=None, relevant=f"{REPO}#1", draft="스트리밍 타임아웃은 재시도로 고쳐졌습니다 [근거 {n}].\n확인: timeout 설정"):
        self.findings = findings
        self.relevant = relevant
        self.draft = draft
        self.calls = []
        self.prompts = []

    def relevant_numbers(self, user):
        return [int(n) for n, issue in re.findall(r"\[근거 (\d+)\] \([^)]*\) (\S+)", user) if issue == self.relevant]

    def __call__(self, system, user):
        self.prompts.append(user)
        if "PR을 검토하는 시니어 리뷰어" in system:
            self.calls.append("review")
            return json.dumps({"findings": self.findings or []}) if self.findings is not None else "형식이 깨진 응답"
        if "검색 결과를 검수" in system:
            self.calls.append("grade")
            numbers = self.relevant_numbers(user)
            return json.dumps({"sufficient": bool(numbers), "relevant_numbers": numbers, "reason": "테스트 판정"})
        if "후보 목록에서 고릅니다" in system:
            self.calls.append("select")
            return json.dumps({"selected_ids": [self.relevant], "reasons": {}})
        if "PR 리뷰를 돕는 기술 분석가" in system:
            self.calls.append("resolution")
            return self.draft.format(n=(self.relevant_numbers(user) or [99])[0])
        raise AssertionError(f"알 수 없는 프롬프트: {system[:40]}")


def build_index(make_client, tmp_path) -> Path:
    client, _ = make_client(routes(), cache=False)
    index = LibraryIndex(tmp_path / "agent_index", RETRIEVAL, FakeEmbedder())
    LibraryCorpusBuilder(client, index, TAGGING, post=fake_post([DISCUSSION])).build(Library(REPO, "llm_sdk", ["openai"]), 10, {})
    return index.root


def finding(**extra):
    return Finding(**{"id": "f1", "file": "app/llm.py", "line": 5, "kind": "library_usage", "library": "openai",
                      "repo": REPO, "summary": "스트리밍 호출에 timeout 미지정", "concern": "APITimeoutError가 날 수 있음",
                      "search_queries": ["stream timeout"], "exact_tokens": ["APITimeoutError"], **extra})


# ---------------------------------------------------------------- 프롬프트 4


def test_patch_is_numbered_and_masked():
    text = format_patch(bundle())
    assert "    2 + openai==1.51.0" in text and "      - openai==0.28.1" in text
    assert "    5 +     return openai.ChatCompletion.create" in text
    assert FAKE_KEY not in text and "poetry.lock" not in text


def test_parse_findings_validates_location_kind_and_library(tmp_path):
    data = {"findings": [
        {"file": "app/llm.py", "line": 5, "kind": "library_usage", "library": "openai", "summary": "예전 API 호출",
         "search_queries": ["ChatCompletion removed in 1.0"], "exact_tokens": ["ChatCompletion"], "severity": "high"},
        {"file": "app/llm.py", "line": 99, "kind": "general", "library": "openai", "summary": "예외 처리 없음"},
        {"file": "nope.py", "line": 1, "kind": "general", "summary": "없는 파일"},
        {"file": "requirements.txt", "line": 2, "kind": "weird", "summary": "", "severity": "high"},
        {"file": "requirements.txt", "line": 2, "kind": "dependency", "library": "unknown-pkg", "summary": "모르는 패키지"},
    ]}
    found = parse_findings(data, bundle(), registry(tmp_path).resolve)
    assert [(f.file, f.line, f.kind) for f in found] == [
        ("app/llm.py", 5, "library_usage"), ("app/llm.py", 5, "general"), ("requirements.txt", 2, "dependency")]
    usage, general, unknown = found
    assert usage.repo == REPO and usage.needs_research and usage.severity == "high"
    assert general.library is None and not general.needs_research  # 줄 99는 patch에 없어 가장 가까운 추가 줄로
    assert unknown.repo is None and not unknown.needs_research and unknown.severity == "low"


def test_review_caps_and_falls_back_to_rule_signals(tmp_path):
    diff_bundle = bundle()
    signals = extract_signals(diff_bundle)
    resolve = registry(tmp_path).resolve
    many = [{"file": "app/llm.py", "line": 5, "kind": "general", "summary": f"지적 {i}", "severity": "low"} for i in range(7)]
    result = review(FakeLLM(findings=many), diff_bundle, signals, format_signals(signals), resolve, max_rag=5, max_general=2)
    assert result.source == "llm" and [f.id for f in result.findings] == ["f1", "f2"]

    broken = review(FakeLLM(findings=None), diff_bundle, signals, format_signals(signals), resolve)
    assert broken.source == "rule" and broken.error == "후보 JSON 파싱 실패"
    assert {(f.kind, f.summary) for f in broken.findings} == {
        ("dependency", "openai ==0.28.1 -> ==1.51.0"), ("library_usage", "openai.ChatCompletion.create 호출")}
    dependency = next(f for f in broken.findings if f.kind == "dependency")
    assert dependency.severity == "medium" and dependency.exact_tokens == ["1.51.0"]  # 메이저 경계
    assert rule_findings(signals, resolve) and review(None, diff_bundle, signals, "", resolve).source == "rule"


def test_patch_instructions_are_data_not_commands(tmp_path):
    llm = FakeLLM(findings=[])
    signals = extract_signals(bundle())
    result = review(llm, bundle(), signals, format_signals(signals), registry(tmp_path).resolve)
    assert result.findings == [] and FAKE_KEY not in llm.prompts[0]


# ---------------------------------------------------------------- 근거 검색과 해결 방법


def test_research_grounds_finding_with_library_evidence(make_client, tmp_path):
    index = build_index(make_client, tmp_path)
    llm = FakeLLM()
    researcher = build_researcher(ROOT, index, CORPUS, llm, llm, FakeEmbedder(), client=None)
    result = researcher.research(finding())
    assert result.status == "grounded", result.reason
    assert [e.issue_id for e in result.cited_evidence] == [f"{REPO}#1"]
    assert result.draft.startswith("스트리밍 타임아웃") and "확인:" in result.draft
    assert llm.calls == ["grade", "resolution"]  # 첫 시도에서 충분하면 더 검색하지 않는다
    assert "app/llm.py:5" in llm.prompts[-1] and "openai/openai-python" in llm.prompts[-1]


def test_research_without_exact_token_match_skips_llm(make_client, tmp_path):
    index = build_index(make_client, tmp_path)
    llm = FakeLLM()
    researcher = build_researcher(ROOT, index, CORPUS, llm, llm, FakeEmbedder(), client=None)
    result = researcher.research(finding(exact_tokens=["NoSuchThingError"]))
    assert result.status == "no_evidence" and "정확 토큰" in result.reason
    assert llm.calls == []  # 정확 토큰이 근거에 없으면 LLM 판정 없이 부족


def test_research_rejects_unrelated_evidence_and_unusable_draft(make_client, tmp_path):
    index = build_index(make_client, tmp_path)
    unrelated = FakeLLM(relevant="other/repo#1")
    result = build_researcher(ROOT, index, CORPUS, unrelated, unrelated, FakeEmbedder(), None).research(finding())
    assert result.status == "no_evidence" and result.reason == "테스트 판정" and len(result.attempts) == 2

    refused = FakeLLM(draft="제공된 문서에서 확인되지 않습니다.")
    result = build_researcher(ROOT, index, CORPUS, refused, refused, FakeEmbedder(), None).research(finding())
    assert result.status == "no_evidence" and "직접 관련 없다" in result.reason


def test_research_skips_general_and_respects_search_budget(make_client, tmp_path):
    index = build_index(make_client, tmp_path)
    llm = FakeLLM()
    researcher = build_researcher(ROOT, index, CORPUS, llm, llm, FakeEmbedder(), client=None, search_budget=0)
    assert researcher.research(finding(kind="general", library=None, repo=None)).status == "skipped"
    assert researcher.research(finding()).collection_skipped
    assert Researcher(researcher.pipeline).research(finding()).status == "grounded"
    researcher.pipeline.llm = None
    assert researcher.research(finding()).status == "skipped"


def test_main_prints_review_and_grounded_resolution(make_client, tmp_path, capsys):
    from agent.main import main

    index = build_index(make_client, tmp_path)
    patch = tmp_path / "changes.patch"
    patch.write_text(DIFF, encoding="utf-8")
    llm = FakeLLM(findings=[
        {"file": "app/llm.py", "line": 5, "kind": "library_usage", "library": "openai", "summary": "스트리밍 호출에 timeout 미지정",
         "concern": "APITimeoutError", "search_queries": ["stream timeout"], "exact_tokens": ["APITimeoutError"], "severity": "high"},
        {"file": "app/llm.py", "line": 4, "kind": "general", "summary": "예외 처리 없음", "severity": "low"}])
    argv = ["--diff", str(patch), "--dry-run", "--no-pypi", "--no-collect", "--target-root", str(tmp_path),
            "--cache-dir", str(tmp_path), "--index", str(index)]
    assert main(argv, env={}, llm=llm, fast_llm=llm, embedder=FakeEmbedder()) == 0
    out = capsys.readouterr().out
    assert "== 리뷰 후보 (프롬프트 4): 근거 검색 1건, 일반 1건" in out
    assert "-> 근거 있음" in out and re.search(rf"\[근거 \d+\] {REPO}#1 ", out)
    assert FAKE_KEY not in out

    assert main(argv + ["--json", "--skip-research"], env={}, llm=llm, fast_llm=llm, embedder=FakeEmbedder()) == 0
    data = json.loads(capsys.readouterr().out)
    assert [f["kind"] for f in data["review"]["findings"]] == ["library_usage", "general"] and data["research"] == []


def test_research_collects_into_agent_index_within_search_budget(make_client, tmp_path):
    from src.indexing.bm25_indexer import load_bm25_ids

    client, session = make_client(routes(), cache=False)
    index = tmp_path / "agent_index"  # 아직 아무 라이브러리도 구축하지 않은 코퍼스
    llm = FakeLLM()
    researcher = build_researcher(ROOT, index, CORPUS, llm, llm, FakeEmbedder(), client, search_budget=2)
    result = researcher.research(finding())
    assert result.status == "grounded" and [a["stage"] for a in result.attempts] == ["original", "rewritten", "collected"]
    assert result.searches == 2 and researcher.searches_used == 2  # '"APITimeoutError"', 'stream timeout'
    assert f"{REPO}#1" in {e.issue_id for e in result.cited_evidence}
    assert any(i.startswith(f"doc:{REPO}#1:") for i in load_bm25_ids(index / "sparse" / "issue_documents"))
    assert (index / "query_cache.json").exists()

    searched = len([p for p in session.paths() if p == "/search/issues"])
    second = researcher.research(finding(id="f2", search_queries=["another query"]))
    assert second.collection_skipped and len([p for p in session.paths() if p == "/search/issues"]) == searched


def test_clean_tokens_keeps_searchable_names_only():
    from agent.reviewer import clean_tokens
    raw = ["openai==1.51.0", "request_timeout=5", 'model="gpt-4o-mini"', "messages", "`ChatCompletion.create(`",
           "APITimeoutError", "id", "v2", "gpt-4o-mini"]
    assert clean_tokens(raw, 10) == ["1.51.0", "request_timeout", "ChatCompletion.create", "APITimeoutError", "v2",
                                     "gpt-4o-mini"]
