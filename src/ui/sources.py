"""답변, 근거 카드, 출처 유형 배지, 검색 과정 요약 (요구사항 F2006~F2008, F2012).

화면에는 프롬프트 전문, API 키, 스택 트레이스를 보여 주지 않는다 (NF2004).
"""
from __future__ import annotations

import re

import streamlit as st

from src.prompts.answer_generator import NO_EVIDENCE_MESSAGE, SOURCE_TYPE_LABELS
from src.retrieval.evidence_selector import chunk_body
from src.ui.state import NO_RESULT, RATE_LIMIT, is_rate_limited

SNIPPET_CHARS = 300
BADGE_COLORS = {"issue_metadata": "gray", "issue_body": "blue", "comment": "green", "pull_request": "violet",
                "pull_patch": "violet", "official_doc": "orange"}
STAGE_LABELS = {"original": "원래 질문", "rewritten": "재작성 검색어", "collected": "외부 수집 후"}


MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-!|>~<])")


def snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """원문 일부. 이슈 본문의 마크다운(제목, 링크, 코드)이 화면에서 그대로 렌더링되지 않게 기호를 이스케이프한다."""
    body = " ".join(chunk_body(text).split())
    body = body[:limit] + ("…" if len(body) > limit else "")
    return MARKDOWN_SPECIAL.sub(r"\\\1", body)


def key_values(rows: dict) -> None:
    """조건·신호 같은 짧은 값 목록을 '- **이름**: 값' 형태로 보여 준다."""
    lines = []
    for name, value in rows.items():
        if isinstance(value, (list, tuple)):
            value = ", ".join(f"`{v}`" for v in value) if value else "-"
        lines.append(f"- **{name}**: {value if value not in (None, '') else '-'}")
    st.markdown("\n".join(lines))


def badge(label: str, color: str) -> str:
    return f":{color}-badge[{label}]"


def evidence_card(e) -> None:
    with st.container(border=True):
        label = SOURCE_TYPE_LABELS.get(e.document_type, e.document_type)
        badges = [badge(label, BADGE_COLORS.get(e.document_type, "gray"))]
        if e.author_role == "maintainer":
            badges.append(badge("유지보수자", "red"))
        st.markdown(f"**[근거 {e.number}]** {' '.join(badges)}")
        st.markdown(f"[{e.issue_id} · {e.title}]({e.source_url})")
        st.caption(snippet(e.text))


def render_process(state: dict) -> None:
    """검색 과정 요약. 프롬프트 1 결과와 시도별 판정, 질문 시점 수집 결과만 보여 준다."""
    with st.expander("검색 과정", expanded=False):
        plan = state.get("query_plan") or {}
        st.markdown(f"**검색어 추출** ({plan.get('source', '-')}, 유형 {plan.get('query_type', '-')})")
        key_values({"검색어": plan.get("search_queries") or [], "정확 토큰": plan.get("exact_tokens") or [],
                    "저장소 후보": plan.get("candidate_repos") or []})
        rows = [{"시도": STAGE_LABELS.get(a["stage"], a["stage"]), "검색 저장소": ", ".join(a["route"]["repos"]),
                 "근거 수": a["evidence_count"], "판정": "충분" if a["grade"]["sufficient"] else "부족",
                 "이유": a["grade"]["reason"]} for a in state.get("attempts", [])]
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
        collection = state.get("collection")
        if collection:
            selection = collection.get("selection") or {}
            st.markdown("**질문 시점 외부 수집**")
            key_values({
                "캐시 적중": [h["query"] for h in collection.get("cache_hits", [])],
                "GitHub 검색": [f"{s['query']} ({s['results']}건)" for s in collection.get("searched", [])],
                "후보 수": len(collection.get("candidates", [])),
                "선택": selection.get("selected_ids") or [],
                "새로 색인한 이슈": collection.get("collected") or [],
            })
        timings = state.get("timings") or {}
        if timings:
            st.caption("처리 시간: " + " / ".join(f"{k} {v:.1f}s" for k, v in timings.items()))


def render_answer(state: dict, safety_notice: str) -> None:
    st.markdown(state.get("answer") or NO_EVIDENCE_MESSAGE)
    cited = state.get("cited_evidence") or []
    if cited:
        st.markdown("**답변에 사용한 근거**")
        for e in cited:
            evidence_card(e)
    elif not state.get("answered_from_evidence"):
        st.info(NO_RESULT)
        nearest = state.get("nearest_issues") or []
        if nearest:
            st.caption("가장 가까운 사례 (관련도 낮음, 근거로 쓰지 않음)")
            for n in nearest:
                st.markdown(f"- [{n['issue_id']}]({n['url']})")
    if state.get("general_answer"):
        with st.container(border=True):
            st.markdown("**참고: RAG 미사용 일반 답변**")
            st.caption("수집된 이슈 데이터를 사용하지 않은 일반 지식 답변입니다. 근거 번호가 없으며, 적용 전 공식 문서로 검증하세요.")
            st.markdown(state["general_answer"])
    collection_errors = (state.get("collection") or {}).get("errors", [])
    if is_rate_limited(collection_errors):
        st.warning(RATE_LIMIT)
    for warning in state.get("warnings", []):
        st.warning(warning)
    render_process(state)
    st.caption(safety_notice)
