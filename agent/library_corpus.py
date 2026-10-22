"""에이전트 코퍼스 구축: 라이브러리 저장소의 해결 이력 (기획서 5.15절, 17.5절).

수집·정제·색인은 챗봇과 같은 함수를 쓰고, 어떤 이슈를 해결 근거로 인정할지만 다르다.

    fix      completed로 종료되고 병합 PR·종료 커밋·유지보수자의 해결 설명이 있음
    usage    질문·invalid·not_planned 등으로 종료되고 유지보수자가 원인이나 올바른 사용법을 설명함
    discussion  Discussions Q&A에서 채택된 답변 (GraphQL)

한 라이브러리의 구축 순서: 최근 종료 이슈 얕은 검색 -> 규칙 사전 필터 -> 순서대로 상세 수집하며 해결 근거 판정
-> 목표 건수가 차면 멈춤 -> Discussions -> RAG 1/RAG 2 upsert -> 상태 파일 기록.
상태 파일로 이미 색인한 이슈는 다시 수집하지 않고, 오래되면 마지막 구축 이후 종료된 이슈만 더한다.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from agent.registry import Corpus, Library, Resolution
from src.collectors.cache import RawStore, utc_now, write_json_atomic
from src.collectors.github_client import GitHubAPIError, GitHubClient
from src.collectors.issue_collector import NotAnIssueError, to_shallow_record
from src.collectors.runner import collect_issue_detail
from src.indexing import document_indexer, metadata_indexer
from src.indexing.bm25_indexer import upsert_bm25
from src.indexing.chroma_store import get_collection, open_client, upsert_records
from src.indexing.document_indexer import document_records
from src.indexing.metadata_indexer import issue_records
from src.preprocessing.build import build_issue_documents, load_raw
from src.preprocessing.document_builder import LICENSE_NOTE
from src.preprocessing.metadata_builder import build_issue_item, content_hash
from src.preprocessing.normalizer import normalize_markdown
from src.preprocessing.resolution import infer_maintainers, judge_evidence
from src.preprocessing.secret_redactor import redact
from src.preprocessing.tagger import EXCEPTION_NAME, VERSION_TOKEN, TaggingRules, normalize_label, tag_issue

STATE_FILE = "corpus_state.json"
GRAPHQL_URL = "https://api.github.com/graphql"
MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
USAGE_LABELS = {"question", "invalid", "works as intended", "working as intended", "not a bug", "not-a-bug",
                "wontfix", "won't fix", "usage", "support", "user error", "configuration", "expected behavior"}
EXCLUDED_TYPES = {"feature", "task", "test"}
MIN_BODY_CHARS = 100
DETAIL_ATTEMPTS = 2  # 목표 건수의 몇 배까지 상세 수집을 시도할지 (판정에서 빠지는 이슈 대비)
DISCUSSION_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    discussions(first: $first, after: $after, answered: true, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body url createdAt updatedAt authorAssociation
        category { name isAnswerable }
        author { login }
        answer { body url createdAt authorAssociation author { login } }
      }
    }
  }
}"""

Post = Callable[[str, dict], dict | None]  # (URL, JSON 본문) -> 응답 JSON


@dataclass
class BuildReport:
    repo: str
    mode: str  # full, incremental
    searched: int = 0
    prefiltered: int = 0
    detailed: int = 0
    indexed_issues: int = 0
    held_out: int = 0
    discussions: int = 0
    chunks: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)
    requests: int = 0
    stopped: str | None = None  # 예산 초과 등으로 멈춘 이유
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 판정


def prefilter(tag: dict, shallow: dict) -> str | None:
    """상세 수집 전에 버리는 이유. None이면 후보."""
    if tag["issue_type"] in EXCLUDED_TYPES:
        return f"type:{tag['issue_type']}"
    if shallow.get("state_reason") == "duplicate":
        return "duplicate"
    if tag.get("stale"):
        return "stale"  # 봇이 닫은 이슈는 답이 없는 경우가 대부분이다
    if (shallow.get("comments") or 0) == 0:
        return "no_comments"
    if len(shallow.get("body") or "") < MIN_BODY_CHARS:
        return "body_too_short"
    return None


def resolution_kind(shallow: dict, tag: dict, evidence: dict) -> str | None:
    """fix, usage 중 하나. 해결 근거가 없으면 None."""
    labels = {normalize_label(label) for label in shallow.get("labels") or []}
    usage_like = (tag["issue_type"] in {"question", "invalid"} or bool(labels & USAGE_LABELS)
                  or shallow.get("state_reason") == "not_planned")
    if usage_like:
        return "usage" if evidence.get("maintainer_detail") else None
    if shallow.get("state_reason") == "completed" and evidence.get("evidence_level") in {"strong", "medium"}:
        return "fix"
    return None


# ---------------------------------------------------------------- Discussions


def default_post(token: str | None) -> Post:
    import requests

    def post(url: str, body: dict) -> dict | None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = requests.post(url, json=body, headers=headers, timeout=30)
        except requests.RequestException as exc:
            return {"errors": [{"message": type(exc).__name__}]}
        if response.status_code != 200:
            return {"errors": [{"message": f"HTTP {response.status_code}"}]}
        return response.json()
    return post


def fetch_discussions(post: Post, repo: str, limit: int, errors: list[str] | None = None) -> list[dict]:
    """채택 답변이 있는 Q&A Discussions (최근 갱신 순). 실패 사유는 errors에 더한다."""
    owner, name = repo.split("/", 1)
    found, after = [], None
    while len(found) < limit:
        data = post(GRAPHQL_URL, {"query": DISCUSSION_QUERY,
                                  "variables": {"owner": owner, "name": name, "first": min(50, limit), "after": after}})
        if errors is not None and (data is None or data.get("errors")):
            messages = [e.get("message", "") for e in (data or {}).get("errors") or [{"message": "응답 없음"}]]
            errors.append(f"Discussions 조회 실패: {'; '.join(messages)[:200]}")
        page = ((((data or {}).get("data") or {}).get("repository") or {}).get("discussions")) or {}
        for node in page.get("nodes") or []:
            if node.get("answer") and (node.get("category") or {}).get("isAnswerable", True):
                found.append(node)
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
    return found[:limit]


def _clean(text: str | None) -> str:
    body, _ = redact(text or "")
    body, _ = normalize_markdown(body)
    return body


def discussion_item_and_documents(repo: str, node: dict) -> tuple[dict, list[dict]]:
    number = node["number"]
    issue_id = f"{repo}#{number}"
    title, question, answer = _clean(node.get("title")), _clean(node.get("body")), _clean(node["answer"].get("body"))
    errors = list(dict.fromkeys(EXCEPTION_NAME.findall(question)))[:5]
    versions = list(dict.fromkeys(m.group(0) for m in VERSION_TOKEN.finditer(question)))[:5]
    index_text = "\n".join([f"저장소: {repo}", f"Discussion 번호: {number}", f"제목: {title}",
                            f"분류: {(node.get('category') or {}).get('name', '')}", "유형: question",
                            f"오류 유형: {', '.join(errors)}", f"버전: {', '.join(versions)}",
                            f"질문 요약: {' '.join(question.split())[:500]}", "해결 근거: 채택된 답변"])
    item = {
        "issue_id": issue_id, "repo": repo, "number": number, "title": title, "labels": ["discussion"],
        "issue_type": "question", "component": "general", "components": [], "category": None, "categories": [],
        "error_types": errors, "versions": versions, "platforms": [], "state": "closed", "state_reason": "answered",
        "created_at": node.get("createdAt"), "closed_at": node["answer"].get("createdAt"), "source_url": node.get("url"),
        "evidence_level": "medium", "index_text": index_text, "content_hash": content_hash(index_text),
        "corpus": "library", "resolution_kind": "discussion", "library": repo,
    }
    role = "maintainer" if node["answer"].get("authorAssociation") in MAINTAINER_ASSOCIATIONS else "other"
    common = {"issue_id": issue_id, "repo": repo, "issue_title": title, "component": "general",
              "license_note": LICENSE_NOTE, "source_type": "github_discussion", "corpus": "library",
              "resolution_kind": "discussion", "library": repo}
    documents = [
        {**common, "document_id": f"{issue_id}:discussion_question", "document_type": "discussion_question",
         "title": title, "body": question, "content_hash": content_hash(f"{title}\n{question}"),
         "author_role": "reporter", "source_url": node.get("url"), "source_updated_at": node.get("updatedAt"),
         "selection_reason": "discussion"},
        {**common, "document_id": f"{issue_id}:discussion_answer", "document_type": "discussion_answer",
         "title": f"{title} (채택 답변)", "body": answer, "content_hash": content_hash(answer),
         "author_role": role, "source_url": node["answer"].get("url") or node.get("url"),
         "source_updated_at": node["answer"].get("createdAt"), "selection_reason": "accepted_answer"},
    ]
    return item, [d for d in documents if d["body"].strip()]


# ---------------------------------------------------------------- 색인과 상태


class LibraryIndex:
    """에이전트 코퍼스 색인 (챗봇 색인과 다른 폴더). 같은 컬렉션 이름과 ID 규칙을 쓴다."""

    def __init__(self, root: Path, retrieval_config: dict, embedder=None):
        self.root = Path(root)
        collections = retrieval_config.get("collections", {})
        self.names = (collections.get("issue_metadata", metadata_indexer.COLLECTION),
                      collections.get("issue_documents", document_indexer.COLLECTION))
        self.chunking = retrieval_config.get("chunking", {})
        self.distance = retrieval_config.get("embedding", {}).get("distance", "cosine")
        self.embedder = embedder
        self.chroma_dir, self.sparse_dir, self.raw_dir = self.root / "chroma", self.root / "sparse", self.root / "raw"

    def upsert(self, items: list[dict], documents: list[dict]) -> int:
        issue_recs, doc_recs = issue_records(items), document_records(documents, self.chunking)
        for name, records in zip(self.names, (issue_recs, doc_recs)):
            if not records:
                continue
            if self.embedder is not None:
                upsert_records(get_collection(open_client(self.chroma_dir), name, self.distance), records, self.embedder)
            upsert_bm25(records, self.sparse_dir / name)
        return len(doc_recs)


def load_state(root: Path) -> dict:
    path = Path(root) / STATE_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(root: Path, state: dict) -> None:
    write_json_atomic(Path(root) / STATE_FILE, state)


def needs_build(state: dict, repo: str, refresh_days: int, now: datetime | None = None,
                embedded: bool = True) -> str | None:
    """None(최신), "full"(처음), "incremental"(오래됨).

    embedded: 이번 구축에서 임베딩하는지. 임베딩 없이(--skip-embed) 만든 구축은 Chroma가 비어 있어 처음부터 다시 한다."""
    entry = state.get(repo)
    if not entry or (embedded and not entry.get("embedded")):
        return "full"
    built = datetime.fromisoformat(entry["built_at"].replace("Z", "+00:00"))
    return "incremental" if (now or datetime.now(timezone.utc)) - built > timedelta(days=refresh_days) else None


# ---------------------------------------------------------------- 구축


class LibraryCorpusBuilder:
    def __init__(self, client: GitHubClient, index: LibraryIndex, tagging: dict, post: Post | None = None,
                 shallow_limit: int = 1000, holdout_every: int = 0, request_budget: int | None = None):
        """holdout_every: N이면 판정을 통과한 N번째 이슈마다 평가용으로 남기고 색인하지 않는다 (C 방식 검증용)."""
        self.client = client
        self.index = index
        self.raw = RawStore(index.raw_dir)
        self.rules = TaggingRules.from_config(tagging, {})
        self.post = post
        self.shallow_limit = shallow_limit
        self.holdout_every = holdout_every
        self.request_budget = request_budget

    def _over_budget(self, start: int) -> bool:
        return self.request_budget is not None and self.client.stats.requests - start >= self.request_budget

    def _search(self, repo: str, since: str | None) -> list[dict]:
        query = f"repo:{repo} is:issue is:closed" + (f" closed:>={since}" if since else "")
        records = []
        for page in self.client.paginate("/search/issues", {"q": query, "sort": "updated", "order": "desc", "per_page": 100},
                                         max_pages=max(1, self.shallow_limit // 100)):
            records += [to_shallow_record(repo, item) for item in (page.data or {}).get("items", [])
                        if "pull_request" not in item]
            if len(records) >= self.shallow_limit:
                break
        return records[: self.shallow_limit]

    def build(self, library: Library, deep_target: int, state: dict, mode: str = "full") -> BuildReport:
        repo = library.repo
        report = BuildReport(repo, mode)
        start = self.client.stats.requests
        entry = (state.get(repo) or {}) if mode == "incremental" else {}
        known = set(entry.get("issues", [])) | set(entry.get("held_out", []))
        since = entry.get("built_at", "")[:10] if mode == "incremental" else None

        try:
            shallow = self._search(repo, since)
        except GitHubAPIError as exc:
            report.errors.append(f"검색 실패: {exc.status} {exc.reason}")
            return report
        report.searched = len(shallow)
        candidates = []
        for record in shallow:
            if record["issue_id"] in known:
                continue
            tag = tag_issue(record, self.rules, {})
            reason = prefilter(tag, record)
            if reason:
                report.rejected[reason] = report.rejected.get(reason, 0) + 1
            else:
                candidates.append((record, tag))
        report.prefiltered = len(candidates)
        candidates.sort(key=lambda c: (c[0].get("closed_at") or ""), reverse=True)

        items, documents, indexed, held_out, accepted = [], [], [], [], 0
        closed_issues: list[dict] = []  # 유지보수자 추정용 (이슈를 여러 번 닫은 계정)
        for record, tag in candidates[: deep_target * DETAIL_ATTEMPTS]:
            if accepted >= deep_target:
                break
            if self._over_budget(start):
                report.stopped = f"요청 예산 {self.request_budget}회 도달"
                break
            try:
                summary = collect_issue_detail(self.client, repo, record["number"], self.raw, include_comments=True,
                                               include_timeline=True, include_linked_prs=True, max_linked_prs=3)
            except (GitHubAPIError, NotAnIssueError) as exc:
                report.errors.append(f"{record['issue_id']}: {exc}")
                continue
            report.detailed += 1
            issue = load_raw(self.raw, repo, "issues", record["number"]) or {}
            comments = load_raw(self.raw, repo, "comments", record["number"]) or []
            closed_issues.append(issue)
            maintainers = infer_maintainers(closed_issues)
            evidence = judge_evidence(summary, comments, (issue.get("user") or {}).get("login"), maintainers)
            kind = resolution_kind(record, tag, evidence)
            if not kind:
                # 어디서 빠지는지 보이게 종료 사유와 근거 수준을 붙인다 (예: no_resolution:not_planned/weak)
                reason = f"no_resolution:{record.get('state_reason')}/{evidence['evidence_level']}"
                report.rejected[reason] = report.rejected.get(reason, 0) + 1
                continue
            accepted += 1
            if self.holdout_every and accepted % self.holdout_every == 0:
                held_out.append(record["issue_id"])
                continue
            row = {"issue_id": record["issue_id"], "repo": repo, "number": record["number"], "final_split": "library",
                   **evidence}
            item = build_issue_item(issue, tag, row, summary)
            marks = {"corpus": "library", "resolution_kind": kind, "library": repo}
            docs, _ = build_issue_documents(issue, comments, summary, item, self.raw, maintainers)
            items.append({**item, **marks})
            documents += [{**d, **marks} for d in docs]
            indexed.append(record["issue_id"])
            report.kinds[kind] = report.kinds.get(kind, 0) + 1

        if library.discussions and self.post and not report.stopped:
            for node in fetch_discussions(self.post, repo, max(1, deep_target // 3), report.errors):
                if f"{repo}#{node['number']}" in known:
                    continue
                item, docs = discussion_item_and_documents(repo, node)
                items.append(item)
                documents += docs
                indexed.append(item["issue_id"])
                report.discussions += 1
            report.kinds["discussion"] = report.discussions

        report.chunks = self.index.upsert(items, documents) if items else 0
        report.indexed_issues, report.held_out = len(indexed) - report.discussions, len(held_out)
        report.requests = self.client.stats.requests - start
        state[repo] = {"built_at": utc_now(), "mode": mode, "embedded": self.index.embedder is not None,
                       "issues": sorted(known - set(entry.get("held_out", [])) | set(indexed)),
                       "held_out": sorted(set(entry.get("held_out", [])) | set(held_out)),
                       "kinds": {k: entry.get("kinds", {}).get(k, 0) + v for k, v in report.kinds.items()}}
        return report


def ensure_libraries(builder: LibraryCorpusBuilder, corpus: Corpus, resolutions: list[Resolution], root: Path,
                     deep_target: int, max_new: int, refresh_days: int) -> list[dict]:
    """A 방식: 대상 레포가 쓰는 큐레이션 라이브러리만 구축한다. 한 번에 새로 구축하는 수에 상한을 둔다.

    반환: 라이브러리마다 {repo, status: ready|built|refreshed|deferred|failed, report}"""
    state = load_state(root)
    by_repo = corpus.by_repo()
    repos = list(dict.fromkeys(r.repo for r in resolutions if r.curated and r.repo and r.repo.lower() in by_repo))
    results, built = [], 0
    for repo in repos:
        library = by_repo[repo.lower()]
        mode = needs_build(state, library.repo, refresh_days, embedded=builder.index.embedder is not None)
        if mode is None:
            results.append({"repo": library.repo, "status": "ready"})
            continue
        if built >= max_new:
            results.append({"repo": library.repo, "status": "deferred"})
            continue
        report = builder.build(library, deep_target, state, mode)
        built += 1
        save_state(root, state)
        status = "failed" if report.errors and not report.indexed_issues else ("built" if mode == "full" else "refreshed")
        results.append({"repo": library.repo, "status": status, "report": report.as_dict()})
    return results
