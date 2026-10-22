"""화면 상태: 리소스 로딩, 색인·환경 점검, 이슈별 대화 분리 (요구사항 F2009, F2012)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import streamlit as st

from src.config import load_yaml
from src.services.assistant import Assistant, build_assistant, resolve_paths

logger = logging.getLogger("issuelens.ui")

ALL_REPOS = "전체 저장소"
NO_ISSUE = "선택 안 함"
INTERNAL_ERROR = "처리 중 오류가 발생했습니다. 상세 추적 정보는 서버 로그에서 확인해 주세요."
RATE_LIMIT = "GitHub API 호출 한도에 도달했습니다. 잠시 후 다시 시도해 주세요. 그동안은 이미 색인된 데이터로만 답합니다."
NO_RESULT = "현재 데이터에서 관련 근거를 찾지 못했습니다. 조건을 줄이거나 다른 표현으로 질문해 주세요."

AssistantFactory = Callable[[Path], Assistant]


@dataclass
class IndexStatus:
    ready: bool  # RAG 1이 있어야 화면을 쓸 수 있다
    issues: int
    chunks: int
    query_time_documents: int


def chroma_exists(root: Path) -> bool:
    """빈 폴더에 Chroma 클라이언트를 열면 빈 DB가 새로 생기므로, 열기 전에 파일이 있는지 본다."""
    chroma_dir = resolve_paths(root, load_yaml(root / "configs/app.yaml")).get("chroma_dir")
    return bool(chroma_dir) and (chroma_dir / "chroma.sqlite3").exists()


@st.cache_resource(show_spinner="검색 색인을 불러오는 중")
def load_assistant(root: str, _factory: AssistantFactory | None = None) -> Assistant:
    """세션 사이에 공유한다. 색인과 클라이언트를 질문마다 다시 열지 않는다."""
    return (_factory or build_assistant)(Path(root))


def index_status(assistant: Assistant, root: Path) -> IndexStatus:
    counts = assistant.retriever.counts()
    path = resolve_paths(root, assistant.app_config)["processed_dir"] / "query_time_documents.jsonl"
    documents = 0
    if path.exists():
        with open(path, encoding="utf-8") as f:
            documents = sum(1 for line in f if line.strip())
    return IndexStatus(counts["issues"] > 0, counts["issues"], counts["chunks"], documents)


def environment_notices(assistant: Assistant) -> list[tuple[str, str]]:
    """(수준, 문구). 키 값은 보여 주지 않고 있는지만 알린다 (NF2003, NF2004)."""
    notices = []
    if assistant.pipeline.llm is None:
        notices.append(("warning", "답변 생성 API 키가 설정되지 않았습니다. .env의 OPENAI_API_KEY를 확인해 주세요. "
                                   "검색과 근거 목록만 보여 줍니다."))
    if assistant.pipeline.collector is not None and not os.getenv("GITHUB_TOKEN"):
        notices.append(("info", "GitHub Token이 없어 질문 시점 수집은 공개 API의 제한된 조회만 가능합니다. 환경변수를 확인해 주세요."))
    return notices


def conversation_key(issue_id: str | None, repo: str | None) -> str:
    """선택 이슈마다 대화를 따로 둔다. 이슈가 없으면 저장소 범위로 나눈다."""
    return f"issue:{issue_id}" if issue_id else f"repo:{repo or 'all'}"


def conversation(key: str) -> list[dict]:
    return st.session_state.setdefault("conversations", {}).setdefault(key, [])


def reset_conversation(key: str) -> None:
    st.session_state.setdefault("conversations", {})[key] = []


def select_issue(issue_id: str | None, repo: str | None = None) -> None:
    """버튼 콜백. 위젯이 만들어지기 전에 선택값을 바꿔야 해서 on_click으로만 부른다."""
    if repo:
        st.session_state["repo_filter"] = repo
    st.session_state["selected_issue"] = issue_id or NO_ISSUE


def is_rate_limited(messages: list[str]) -> bool:
    return any(code in m for m in messages for code in (" 403 ", " 429 ", ": 403", ": 429", "rate limit"))
