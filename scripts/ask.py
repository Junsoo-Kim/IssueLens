"""질문 하나를 LangGraph(retrieve -> generate)로 처리하고 단계별 결과를 출력한다.

    python scripts/ask.py "Keycloak 업그레이드 후 토큰 검증이 실패해"
    python scripts/ask.py "이 문제 다른 버전에도 있나요?" --issue keycloak/keycloak#12345
    python scripts/ask.py "..." --allow-general        # 근거가 없으면 RAG 미사용 일반 답변을 분리해 덧붙임
    python scripts/ask.py "..." --no-collect           # 질문 시점 외부 수집을 끔 (GitHub API 호출 없음)
    python scripts/ask.py --graph                      # 그래프 구조 출력 (요구사항 F2007 증빙)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.graph.workflow import graph_diagram  # noqa: E402
from src.prompts.answer_generator import source_label  # noqa: E402
from src.retrieval.evidence_selector import chunk_body  # noqa: E402
from src.services.assistant import build_assistant  # noqa: E402

SNIPPET_CHARS = 160
STAGE_LABELS = {"original": "원래 질문", "rewritten": "재작성 검색어", "collected": "외부 수집 후"}


def snippet(text: str) -> str:
    body = " ".join(chunk_body(text).split())
    return body[:SNIPPET_CHARS] + ("…" if len(body) > SNIPPET_CHARS else "")


def format_state(state: dict, safety_notice: str) -> str:
    plan = state.get("query_plan", {})
    lines = [
        "== 프롬프트 1 (검색어 추출)",
        f"  출처: {plan.get('source')}{' (' + plan['error'] + ')' if plan.get('error') else ''} / 유형: {plan.get('query_type')}",
        f"  검색어: {plan.get('search_queries')}",
        f"  정확 토큰: {plan.get('exact_tokens') or '-'} / 저장소 후보: {plan.get('candidate_repos') or '-'}",
        "",
        "== 검색 시도",
    ]
    for i, attempt in enumerate(state.get("attempts", []), start=1):
        grade = attempt["grade"]
        lines.append(f"  {i}. {STAGE_LABELS.get(attempt['stage'], attempt['stage'])}: 라우팅 {attempt['route']['repos']} / "
                     f"근거 {attempt['evidence_count']}건 / 판정 {'충분' if grade['sufficient'] else '부족'}"
                     f" ({grade['source']}: {grade['reason']})")
    collection = state.get("collection")
    if collection:
        selection = collection.get("selection") or {}
        lines += [
            "",
            "== 질문 시점 외부 수집 (프롬프트 2 선별)",
            f"  캐시 적중(재수집 안 함): {[h['query'] for h in collection['cache_hits']] or '-'}",
            f"  GitHub 검색: {[(s['query'], s['results']) for s in collection['searched']] or '-'}",
            f"  후보 {len(collection['candidates'])}건 → 선택 {selection.get('selected_ids') or '-'} "
            f"/ 탈락 {len(selection.get('rejected_ids', []))}건 ({selection.get('source', '-')})",
            f"  새로 색인한 이슈 {collection['collected'] or '-'} (청크 {collection['indexed_chunks']}개)",
        ]
        lines += [f"  오류: {e}" for e in collection.get("errors", [])]

    lines += ["", "== 답변", state.get("answer", "")]
    cited = state.get("cited_evidence", [])
    if cited:
        lines += ["", "== 답변에 사용한 근거"]
        for e in cited:
            lines.append(f"[근거 {e.number}] {source_label(e)} · {e.issue_id} · {e.title}")
            lines.append(f"    {e.source_url}")
            lines.append(f"    {snippet(e.text)}")
    elif not state.get("answered_from_evidence"):
        nearest = state.get("nearest_issues", [])
        if nearest:
            lines += ["", "가장 가까운 사례 (관련도 낮음, 근거로 쓰지 않음):"]
            lines += [f"  - {n['issue_id']} {n['url']}" for n in nearest]
    if state.get("general_answer"):
        lines += ["", "─── 참고: RAG 미사용 일반 답변 ───",
                  "※ 수집된 이슈 데이터를 사용하지 않은 일반 지식 답변입니다. 근거 번호가 없으며, 적용 전 공식 문서로 검증하세요.",
                  state["general_answer"]]
    lines += [f"\n주의: {w}" for w in state.get("warnings", [])]
    timings = state.get("timings", {})
    lines += ["", "처리 시간: " + " / ".join(f"{k} {v:.1f}s" for k, v in timings.items()), "", safety_notice]
    return "\n".join(lines)


def main(argv: list[str] | None = None, **overrides) -> int:
    parser = argparse.ArgumentParser(description="IssueLens 질의응답 (LangGraph)")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--issue", help="선택 이슈 (owner/repo#번호)")
    parser.add_argument("--repo", help="저장소 고정 (owner/repo)")
    parser.add_argument("--allow-general", action="store_true", help="근거 부족 시 RAG 미사용 일반 답변 덧붙이기")
    parser.add_argument("--no-collect", action="store_true", help="질문 시점 외부 수집 끄기")
    parser.add_argument("--graph", action="store_true", help="그래프 구조(Mermaid)만 출력")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    load_dotenv(ROOT / ".env")

    assistant = build_assistant(overrides.pop("root", ROOT), enable_collection=False if args.no_collect else None, **overrides)
    if args.graph:
        print(graph_diagram(assistant.graph))
        return 0
    if not args.question:
        parser.error("질문을 입력하세요")
    if assistant.retriever.issue_collection.count() == 0:
        print("색인이 비어 있습니다. 먼저 scripts/build_indexes.py를 실행하세요.", file=sys.stderr)
        return 2
    state = assistant.ask(args.question, selected_issue_id=args.issue, repo=args.repo,
                          allow_general_answer=True if args.allow_general else None)
    print(format_state(state, assistant.app_config.get("safety_notice", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
