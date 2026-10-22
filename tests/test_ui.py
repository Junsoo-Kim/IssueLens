"""Streamlit 화면 테스트 (요구사항 F2009, F2011, F2010, F2012, NF2004). AppTest로 실제 화면 코드를 실행한다."""
from __future__ import annotations

import shutil

import pytest
from streamlit.testing.v1 import AppTest

from src.config import PROJECT_ROOT
from src.ui.main import MISSING_RAG1
from tests.test_graph import KC, rag_root  # noqa: F401  (픽스처)
from tests.test_retrieval import index_dirs  # noqa: F401  (픽스처)

HARNESS = str(PROJECT_ROOT / "tests/ui/app_harness.py")
TIMEOUT = 60


def start(monkeypatch, root, llm="fake") -> AppTest:
    monkeypatch.setenv("ISSUELENS_TEST_ROOT", str(root))
    monkeypatch.setenv("ISSUELENS_TEST_LLM", llm)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    at = AppTest.from_file(HARNESS, default_timeout=TIMEOUT)
    at.run()
    assert not at.exception, at.exception
    return at


def texts(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [e.value for kind in (at.error, at.warning, at.info, at.success) for e in kind]
    return "\n".join(str(p) for p in parts)


def ask(at: AppTest, question: str) -> AppTest:
    at.chat_input[0].set_value(question).run()
    assert not at.exception, at.exception
    return at


def test_missing_index_shows_guide(monkeypatch, tmp_path):
    root = tmp_path / "empty"
    shutil.copytree(PROJECT_ROOT / "configs", root / "configs")
    at = start(monkeypatch, root)
    assert [e.value for e in at.error] == [MISSING_RAG1]


def test_status_and_missing_key_notices(monkeypatch, rag_root):  # noqa: F811
    at = start(monkeypatch, rag_root, llm="none")
    metrics = {m.label: m.value for m in at.metric}
    assert int(metrics["RAG 1 이슈"]) > 0 and int(metrics["RAG 2 청크"].replace(",", "")) > 0
    assert any("OPENAI_API_KEY" in w.value for w in at.warning)
    ask(at, "TokenVerificationException after upgrade")
    assert any("LLM API 키가 없어" in w.value for w in at.warning)  # 키 없이도 화면이 멈추지 않는다


def test_chat_answers_with_evidence_and_isolates_issues(monkeypatch, rag_root):  # noqa: F811
    at = start(monkeypatch, rag_root)
    ask(at, "TokenVerificationException after upgrade")
    body = texts(at)
    assert "[근거 1]" in body and "답변에 사용한 근거" in body
    assert "기술 조사 보조 결과" in body  # 안전 안내
    assert "당신은" not in body and "Traceback" not in body  # 프롬프트 전문과 스택 트레이스는 보이지 않는다

    at.selectbox(key="selected_issue").set_value(f"{KC}#3").run()
    assert not at.chat_message  # 다른 이슈로 바꾸면 이전 대화가 보이지 않는다
    assert f"{KC}#3" in texts(at)  # 선택 이슈 헤더
    at.selectbox(key="selected_issue").set_value("선택 안 함").run()
    assert len(at.chat_message) == 2  # 원래 범위로 돌아오면 대화가 남아 있다

    at.button(key="new_conversation").click().run()
    assert not at.chat_message


def test_explore_tab_shows_matching_cards_and_selects_issue(monkeypatch, rag_root):  # noqa: F811
    at = start(monkeypatch, rag_root)
    at.text_input[0].set_value("keycloak 26 업그레이드 후 로그인 토큰 검증 실패 버그 찾아줘")
    at.button(key="FormSubmitter:explore-탐색").click().run()
    assert not at.exception, at.exception
    body = texts(at)
    assert f"{KC}#2" in body and "26.0.1 업그레이드 후 토큰 검증 실패" in body
    assert f"{KC}#1)" not in body and f"{KC}#3)" not in body

    at.button(key=f"pick:{KC}#2").click().run()
    assert at.selectbox(key="selected_issue").value == f"{KC}#2"


def test_diagnosis_tab_shows_signals_and_answer(monkeypatch, rag_root):  # noqa: F811
    at = start(monkeypatch, rag_root)
    at.text_area[0].set_value("ERROR org.keycloak.exceptions.TokenVerificationException: Invalid token\n"
                              "    at org.keycloak.TokenVerifier.verify(TokenVerifier.java:123)")
    at.button(key="FormSubmitter:diagnose-진단").click().run()
    assert not at.exception, at.exception
    body = texts(at)
    assert "가장 유사한 종료 이슈" in body and "근거로 사용" in body and "[근거 1]" in body
