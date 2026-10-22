"""오류 로그 기반 유사 장애 진단 (요구사항 F2010, 기획서 3.3절).

로그에서 예외명, 핵심 오류 구문, 라이브러리, 버전, 실행 환경을 규칙으로 뽑아 보여 주고,
진단 질문을 RAG 파이프라인(재시도, 질문 시점 수집, 근거 판정, 인용 검증)에 그대로 넘긴다.
답변의 원인 후보, 환경 비교, 확인 절차는 근거 번호로 연결되고, 근거가 부족하면 답하지 않는다.
패치를 자동 적용하거나 PR을 만들지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from src.preprocessing.tagger import EXCEPTION_NAME, VERSION_TOKEN

MAX_LOG_CHARS = 3000
SIMILAR_ISSUES = 3
ERROR_LINE = re.compile(r"\b(ERROR|FATAL|CRITICAL|Exception|Error|Caused by|failed|Failed|FAILED|panic)\b")
JAVA_FRAME = re.compile(r"^\s*at ((?:[a-z_][\w]*\.){2,})[A-Z]", re.MULTILINE)
PYTHON_FRAME = re.compile(r'File "[^"]*(?:site-packages|dist-packages)[/\\]([A-Za-z_][\w]*)')
PACKAGE_IN_TEXT = re.compile(r"\b((?:org|com|io|net)\.[a-z_][\w]*(?:\.[a-z_][\w]*)?)\.")
ENVIRONMENTS = {
    "Kubernetes": re.compile(r"\bkubernetes\b|\bk8s\b|\bkubectl\b|\bhelm\b|\bpod\b", re.I),
    "Docker": re.compile(r"\bdocker\b|\bcontainer\b", re.I),
    "PostgreSQL": re.compile(r"\bpostgres(?:ql)?\b|\bpsql\b", re.I),
    "MySQL": re.compile(r"\bmysql\b", re.I),
    "Redis": re.compile(r"\bredis\b", re.I),
    "Kafka": re.compile(r"\bkafka\b", re.I),
    "Elasticsearch/OpenSearch": re.compile(r"\belasticsearch\b|\bopensearch\b", re.I),
    "Java": re.compile(r"\bjava(?:\s+version)?\s*\d+|\bjdk\b|\bopenjdk\b|^\s+at [\w.$]+\(", re.I | re.M),
    "Python": re.compile(r"Traceback \(most recent call last\)|\bpython\s*3", re.I),
    "Windows": re.compile(r"\bwindows\b|[A-Z]:\\\\", re.I),
    "Linux": re.compile(r"\blinux\b|\bubuntu\b|\bdebian\b|\balpine\b|\bcentos\b|\brhel\b", re.I),
    "macOS": re.compile(r"\bmacos\b|\bdarwin\b", re.I),
}


def _unique(items, limit: int) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))[:limit]


def extract_signals(log: str) -> dict:
    """화면에 보여 줄 로그 신호. 규칙 기반이라 LLM 없이도 동작한다."""
    lines = [line.strip() for line in log.splitlines() if line.strip()]
    libraries = [m.rstrip(".") for m in JAVA_FRAME.findall(log)]
    libraries += PYTHON_FRAME.findall(log) + PACKAGE_IN_TEXT.findall(log)
    # 패키지 경로는 앞 두 단계(예: org.keycloak)로 묶는다
    libraries = [".".join(name.split(".")[:2]) if "." in name else name for name in libraries]
    return {
        "exceptions": _unique(EXCEPTION_NAME.findall(log), 5),
        "error_lines": _unique((line[:200] for line in lines if ERROR_LINE.search(line) and not line.startswith("at ")), 5),
        "libraries": _unique(libraries, 6),
        "versions": _unique((m.group(0) for m in VERSION_TOKEN.finditer(log)), 6),
        "environments": [name for name, pattern in ENVIRONMENTS.items() if pattern.search(log)],
    }


def trim_log(log: str, max_chars: int = MAX_LOG_CHARS) -> str:
    """긴 로그는 앞부분과 오류 줄 위주로 줄인다. 스택 프레임이 반복되면 검색어가 흐려진다."""
    if len(log) <= max_chars:
        return log.strip()
    lines = log.strip().splitlines()
    head = lines[:20]
    errors = [line for line in lines[20:] if ERROR_LINE.search(line)]
    return "\n".join(dict.fromkeys([*head, "...", *errors]))[:max_chars]


def diagnosis_question(log: str, note: str = "") -> str:
    prefix = "다음 오류 로그와 비슷한 종료 이슈를 찾아 원인 후보, 환경 차이, 우선 확인할 재현·검증 절차를 알려줘."
    if note.strip():
        prefix += f"\n상황 설명: {note.strip()[:500]}"
    return f"{prefix}\n\n{trim_log(log)}"


@dataclass
class Diagnosis:
    signals: dict
    state: dict
    similar_issues: list[dict] = field(default_factory=list)


def similar_issues(state: dict, lookup: Callable[[str], dict | None], limit: int = SIMILAR_ISSUES) -> list[dict]:
    """근거로 쓴 이슈를 먼저, 부족하면 가까운 사례로 채운다. 근거로 쓰지 않은 이슈는 그렇게 표시한다."""
    cited = [e.issue_id for e in state.get("cited_evidence") or []]
    evidence = [e.issue_id for e in state.get("evidence") or []] if state.get("answered_from_evidence") else []
    nearest = [n["issue_id"] for n in state.get("nearest_issues") or []]
    used = set(cited) | set(evidence)
    result = []
    for issue_id in _unique([*cited, *evidence, *nearest], limit):
        metadata = lookup(issue_id) or {}
        result.append({
            "issue_id": issue_id,
            "title": metadata.get("title", ""),
            "url": metadata.get("source_url") or f"https://github.com/{issue_id.replace('#', '/issues/')}",
            "used_as_evidence": issue_id in used,
        })
    return result


def diagnose(assistant, log: str, note: str = "", repo: str | None = None,
             progress: Callable[[str], None] | None = None) -> Diagnosis:
    signals = extract_signals(log)
    state = assistant.ask(diagnosis_question(log, note), repo=repo, allow_general_answer=False, progress=progress)
    return Diagnosis(signals, state, similar_issues(state, assistant.retriever.issue_metadata))

