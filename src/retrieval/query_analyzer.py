"""규칙 기반 질의 분석: 질의 유형, 정확 토큰, 명시 저장소, 버전, 오류 유형, 범주 추정 (기획서 9.2절).

LLM 질의 분석(프롬프트 1)이 실패했을 때의 대체 경로이자, 프롬프트 1 없이도 검색이 동작하게 하는 기본 경로다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.preprocessing.tagger import STACK_TRACE, VERSION_TOKEN, classify_categories

# 질문은 한국어 조사가 영문 토큰에 바로 붙는다("NotFoundException이", "500에러"). \b는 한글도 단어 문자로 보아
# 경계를 못 찾으므로 영문·숫자 기준 경계를 쓴다
_L, _R = r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])"
QUESTION_EXCEPTION = re.compile(_L + r"[A-Z][A-Za-z0-9_]*(?:Exception|Error)" + _R)
LOG_LEVEL = re.compile(_L + r"(ERROR|WARN(?:ING)?|FATAL|CRITICAL)" + _R)
STATUS_CODE = re.compile(_L + r"[45]\d\d" + r"(?![0-9])")
CONFIG_KEY = re.compile(_L + r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,}" + _R)
DOTTED_PATH = re.compile(_L + r"[a-z][A-Za-z0-9_]*(?:\.[a-z_][A-Za-z0-9_]*){2,}" + _R)
CONDITION = re.compile(r"찾아|검색해|목록|리스트|보여\s*줘|알려\s*줘.*이슈|\bfind\b|\blist\b|\bshow me\b", re.I)
QUERY_TYPES = ("error_log", "natural_language", "condition_search", "selected_issue")


@dataclass
class QueryAnalysis:
    question: str
    query_type: str
    explicit_repos: list[str] = field(default_factory=list)
    exact_tokens: list[str] = field(default_factory=list)
    error_types: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)

    def sparse_query(self) -> str:
        """BM25에는 원문 질문과 정확 토큰을 함께 넣어 기술 토큰의 비중을 높인다."""
        return " ".join([self.question, *self.exact_tokens])


def repo_aliases(repositories: list[dict]) -> dict[str, list[str]]:
    """저장소 이름과 설정의 aliases로 질문 속 언급을 찾는다. 공백·하이픈은 무시하고 비교한다."""
    aliases = {}
    for repo in repositories:
        name = repo["repo"].split("/", 1)[1]
        aliases[repo["repo"]] = sorted({_compact(name), *(_compact(a) for a in repo.get("aliases", []))})
    return aliases


def _compact(text: str) -> str:
    return re.sub(r"[\s\-_]+", "", text.lower())


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))


def analyze_query(question: str, repositories: list[dict], selected_issue_id: str | None = None) -> QueryAnalysis:
    compact = _compact(question)
    explicit = [repo for repo, names in repo_aliases(repositories).items() if any(n in compact for n in names)]
    error_types = _unique(QUESTION_EXCEPTION.findall(question))
    versions = _unique(m.group(0) for m in VERSION_TOKEN.finditer(question))
    exact = _unique([*error_types, *versions, *CONFIG_KEY.findall(question), *DOTTED_PATH.findall(question),
                     *STATUS_CODE.findall(question)])

    if selected_issue_id:
        query_type = "selected_issue"
    elif error_types or STACK_TRACE.search(question) or LOG_LEVEL.search(question):
        query_type = "error_log"
    elif CONDITION.search(question):
        query_type = "condition_search"
    else:
        query_type = "natural_language"

    return QueryAnalysis(
        question=question.strip(),
        query_type=query_type,
        explicit_repos=explicit,
        exact_tokens=exact,
        error_types=error_types,
        versions=versions,
        categories=classify_categories(question, question, [], []),
    )
