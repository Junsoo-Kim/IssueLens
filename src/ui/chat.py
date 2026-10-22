"""메인 화면 탭: 질의응답, 조건 탐색, 로그 진단 (요구사항 F2009~F2012, 기획서 10.2절)."""
from __future__ import annotations

import streamlit as st

from src.config import load_yaml
from src.services.assistant import Assistant
from src.services.incident_diagnoser import diagnose
from src.services.issue_explorer import explore_issues
from src.ui.sidebar import SidebarChoice
from src.ui.sources import key_values, render_answer
from src.ui.state import INTERNAL_ERROR, conversation, conversation_key, logger, select_issue

ISSUE_TYPE_LABELS = {"bug": "버그", "question": "질문", "feature": "기능", "task": "작업", "test": "테스트", "unknown": "미분류"}


def _run(label: str, work):
    """처리 단계를 상태 상자에 보여 주며 실행한다. 예외는 로그에만 남기고 화면에는 일반 문구만 보인다."""
    with st.status(label, expanded=False) as status:
        def progress(message: str) -> None:
            status.update(label=message)
            status.write(message)
        try:
            result = work(progress)
        except Exception:  # noqa: BLE001  화면에 스택 트레이스를 보이지 않는다 (NF2004)
            logger.exception("질문 처리 실패")
            status.update(label="오류", state="error")
            st.error(INTERNAL_ERROR)
            return None
        status.update(label="처리 완료", state="complete")
        return result


def issue_header(assistant: Assistant, issue_id: str) -> None:
    metadata = assistant.retriever.issue_metadata(issue_id) or {}
    with st.container(border=True):
        st.markdown(f"#### [{issue_id}]({metadata.get('source_url', '')}) {metadata.get('title', '')}")
        labels = [l for l in (metadata.get("labels_csv") or "").split(",") if l]
        st.caption(" · ".join([
            f"유형 {ISSUE_TYPE_LABELS.get(metadata.get('issue_type'), metadata.get('issue_type'))}",
            f"상태 {metadata.get('state')} ({metadata.get('state_reason')})",
            f"라벨 {', '.join(labels) or '-'}",
            f"종료 {str(metadata.get('closed_at') or '-')[:10]}",
        ]))


def chat_tab(assistant: Assistant, choice: SidebarChoice) -> None:
    if choice.issue_id:
        issue_header(assistant, choice.issue_id)
        st.caption("선택한 이슈의 메타데이터와 본문을 항상 근거에 포함합니다. 이슈를 바꾸면 대화가 따로 저장됩니다.")
    else:
        scope = choice.repo or "전체 저장소"
        st.caption(f"검색 범위: {scope}. 사이드바나 조건 탐색 탭에서 이슈를 고르면 그 이슈 기준으로 대화합니다.")

    messages = conversation(conversation_key(choice.issue_id, choice.repo))
    safety = assistant.app_config.get("safety_notice", "")
    history = st.container()  # 입력창보다 위에 대화를 쌓는다
    for message in messages:
        with history, st.chat_message(message["role"]):
            if message["role"] == "user":
                st.markdown(message["content"])
            elif message.get("state"):
                render_answer(message["state"], safety)
            else:
                st.error(message["content"])

    question = st.chat_input("장애 증상, 오류 메시지, 궁금한 점을 입력하세요")
    if not question:
        return
    # 처리 중에 이슈를 바꾸면 Streamlit이 실행을 멈추고 다시 그린다. 질문은 답과 함께 저장해 답 없는 질문이 남지 않게 한다
    with history, st.chat_message("user"):
        st.markdown(question)
    with history, st.chat_message("assistant"):
        state = _run("검색 조건 분석 중", lambda progress: assistant.ask(
            question, selected_issue_id=choice.issue_id, repo=None if choice.issue_id else choice.repo,
            allow_general_answer=choice.allow_general, progress=progress))
        if state is None:
            messages.extend([{"role": "user", "content": question}, {"role": "assistant", "content": INTERNAL_ERROR}])
            return
        render_answer(state, safety)
    messages.extend([{"role": "user", "content": question}, {"role": "assistant", "state": state}])


def explore_tab(assistant: Assistant, choice: SidebarChoice, root) -> None:
    st.caption("예: Keycloak 26 버전 업그레이드 후 토큰 검증이 실패한 종료 버그를 찾아줘")
    with st.form("explore"):
        question = st.text_input("찾을 이슈 조건")
        submitted = st.form_submit_button("탐색")
    if submitted and question.strip():
        categories = load_yaml(root / "configs/repositories.yaml").get("categories", {})
        result = _run("조건 분석 및 이슈 검색 중", lambda progress: explore_issues(
            assistant.pipeline.llm, assistant.retriever, question, assistant.pipeline.repositories, categories,
            repo=choice.repo))
        st.session_state["explore_result"] = result.as_dict() if result else None

    result = st.session_state.get("explore_result")
    if not result:
        return
    conditions = result["conditions"]
    st.markdown("**추출된 조건**")
    key_values({"저장소": conditions["repos"] or "제한 없음", "유형": conditions["issue_types"] or "제한 없음",
                "버전": conditions["versions"] or "제한 없음", "범주 (판정 참고)": conditions["categories"] or "-",
                "오류 유형": conditions.get("error_types") or "제한 없음",
                "증상 검색어": conditions["symptom"]})
    filtered = ", ".join(f"{k} {v}건" for k, v in result["filtered_out"].items()) or "없음"
    st.caption(f"검색 후보 {result['searched']}건 → 메타데이터 조건 불일치 제외: {filtered} → "
               f"조건 판정({(result.get('judged') or {}).get('source', '-')}) 통과 {len(result['issues'])}건")
    for warning in result["warnings"]:
        st.warning(warning)
    if not result["issues"]:
        st.info("조건에 맞는 이슈를 찾지 못했습니다. 조건을 줄이거나 다른 표현으로 입력해 주세요.")
    if result.get("rejected"):
        with st.expander(f"조건 판정에서 제외된 후보 {len(result['rejected'])}건"):
            st.caption("메타데이터 조건은 맞지만 LLM이 요청한 증상과 다르다고 판정한 이슈입니다.")
            for item in result["rejected"]:
                st.markdown(f"- [{item['issue_id']}]({item['url']}) {item['title']}")
    for card in result["issues"]:
        with st.container(border=True):
            st.markdown(f"**[{card['issue_id']}]({card['url']})** {card['title']}")
            st.caption(" · ".join([
                f"유형 {ISSUE_TYPE_LABELS.get(card['issue_type'], card['issue_type'])}",
                f"버전 {', '.join(card['versions']) or '-'}",
                f"범주 {card['category'] or '-'}",
                f"라벨 {', '.join(card['labels']) or '-'}",
                f"{card['state']} ({card['state_reason']}) {str(card['closed_at'] or '')[:10]}",
            ]))
            if card["reason"]:
                st.markdown(f"판정: {card['reason']}")
            st.button("이 이슈로 대화하기", key=f"pick:{card['issue_id']}", on_click=select_issue,
                      args=(card["issue_id"], card["repo"]))


def diagnosis_tab(assistant: Assistant, choice: SidebarChoice) -> None:
    st.caption("오류 메시지나 스택 트레이스를 붙여 넣으면 비슷한 종료 이슈와 근거 있는 원인 후보를 찾습니다. "
               "패치를 자동 적용하거나 PR을 만들지 않습니다.")
    with st.form("diagnose"):
        log = st.text_area("오류 로그", height=220)
        note = st.text_input("상황 설명 (선택)", placeholder="예: 26.0.1로 업그레이드한 직후부터 발생")
        submitted = st.form_submit_button("진단")
    if submitted and log.strip():
        result = _run("로그 분석 중", lambda progress: diagnose(assistant, log, note, repo=choice.repo, progress=progress))
        st.session_state["diagnosis"] = result

    result = st.session_state.get("diagnosis")
    if not result:
        return
    signals = result.signals
    st.markdown("**로그에서 찾은 신호**")
    key_values({"예외": signals["exceptions"], "라이브러리·컴포넌트": signals["libraries"],
                "버전": signals["versions"], "실행 환경": signals["environments"]})
    if signals["error_lines"]:
        st.code("\n".join(signals["error_lines"]), language=None)
    if result.similar_issues:
        st.markdown("**가장 유사한 종료 이슈**")
        for issue in result.similar_issues:
            tag = "근거로 사용" if issue["used_as_evidence"] else "참고 (근거로 쓰지 않음)"
            st.markdown(f"- [{issue['issue_id']}]({issue['url']}) {issue['title']} — {tag}")
    st.markdown("**진단**")
    render_answer(result.state, assistant.app_config.get("safety_notice", ""))
