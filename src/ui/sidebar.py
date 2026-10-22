"""사이드바: 저장소 선택, 색인 현황, 이슈 선택, 새 대화, 답변 설정, 검색 설정 표시 (기획서 10.1절)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from src.config import load_yaml
from src.services.assistant import Assistant
from src.ui.state import ALL_REPOS, NO_ISSUE, IndexStatus, conversation_key, reset_conversation


@dataclass
class SidebarChoice:
    repo: str | None
    issue_id: str | None
    allow_general: bool


def _issue_label(issue_id: str, titles: dict[str, str]) -> str:
    if issue_id == NO_ISSUE:
        return NO_ISSUE
    return f"#{issue_id.rsplit('#', 1)[1]} {titles.get(issue_id, '')}"[:90]


def render_sidebar(assistant: Assistant, status: IndexStatus, root: Path) -> SidebarChoice:
    repos = [r["repo"] for r in assistant.pipeline.repositories]
    with st.sidebar:
        st.header("IssueLens")
        repo_choice = st.selectbox("저장소", [ALL_REPOS, *repos], key="repo_filter")
        repo = None if repo_choice == ALL_REPOS else repo_choice

        left, right = st.columns(2)
        left.metric("RAG 1 이슈", f"{status.issues:,}")
        right.metric("RAG 2 청크", f"{status.chunks:,}")
        if status.query_time_documents:
            st.caption(f"질문 시점 수집 문서 {status.query_time_documents:,}건 포함")

        issues = assistant.retriever.list_issues(repo)
        titles = {m["issue_id"]: m.get("title", "") for m in issues}
        options = [NO_ISSUE, *titles]
        selected = st.session_state.get("selected_issue")
        if selected and selected not in options:  # 다른 저장소의 이슈가 남아 있으면 선택을 푼다
            st.session_state["selected_issue"] = NO_ISSUE
        choice = st.selectbox(f"이슈 선택 ({len(issues):,}건)", options, key="selected_issue",
                              format_func=lambda i: _issue_label(i, titles))
        issue_id = None if choice == NO_ISSUE else choice

        key = conversation_key(issue_id, repo)
        st.button("새 대화", key="new_conversation", on_click=reset_conversation, args=(key,), width="stretch",
                  help="현재 이슈(또는 저장소)의 대화만 비웁니다. 다른 이슈의 대화는 그대로 둡니다.")

        default_general = assistant.app_config.get("rag", {}).get("allow_general_answer", False)
        allow_general = st.toggle("근거가 없으면 일반 답변도 보기", value=default_general,
                                  help="RAG 미사용 답변을 별도 섹션으로 덧붙입니다. 근거 번호가 없습니다.")

        with st.expander("검색 설정"):
            config = load_yaml(root / "configs/retrieval.yaml")
            retrieval = config.get("retrieval", {})
            st.write({
                "검색 방식": "Hybrid (BM25 + Dense, RRF)",
                "임베딩": config.get("embedding", {}).get("model"),
                "이슈 Top-k": retrieval.get("issue", {}).get("rerank_top_k"),
                "근거 최대 수": retrieval.get("document", {}).get("final_evidence_limit"),
                "청크 크기/겹침": {k: f"{v['chunk_size']}/{v['chunk_overlap']}" for k, v in config.get("chunking", {}).items()},
                "최대 검색 시도": assistant.pipeline.settings.max_attempts,
                "질문 시점 수집": "켜짐" if assistant.pipeline.collector is not None else "꺼짐",
            })
    return SidebarChoice(repo, issue_id, allow_general)
