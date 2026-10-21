"""Search API 기반 종료 이슈 얕은 수집, 이슈 상세 조회, 저장소 라이선스 조회."""
from __future__ import annotations

from dataclasses import dataclass, field

from src.collectors.cache import RawStore
from src.collectors.github_client import GitHubAPIError, GitHubClient

DEFAULT_QUERY = "is:issue is:closed"
SEARCH_PAGE_SIZE = 100
SEARCH_RESULT_CAP = 1000  # Search API는 쿼리당 최대 1,000건만 반환한다


class NotAnIssueError(Exception):
    """이슈 번호로 조회한 객체가 Pull Request였다."""


@dataclass
class SearchResult:
    repo: str
    issues: list[dict] = field(default_factory=list)
    total_count: int = 0
    pages: int = 0
    incomplete: bool = False
    skipped_pull_requests: int = 0


def build_search_query(repo: str, query: str = DEFAULT_QUERY, since: str | None = None) -> str:
    q = f"repo:{repo} {query}"
    if since:
        q += f" updated:>={since}"
    return q


def to_shallow_record(repo: str, item: dict) -> dict:
    """태깅과 후보 선별에 필요한 필드만 남긴다."""
    return {
        "issue_id": f"{repo}#{item['number']}",
        "repo": repo,
        "number": item["number"],
        "title": item.get("title") or "",
        "body": item.get("body") or "",
        "labels": [label["name"] for label in item.get("labels", [])],
        "state": item.get("state"),
        "state_reason": item.get("state_reason"),
        "comments": item.get("comments", 0),
        "author_association": item.get("author_association"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "closed_at": item.get("closed_at"),
        "html_url": item.get("html_url"),
    }


def search_closed_issues(
    client: GitHubClient,
    repo: str,
    raw_store: RawStore,
    query: str = DEFAULT_QUERY,
    limit: int = SEARCH_RESULT_CAP,
    max_pages: int | None = None,
    since: str | None = None,
) -> SearchResult:
    """종료 이슈를 최근 갱신순으로 limit건까지 수집한다. 댓글, 타임라인, PR은 호출하지 않는다."""
    params = {
        "q": build_search_query(repo, query, since),
        "sort": "updated",
        "order": "desc",
        "per_page": SEARCH_PAGE_SIZE,
    }
    limit = min(limit, SEARCH_RESULT_CAP)
    result = SearchResult(repo=repo)
    seen: set[int] = set()

    for page in client.paginate("/search/issues", params, max_pages=max_pages):
        result.pages += 1
        raw_store.save(repo, "search", f"closed_page_{result.pages:03d}", page.data)
        result.total_count = page.data.get("total_count", 0)
        if page.data.get("incomplete_results"):
            result.incomplete = True
        for item in page.data.get("items", []):
            if "pull_request" in item:
                result.skipped_pull_requests += 1
                continue
            # 수집 중 갱신된 이슈는 다음 페이지에 다시 나올 수 있다
            if item["number"] in seen:
                continue
            seen.add(item["number"])
            result.issues.append(to_shallow_record(repo, item))
            if len(result.issues) >= limit:
                return result
    return result


def fetch_issue(client: GitHubClient, repo: str, number: int, raw_store: RawStore) -> dict:
    data = client.get(f"/repos/{repo}/issues/{number}").data
    if "pull_request" in data:
        raise NotAnIssueError(f"{repo}#{number}는 Pull Request입니다")
    raw_store.save(repo, "issues", str(number), data)
    return data


def fetch_repository_license(client: GitHubClient, repo: str, raw_store: RawStore) -> dict:
    """저장소 라이선스(SPDX)를 기록한다. 라이선스 파일이 없으면 None 값으로 반환한다."""
    try:
        data = client.get(f"/repos/{repo}/license").data
    except GitHubAPIError as exc:
        if exc.status == 404:
            return {"spdx_id": None, "name": None, "url": None}
        raise
    raw_store.save(repo, "repository", "license", {k: v for k, v in data.items() if k != "content"})
    license_info = data.get("license") or {}
    return {
        "spdx_id": license_info.get("spdx_id"),
        "name": license_info.get("name"),
        "url": data.get("html_url"),
    }
