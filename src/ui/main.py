"""Streamlit 화면 조립. app.py가 run()을 부른다. 테스트는 root와 factory를 바꿔 가짜 LLM으로 띄운다."""
from __future__ import annotations

from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from src.config import PROJECT_ROOT
from src.ui.chat import chat_tab, diagnosis_tab, explore_tab
from src.ui.sidebar import render_sidebar
from src.ui.state import INTERNAL_ERROR, AssistantFactory, chroma_exists, environment_notices, index_status, \
    load_assistant, logger

MISSING_RAG1 = "검색 색인이 없습니다. 먼저 이슈 메타데이터 색인 스크립트를 실행해 주세요: python scripts/build_indexes.py"
MISSING_RAG2 = "선택 이슈의 연결 문서 색인(RAG 2)이 비어 있습니다. 연결 문서를 수집하거나 색인해 주세요."


def run(root: Path | None = None, factory: AssistantFactory | None = None) -> None:
    root = Path(root or PROJECT_ROOT)
    st.set_page_config(page_title="IssueLens", layout="wide")
    load_dotenv(root / ".env")
    st.title("IssueLens")
    st.caption("운영 이슈 이력 기반 장애 진단 및 해결 지원 RAG 챗봇")

    if not chroma_exists(root):
        st.error(MISSING_RAG1)
        st.stop()
    try:
        assistant = load_assistant(str(root), factory)
        status = index_status(assistant, root)
    except Exception:  # noqa: BLE001
        logger.exception("검색 색인 로딩 실패")
        st.error(INTERNAL_ERROR)
        st.stop()
    if not status.ready:
        st.error(MISSING_RAG1)
        st.stop()
    if status.chunks == 0:
        st.warning(MISSING_RAG2)
    for level, message in environment_notices(assistant):
        getattr(st, level)(message)

    choice = render_sidebar(assistant, status, root)
    chat, explore, diagnosis = st.tabs(["질의응답", "조건 탐색", "로그 진단"])
    with chat:
        chat_tab(assistant, choice)
    with explore:
        explore_tab(assistant, choice, root)
    with diagnosis:
        diagnosis_tab(assistant, choice)
