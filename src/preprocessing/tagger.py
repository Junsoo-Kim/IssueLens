"""얕은 수집 이슈 태깅: 이슈 유형, 컴포넌트, 공통 상위 범주, 오류·버전, 품질 신호와 점수.

태깅 우선순위는 기획서 5.4절을 따른다: 라벨 -> 제목/본문의 명시적 키워드 -> 오류 로그 -> 정규식.
LLM 분류는 쓰지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.collectors.comment_collector import CODE_BLOCK, ERROR_TEXT, MAINTAINER_ASSOCIATIONS

HEADING = re.compile(r"^#{1,4}\s+(.+?)\s*#*\s*$", re.MULTILINE)
NO_RESPONSE = re.compile(r"^_?no response_?$", re.IGNORECASE)
LABEL_NOISE = re.compile(r"[^a-z0-9/:\-_.' ]+")
EXCEPTION_NAME = re.compile(r"\b[A-Z][A-Za-z0-9_]*(?:Exception|Error)\b")
STACK_TRACE = re.compile(r"Traceback \(most recent call last\)|^\s+at [\w.$]+\(", re.MULTILINE)
VERSION_TOKEN = re.compile(r"(?<![\d.])v?\d+\.\d+(?:\.\d+)?(?:[-.](?:rc|beta|alpha)\.?\d*)?(?![\d.])", re.IGNORECASE)
REPRODUCTION = re.compile(r"steps? to reproduce|how to reproduce|reproduc(e|tion) steps|to reproduce", re.IGNORECASE)
TITLE_PREFIX = re.compile(r"^\s*\[([^\]]{2,40})\]")

SECTION_KINDS = {
    "reproduction": re.compile(r"reproduc|steps", re.IGNORECASE),
    "environment": re.compile(r"version|environment|deploy|self.?host|platform|operating system|\bos\b|browser|setup|install", re.IGNORECASE),
    "logs": re.compile(r"log|stack|trace|error|output", re.IGNORECASE),
}

TITLE_TYPE_RULES = [
    ("test", re.compile(r"\bflaky\b|\bplaywright\b|\bunit tests?\b|\be2e\b|\btests? (is |are )?fail", re.IGNORECASE)),
    ("feature", re.compile(r"^\s*\[?(feature|feat|enhancement|proposal|request|rfc)\b|feature request|add support|support for|would be nice|content proposal", re.IGNORECASE)),
    ("bug", re.compile(r"\b(error|exception|fail(s|ed|ing|ure)?|crash(es|ed|ing)?|bug|broken|not working|doesn'?t work|does not work|unable to|cannot|can'?t|timeout|hangs?|leak|incorrect|wrong|regression|500)\b", re.IGNORECASE)),
    ("question", re.compile(r"\?\s*$|^\s*(how (to|do|can)|is it possible|is there a way)\b|\bquestion\b", re.IGNORECASE)),
]

# 기획서 5.4절 공통 상위 범주 키워드
CATEGORY_KEYWORDS = {
    "deployment_upgrade": r"upgrad\w*|migrat\w*|helm|docker\w*|kubernetes|k8s|install\w*|deploy\w*|startup|start up|compose|image|release",
    "database_search": r"postgres\w*|mysql|database|\bdb\b|\bsql\b|elasticsearch|opensearch|\bindex\w*|search\w*|query|queries|cassandra|redis|clickhouse",
    "async_event": r"kafka|queue|event\w*|async|worker|celery|schedul\w*|\bsync\w*|workflow|\bjob\b|mqtt|rule engine|pipeline|ingestion|connector",
    "auth": r"auth\w*|login|logout|oidc|saml|ldap|token|jwt|permission\w*|\brole\w*|rbac|\bsso\b|oauth|session|password|realm|identity|keycloak",
    "performance_reliability": r"slow\w*|performance|memory|\boom\b|leak|\bcpu\b|timeout|latency|hang\w*|crash\w*|deadlock|high load|throughput",
    "api_ui": r"\bapi\b|\bui\b|frontend|page|button|endpoint|\brest\b|graphql|dashboard|widget|display\w*|render\w*|modal",
}
CATEGORY_PATTERNS = {name: re.compile(rf"\b(?:{pattern})", re.IGNORECASE) for name, pattern in CATEGORY_KEYWORDS.items()}

LANGUAGES = {"python": r"\bpython\b|\.py\b|pip install", "java": r"\bjava\b|\.java\b|\bjvm\b|spring",
             "typescript": r"typescript|\.tsx?\b|node\.?js|\bnpm\b|react", "go": r"\bgolang\b|\.go\b"}
DEPENDENCIES = ["postgresql", "postgres", "mysql", "elasticsearch", "opensearch", "kafka", "redis",
                "kubernetes", "docker", "helm", "cassandra", "clickhouse", "airflow", "spark"]

BODY_SCAN_CHARS = 4000
MIN_BODY_CHARS = 100


@dataclass
class TaggingRules:
    label_types: dict[str, set[str]]
    type_priority: list[str]
    stale_labels: set[str]
    unreproducible_labels: set[str]
    component_patterns: list[re.Pattern]

    @classmethod
    def from_config(cls, tagging: dict, repo_config: dict | None = None) -> "TaggingRules":
        repo_config = repo_config or {}
        patterns = list(repo_config.get("component_patterns", [])) + list(tagging.get("component_patterns", []))
        return cls(
            label_types={t: {normalize_label(l) for l in labels} for t, labels in tagging.get("label_types", {}).items()},
            type_priority=list(tagging.get("type_priority", [])),
            stale_labels={normalize_label(l) for l in tagging.get("stale_labels", [])},
            unreproducible_labels={normalize_label(l) for l in tagging.get("unreproducible_labels", [])},
            component_patterns=[re.compile(p, re.IGNORECASE) for p in patterns],
        )


@dataclass
class Sections:
    """이슈 템플릿 본문을 `### 제목` 단위로 나눈 결과. 응답 없는 항목은 비어 있다."""

    filled: dict[str, str] = field(default_factory=dict)

    def has(self, kind: str) -> bool:
        pattern = SECTION_KINDS[kind]
        return any(pattern.search(name) and text for name, text in self.filled.items())

    def text(self, kind: str) -> str:
        pattern = SECTION_KINDS[kind]
        return "\n".join(text for name, text in self.filled.items() if pattern.search(name))


def normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", LABEL_NOISE.sub("", label.lower())).strip()


def parse_sections(body: str) -> Sections:
    matches = list(HEADING.finditer(body))
    filled = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[match.end():end].strip()
        if NO_RESPONSE.match(text):
            text = ""
        filled[match.group(1).strip().lower()] = text
    return Sections(filled)


def meaningful_body(body: str, sections: Sections) -> str:
    """템플릿 제목과 '_No response_'를 뺀 실제 작성 내용."""
    if not sections.filled:
        return body.strip()
    return "\n".join(text for text in sections.filled.values() if text)


def classify_type(labels: list[str], title: str, body: str, rules: TaggingRules) -> tuple[str, str]:
    """(이슈 유형, 근거) 반환. 근거는 label 또는 title 또는 body."""
    normalized = {normalize_label(label) for label in labels}
    label_hits = [t for t in rules.type_priority if normalized & rules.label_types.get(t, set())]
    title_type = next((t for t, pattern in TITLE_TYPE_RULES if pattern.search(title)), None)
    # 테스트 이슈는 bug 라벨이 붙어 있어도 제목으로 걸러낸다 (예: "Flaky test: ...")
    if title_type == "test" and "invalid" not in label_hits:
        return "test", "title"
    if label_hits:
        return label_hits[0], "label"
    if title_type:
        return title_type, "title"
    if REPRODUCTION.search(body[:BODY_SCAN_CHARS]) or STACK_TRACE.search(body):
        return "bug", "body"
    return "unknown", "none"


def extract_components(labels: list[str], title: str, rules: TaggingRules) -> list[str]:
    normalized = [normalize_label(label) for label in labels]
    components = []
    for pattern in rules.component_patterns:
        for label in normalized:
            if pattern.search(label) and label not in components:
                components.append(label)
    prefix = TITLE_PREFIX.match(title)
    if prefix and not VERSION_TOKEN.fullmatch(prefix.group(1).strip()):
        components.append(f"title:{prefix.group(1).strip().lower()}")
    return components


def classify_categories(title: str, body: str, components: list[str], fallback: list[str]) -> list[str]:
    """공통 상위 범주를 점수순으로 반환한다. 제목과 컴포넌트는 본문보다 가중치를 크게 둔다."""
    head = f"{title} {' '.join(components)}"
    text = body[:BODY_SCAN_CHARS]
    scores = {}
    for name, pattern in CATEGORY_PATTERNS.items():
        score = 3 * len(pattern.findall(head)) + len(pattern.findall(text))
        if score:
            scores[name] = score
    ranked = sorted(scores, key=lambda n: (-scores[n], fallback.index(n) if n in fallback else len(fallback)))
    return ranked[:2] or fallback[:1]


def extract_versions(body: str, sections: Sections) -> list[str]:
    source = sections.text("environment") or body[:BODY_SCAN_CHARS]
    versions = []
    for match in VERSION_TOKEN.finditer(source):
        token = match.group(0)
        if token not in versions:
            versions.append(token)
    return versions[:5]


def tag_issue(issue: dict, rules: TaggingRules, repo_config: dict | None = None) -> dict:
    repo_config = repo_config or {}
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    labels = issue.get("labels") or []
    sections = parse_sections(body)
    content = meaningful_body(body, sections)
    normalized_labels = {normalize_label(label) for label in labels}

    issue_type, type_source = classify_type(labels, title, body, rules)
    components = extract_components(labels, title, rules)
    categories = classify_categories(title, body, components, repo_config.get("categories", []))
    error_types = list(dict.fromkeys(EXCEPTION_NAME.findall(body)))[:5]
    lowered = body.lower()

    has_error_log = bool(error_types or STACK_TRACE.search(body) or (sections.has("logs") and CODE_BLOCK.search(sections.text("logs")))
                         or ERROR_TEXT.search(body))
    has_reproduction = bool(sections.has("reproduction") or REPRODUCTION.search(content))
    versions = extract_versions(body, sections)
    has_environment = bool(sections.has("environment") or versions)
    maintainer_author = issue.get("author_association") in MAINTAINER_ASSOCIATIONS
    comments = issue.get("comments") or 0
    stale = bool(normalized_labels & rules.stale_labels)
    unreproducible = bool(normalized_labels & rules.unreproducible_labels)
    state_reason = issue.get("state_reason")
    # 얕은 수집 단계의 근사치. 해결 근거는 상세 수집 후 댓글과 연결 PR로 다시 판정한다
    has_resolution_signal = state_reason == "completed" and (comments > 0 or maintainer_author)

    score = 0
    score += 2 if has_error_log else 0
    score += 2 if has_reproduction else 0
    score += 2 if has_environment else 0
    score += 1 if len(content) >= 300 else 0
    score += 1 if comments > 0 else 0
    score += 1 if maintainer_author else 0
    score += 1 if state_reason == "completed" else 0
    score -= 2 if state_reason == "not_planned" else 0
    score -= 2 if stale else 0
    score -= 1 if unreproducible else 0

    return {
        "issue_id": issue["issue_id"],
        "repo": issue["repo"],
        "number": issue["number"],
        "issue_type": issue_type,
        "type_source": type_source,
        "components": components,
        "component": components[0] if components else "general",
        "categories": categories,
        "category": categories[0] if categories else None,
        "languages": [name for name, pattern in LANGUAGES.items() if re.search(pattern, lowered)],
        "dependencies": [dep for dep in DEPENDENCIES if dep in lowered],
        "error_types": error_types,
        "versions": versions,
        "has_reproduction": has_reproduction,
        "has_error_log": has_error_log,
        "has_environment": has_environment,
        "has_resolution_signal": has_resolution_signal,
        "signal_count": sum((has_error_log, has_reproduction, has_environment, has_resolution_signal)),
        "content_length": len(content),
        "comments": comments,
        "maintainer_author": maintainer_author,
        "stale": stale,
        "unreproducible": unreproducible,
        "state_reason": state_reason,
        "closed_at": issue.get("closed_at"),
        "quality_score": score,
    }
