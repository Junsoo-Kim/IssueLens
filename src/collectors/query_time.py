"""질문 시점 외부 문서 수집 (요구사항 F2003·F2004·NF2002, 기획서 8.2절 retrieve 10번).

색인된 근거로 답하기 어려울 때, 프롬프트 1의 검색어로 같은 저장소의 종료 이슈를 GitHub에서 찾고
프롬프트 2가 제목·요약만 보고 고른 이슈만 본문·댓글·연결 PR까지 수집해 RAG 2에 추가 색인한다.

- 이미 검색한 (저장소, 검색어)는 캐시에 남겨 외부 API를 다시 부르지 않는다 (NF2002).
- 평가용으로 격리한 이슈는 후보에서 제외한다. 평가 이슈의 해결 이력이 색인에 섞이는 누수를 막기 위해서다.
- 이미 색인된 이슈가 검색되면 수집하지 않고, 검색 범위에 넣을 이슈로만 돌려준다.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from src.collectors.cache import RawStore, read_jsonl, utc_now, write_json_atomic, write_jsonl_atomic
from src.collectors.github_client import GitHubAPIError, GitHubClient
from src.collectors.issue_collector import NotAnIssueError, to_shallow_record
from src.collectors.runner import collect_issue_detail
from src.preprocessing.build import build_issue_documents, load_raw
from src.preprocessing.metadata_builder import build_issue_item
from src.preprocessing.resolution import infer_maintainers, judge_evidence
from src.preprocessing.tagger import TaggingRules, tag_issue, throw_site_class
from src.prompts.candidate_selector import select_candidates
from src.prompts.llm import LLM

QUERY_TIME_DOCUMENTS = "query_time_documents.jsonl"
QUERY_CACHE = "query_cache.json"
COLLECTION_LOG = "query_collection_log.jsonl"

IndexDocuments = Callable[[list[dict]], int]


VERSION_ONLY = re.compile(r"v?\d+(?:\.\d+)+")
# 어느 저장소에나 흔해 단독 검색어로는 변별력이 낮은 예외. 다른 토큰이 없을 때만 쓴다
GENERIC_EXCEPTIONS = {"exception", "error", "runtimeexception", "nullpointerexception", "illegalstateexception",
                      "illegalargumentexception", "ioexception", "timeoutexception", "valueerror", "keyerror", "typeerror"}
TOKEN_QUERIES = 2


def token_priority(token: str) -> tuple:
    """흔한 예외, 소문자 경로(로거 이름 등), 한 단어 클래스(Template)를 뒤로 미룬다. 나머지는 나온 순서대로 둔다."""
    humps = sum(1 for a, b in zip(token, token[1:]) if a.islower() and b.isupper())
    return (token.lower() in GENERIC_EXCEPTIONS, not any(c.isupper() for c in token), humps == 0)


def search_token(token: str) -> str:
    """패키지·메서드 경로는 클래스 이름만 남긴다. 이슈에는 전체 경로보다 클래스 이름이 더 자주 나온다.
    예: com.linkedin...AspectTemplateEngine.applyPatch -> AspectTemplateEngine"""
    classes = [part for part in token.split(".") if part[:1].isupper()]
    return classes[-1] if "." in token and classes else token


def collection_queries(search_queries: list[str], exact_tokens: list[str], limit: int = 3,
                       question: str = "") -> list[str]:
    """GitHub 검색은 모든 단어가 들어간 이슈만 돌려줘 긴 문장 검색어는 0건이 되기 쉽다.
    정확 토큰(예외명, 오류 코드, 클래스명)을 하나씩 검색어로 만들어 앞에 둔다. 여러 토큰을 한 검색어에 넣으면
    모두 함께 나오는 이슈만 걸려 원래 이슈 말고는 찾기 어렵다. 매번 같은 검색어라 캐시도 적중한다.
    버전 번호만으로는 엉뚱한 이슈가 걸리므로 빼고, 흔한 예외는 뒤로 미룬다.
    question에 스택 트레이스가 있으면 예외가 난 클래스를 맨 앞에 둔다."""
    tokens = [search_token(t) for t in exact_tokens if not VERSION_ONLY.fullmatch(t)]
    tokens.sort(key=token_priority)
    # 질문에 스택 트레이스가 있으면 예외가 난 애플리케이션 클래스를 가장 먼저 검색한다
    site = throw_site_class(question)
    tokens = list(dict.fromkeys([site, *tokens] if site else tokens))
    head = [f'"{t}"' for t in tokens[:TOKEN_QUERIES]]
    return list(dict.fromkeys([*head, *search_queries]))[:limit]


def query_key(repo: str, query: str) -> str:
    normalized = " ".join(query.lower().split())
    return hashlib.sha256(f"{repo.lower()}|{normalized}".encode("utf-8")).hexdigest()


@dataclass
class CollectionResult:
    queries: list[str]
    repos: list[str]
    cache_hits: list[dict] = field(default_factory=list)
    searched: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    selection: dict | None = None
    collected: list[str] = field(default_factory=list)
    # 이번 검색에 쓸 수 있는 색인 이슈: 새로 수집한 이슈, 캐시에 있던 수집 이슈, 검색에 걸린 기존 색인 이슈
    available: list[str] = field(default_factory=list)
    indexed_chunks: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class QueryTimeCollector:
    def __init__(self, client: GitHubClient, raw_store: RawStore, cache_dir: Path, processed_dir: Path,
                 indexed_ids: set[str], excluded_ids: set[str], index_documents: IndexDocuments,
                 repo_configs: dict[str, dict], tagging: dict, results_per_query: int = 10, max_selected: int = 3,
                 max_linked_prs: int = 5):
        self.client = client
        self.raw_store = raw_store
        self.cache_path = Path(cache_dir) / QUERY_CACHE
        self.documents_path = Path(processed_dir) / QUERY_TIME_DOCUMENTS
        self.log_path = Path(processed_dir) / COLLECTION_LOG
        self.indexed_ids = set(indexed_ids)
        self.excluded_ids = set(excluded_ids)
        self.index_documents = index_documents
        self.repo_configs = repo_configs
        self.tagging = tagging
        self.results_per_query = results_per_query
        self.max_selected = max_selected
        self.max_linked_prs = max_linked_prs
        self.cache: dict[str, dict] = {}
        if self.cache_path.exists():
            with open(self.cache_path, encoding="utf-8") as f:
                self.cache = json.load(f)
        self.documents = read_jsonl(self.documents_path) if self.documents_path.exists() else []
        self.collected_ids = {d["issue_id"] for d in self.documents}
        self._maintainers: dict[str, set[str]] = {}

    def _repo_maintainers(self, repo: str) -> set[str]:
        if repo not in self._maintainers:
            issues_dir = self.raw_store.path(repo, "issues", "x").parent
            issues = []
            for path in sorted(issues_dir.glob("*.json")) if issues_dir.exists() else []:
                with open(path, encoding="utf-8") as f:
                    issues.append(json.load(f))
            self._maintainers[repo] = infer_maintainers(issues)
        return self._maintainers[repo]

    def _search(self, repo: str, query: str) -> list[dict]:
        params = {"q": f"repo:{repo} is:issue is:closed {query}", "per_page": self.results_per_query}
        data = self.client.get("/search/issues", params).data or {}
        return [to_shallow_record(repo, item) for item in data.get("items", []) if "pull_request" not in item]

    def _collect_issue(self, shallow: dict) -> list[dict]:
        repo, number = shallow["repo"], shallow["number"]
        repo_config = self.repo_configs.get(repo, {})
        allowlist = repo_config.get("docs_allowlist", [])
        summary = collect_issue_detail(self.client, repo, number, self.raw_store, docs_allowlist=allowlist,
                                       include_comments=True, include_timeline=True, include_linked_prs=True,
                                       max_linked_prs=self.max_linked_prs)
        issue = load_raw(self.raw_store, repo, "issues", number) or {}
        comments = load_raw(self.raw_store, repo, "comments", number) or []
        maintainers = self._repo_maintainers(repo)
        tag = tag_issue(shallow, TaggingRules.from_config(self.tagging, repo_config), repo_config)
        evidence = judge_evidence(summary, comments, (issue.get("user") or {}).get("login"), maintainers, allowlist)
        row = {"issue_id": shallow["issue_id"], "repo": repo, "number": number, "final_split": "query_time", **evidence}
        item = build_issue_item(issue, tag, row, summary)
        documents, _ = build_issue_documents(issue, comments, summary, item, self.raw_store, maintainers)
        return [{**d, "collection_source": "query_time"} for d in documents]

    def collect(self, question: str, queries: list[str], repos: list[str], llm: LLM | None) -> CollectionResult:
        result = CollectionResult(queries=list(queries), repos=list(repos))
        candidates: dict[str, dict] = {}
        pending: list[tuple[str, str, str, list[str]]] = []
        for repo in repos:
            for query in queries:
                key = query_key(repo, query)
                if key in self.cache:
                    cached = self.cache[key]
                    result.cache_hits.append({"repo": repo, "query": query, "searched_at": cached.get("searched_at")})
                    result.available.extend(cached.get("available_ids", []))
                    continue
                try:
                    records = self._search(repo, query)
                except GitHubAPIError as exc:
                    result.errors.append(f"검색 실패 {repo} '{query}': {exc.status} {exc.reason}")
                    continue
                result.searched.append({"repo": repo, "query": query, "results": len(records)})
                pending.append((key, repo, query, [r["issue_id"] for r in records]))
                for record in records:
                    issue_id = record["issue_id"]
                    if issue_id in self.excluded_ids:
                        continue
                    if issue_id in self.indexed_ids or issue_id in self.collected_ids:
                        result.available.append(issue_id)
                        continue
                    candidates.setdefault(issue_id, record)

        result.candidates = [{"issue_id": c["issue_id"], "title": c["title"]} for c in candidates.values()]
        selection = select_candidates(llm, question, list(candidates.values()), self.max_selected)
        result.selection = selection.as_dict()

        new_documents = []
        for issue_id in selection.selected_ids:
            try:
                new_documents.extend(self._collect_issue(candidates[issue_id]))
                result.collected.append(issue_id)
            except (GitHubAPIError, NotAnIssueError) as exc:
                result.errors.append(f"수집 실패 {issue_id}: {exc}")
        if new_documents:
            result.indexed_chunks = self.index_documents(new_documents)
            by_id = {d["document_id"]: d for d in self.documents}
            by_id.update({d["document_id"]: d for d in new_documents})
            self.documents = list(by_id.values())
            write_jsonl_atomic(self.documents_path, self.documents)
            self.collected_ids.update(result.collected)
        result.available = list(dict.fromkeys(result.collected + result.available))

        for key, repo, query, result_ids in pending:
            hit_ids = set(result_ids)
            self.cache[key] = {
                "repo": repo, "query": query, "searched_at": utc_now(), "result_ids": result_ids,
                "selected_ids": [i for i in selection.selected_ids if i in hit_ids],
                "rejected_ids": [i for i in selection.rejected_ids if i in hit_ids],
                "available_ids": [i for i in result.available if i in hit_ids],
            }
        if pending:
            write_json_atomic(self.cache_path, self.cache)
        self._log(question, result)
        return result

    def _log(self, question: str, result: CollectionResult) -> None:
        """프롬프트 1 검색어, 프롬프트 2 선택·탈락 결과를 남긴다 (요구사항 정의서 4.2절 확인용). 원문은 남기지 않는다."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"logged_at": utc_now(), "question": question, **result.as_dict()}, ensure_ascii=False) + "\n")
