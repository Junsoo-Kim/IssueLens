"""retrieve, generate 노드 (요구사항 F2007, 기획서 8.2절).

retrieve는 근거가 충분하다고 판정될 때까지 최대 max_attempts번 검색한다.
    1. 원래 질문으로 검색
    2. 프롬프트 1이 만든 영어 검색어로 다시 검색 (한국어 질문, 표현 차이 대응)
    3. 질문 시점 외부 수집으로 관련 이슈를 색인한 뒤 다시 검색
generate는 근거가 충분하면 프롬프트 3으로 답하고 인용 번호를 검증한다. 부족하면 답하지 않고 확인되지 않는다고 알리며,
사용자가 허용한 경우에만 RAG 미사용 일반 답변을 분리해 덧붙인다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from langchain_core.runnables import RunnableConfig

from src.collectors.query_time import QueryTimeCollector, collection_queries
from src.graph.state import RAGState
from src.indexing.ids import issue_record_id
from src.prompts.answer_generator import NO_EVIDENCE_MESSAGE, generate_answer, generate_general_answer
from src.prompts.evidence_grader import grade_evidence
from src.prompts.llm import LLM, LLMUnavailable
from src.prompts.query_extractor import extract_queries
from src.retrieval.hybrid import HybridRetriever, RetrievalResult
from src.retrieval.repository_router import issue_id_of_record
from src.services.citation_validator import check_citations, remove_citations

NEAREST_ISSUES = 3


def report(config: RunnableConfig | None, message: str) -> None:
    """화면의 진행 상태 표시용. ask(progress=...)로 넘긴 콜백이 있을 때만 부른다."""
    callback = ((config or {}).get("configurable") or {}).get("progress")
    if callback:
        callback(message)


@dataclass
class PipelineSettings:
    max_attempts: int = 3
    evidence_chars: int = 1200
    citation_retries: int = 1
    collection_queries: int = 3


class RAGPipeline:
    def __init__(self, retriever: HybridRetriever, llm: LLM | None, repositories: list[dict],
                 collector: QueryTimeCollector | None = None, settings: PipelineSettings | None = None):
        self.retriever = retriever
        self.llm = llm
        self.repositories = repositories
        self.collector = collector
        self.settings = settings or PipelineSettings()

    # ------------------------------------------------------------ retrieve

    def _selected_issue(self, issue_id: str | None) -> dict | None:
        if not issue_id:
            return None
        got =self.retriever.issue_collection.get(ids=[issue_record_id(issue_id)], include=["metadatas"])
        title = got["metadatas"][0].get("title", "") if got["metadatas"] else ""
        return {"issue_id": issue_id, "repo": issue_id.rsplit("#", 1)[0], "title": title}

    def _attempt(self, stage: str, search_text: str, result: RetrievalResult, question: str,
                 config: RunnableConfig | None = None) -> dict:
        report(config, "근거 정리 중")
        grade = grade_evidence(self.llm, question, result.evidence, result.assessment)
        return {
            "stage": stage,
            "search_text": search_text,
            "route": {"repos": result.route.repos, "reason": result.route.reason, "confidence": result.route.confidence},
            "top_issues": [issue_id_of_record(h.id) for h in result.issues[:8]],
            "evidence_count": len(result.evidence),
            "assessment": result.assessment,
            "grade": grade.as_dict(),
            "_result": result,
        }

    def retrieve(self, state: RAGState, config: RunnableConfig = None) -> dict:
        started = time.perf_counter()
        question, repo, selected_id = state["question"], state.get("repo"), state.get("selected_issue_id")
        selected = self._selected_issue(selected_id)

        report(config, "검색 조건 분석 중")
        plan = extract_queries(self.llm, question, self.repositories, selected)
        timings = {"query_plan": time.perf_counter() - started}
        english = " ".join([*plan.search_queries, *plan.exact_tokens])
        plan_repo = repo or (plan.candidate_repos[0] if len(plan.candidate_repos) == 1 else None)

        attempts, collection = [], None
        stages = [("original", question, repo, None), ("rewritten", english, plan_repo, None)]
        for index in range(self.settings.max_attempts):
            stage_started = time.perf_counter()
            if index < len(stages):
                stage, text, stage_repo, extra = stages[index]
                if stage == "rewritten" and plan.source == "rule":
                    continue  # 프롬프트 1이 규칙 기반으로 대체되면 검색어가 원래 질문과 같아 같은 검색을 반복하게 된다
            elif self.collector is not None:
                # 앞 시도의 라우팅 결과를 수집 대상 저장소로 쓴다
                repos = ([repo] if repo else plan.candidate_repos or attempts[-1]["route"]["repos"])[:2]
                report(config, "캐시 확인 및 외부 이슈 수집 중")
                queries = collection_queries(plan.search_queries, plan.exact_tokens, self.settings.collection_queries)
                collection_result = self.collector.collect(question, queries, repos, self.llm)
                collection = collection_result.as_dict()
                if not collection_result.available:
                    timings["attempt_collect"] = time.perf_counter() - stage_started
                    break
                stage, text, stage_repo, extra = "collected", english, plan_repo, collection_result.available
            else:
                break
            report(config, f"관련 문서 검색 중 ({len(attempts) + 1}차 시도)")
            result = self.retriever.retrieve(text, repo=stage_repo, selected_issue_id=selected_id, extra_issue_ids=extra)
            attempts.append(self._attempt(stage, text, result, question, config))
            timings[f"attempt_{stage}"] = time.perf_counter() - stage_started
            if attempts[-1]["grade"]["sufficient"]:
                break

        final = next((a for a in reversed(attempts) if a["grade"]["sufficient"]), None)
        # 근거 수가 같으면 뒤 시도를 고른다. 외부 수집 후 시도는 새로 수집한 이슈를 앞에 두므로 가까운 사례로 더 알맞다
        best = final or max(attempts, key=lambda a: (a["evidence_count"], attempts.index(a)), default=None)
        result: RetrievalResult | None = best["_result"] if best else None
        nearest = [{"issue_id": issue_id_of_record(h.id), "url": f"https://github.com/{issue_id_of_record(h.id).replace('#', '/issues/')}"}
                   for h in (result.issues[:NEAREST_ISSUES] if result else [])]
        timings["retrieve_total"] = time.perf_counter() - started
        return {
            "query_plan": plan.as_dict(),
            "attempts": [{k: v for k, v in a.items() if k != "_result"} for a in attempts],
            "collection": collection,
            "evidence": result.evidence if result else [],
            "sufficient": final is not None,
            "grade": best["grade"] if best else {},
            "nearest_issues": nearest,
            "retrieval_warnings": list(dict.fromkeys(w for a in attempts for w in a["_result"].trace.get("warnings", []))),
            "timings": timings,
        }

    # ------------------------------------------------------------ generate

    def _grounded_answer(self, question: str, evidence: list) -> tuple[str, dict, list[str]]:
        numbers = {e.number for e in evidence}
        warnings = []
        answer = generate_answer(self.llm, question, evidence, self.settings.evidence_chars)
        check = check_citations(answer, numbers)
        for _ in range(self.settings.citation_retries):
            if check.valid:
                break
            feedback = (f"이전 답변이 없는 근거 번호 {check.invalid}를 인용했습니다. "
                        f"사용 가능한 번호는 {sorted(numbers)}뿐입니다.")
            answer = generate_answer(self.llm, question, evidence, self.settings.evidence_chars, feedback)
            check = check_citations(answer, numbers)
        if not check.valid:
            answer = remove_citations(answer, check.invalid)
            warnings.append(f"존재하지 않는 근거 번호 {check.invalid}를 답변에서 지웠습니다.")
        return answer, {"cited": check.cited, "invalid": check.invalid}, warnings

    def generate(self, state: RAGState, config: RunnableConfig = None) -> dict:
        started = time.perf_counter()
        question = state["question"]
        evidence = state.get("evidence", [])
        warnings: list[str] = list(state.get("retrieval_warnings", []))
        answer, cited, check, from_evidence, general = NO_EVIDENCE_MESSAGE, [], {"cited": [], "invalid": []}, False, None

        try:
            if state.get("sufficient") and evidence and self.llm is not None:
                report(config, "답변 생성 중")
                answer, check, answer_warnings = self._grounded_answer(question, evidence)
                warnings.extend(answer_warnings)
                from_evidence = answer.strip() != NO_EVIDENCE_MESSAGE
                cited = [e for e in evidence if e.number in check["cited"]]
                if from_evidence and not cited:
                    warnings.append("답변에 인용된 근거가 없습니다. 내용을 근거로 확인할 수 없습니다.")
                if not from_evidence:
                    warnings.append("근거 판정은 통과했지만, 답변 단계에서 근거가 질문의 오류와 직접 맞지 않는다고 보고 "
                                    "답하지 않았습니다. 아래 사례는 참고만 하세요.")
            elif self.llm is None:
                warnings.append("LLM API 키가 없어 답변을 생성하지 못했습니다. .env의 OPENAI_API_KEY를 확인하세요.")
            if not from_evidence and state.get("allow_general_answer") and self.llm is not None:
                general = generate_general_answer(self.llm, question)
        except LLMUnavailable as exc:
            warnings.append(f"답변 생성에 실패했습니다: {exc}")
            answer, from_evidence, cited = NO_EVIDENCE_MESSAGE, False, []

        timings = {**state.get("timings", {}), "generate": time.perf_counter() - started}
        return {
            "answer": answer,
            "answered_from_evidence": from_evidence,
            "cited_evidence": cited,
            "citation_check": check,
            "general_answer": general,
            "warnings": warnings,
            "timings": timings,
        }
