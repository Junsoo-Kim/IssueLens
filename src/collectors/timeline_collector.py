"""타임라인 수집과 연결 PR, 커밋, 중복 이슈, 참조 URL 추출."""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable
from urllib.parse import urlparse

from src.collectors.cache import RawStore
from src.collectors.github_client import GitHubClient

PULL_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
CLOSING_REF = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+(?:by\s+|in\s+)?#(\d+)\b", re.IGNORECASE)
DUPLICATE_REF = re.compile(r"\bduplicate\s+of\s+#(\d+)\b", re.IGNORECASE)
URL = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
URL_TRAILING = ".,;:!?*_"


def fetch_timeline(client: GitHubClient, repo: str, number: int, raw_store: RawStore) -> list[dict]:
    events: list[dict] = []
    for page in client.paginate(f"/repos/{repo}/issues/{number}/timeline", {"per_page": 100}):
        events.extend(page.data)
    raw_store.save(repo, "timeline", str(number), events)
    return events


def _source_repo(source_issue: dict) -> str | None:
    full_name = (source_issue.get("repository") or {}).get("full_name")
    if full_name:
        return full_name
    match = re.match(r"https://github\.com/([\w.-]+/[\w.-]+)/", source_issue.get("html_url") or "")
    return match.group(1) if match else None


def extract_links(timeline: list[dict], repo: str, texts: Iterable[str] = (), issue_number: int | None = None) -> dict:
    """타임라인 이벤트와 본문/댓글 텍스트에서 같은 저장소의 연결 PR, 커밋, 중복 관계를 추출한다.

    텍스트에서 찾은 PR 번호는 이슈 번호일 수도 있으므로 source="text"로 구분하고,
    PR 조회 단계에서 404이면 버린다.
    """
    repo_lower = repo.lower()
    pull_requests: dict[int, str] = {}
    commits: dict[str, dict] = {}
    external_references: list[str] = []
    duplicates: list[int] = []
    closed_by_commit = None

    for event in timeline:
        kind = event.get("event")
        if kind == "cross-referenced":
            source_issue = (event.get("source") or {}).get("issue") or {}
            if "pull_request" not in source_issue:
                continue
            if (_source_repo(source_issue) or "").lower() == repo_lower:
                pull_requests.setdefault(source_issue["number"], "timeline")
            elif source_issue.get("html_url"):
                external_references.append(source_issue["html_url"])
        elif kind == "committed" and event.get("sha"):
            commits.setdefault(event["sha"], {"sha": event["sha"], "url": event.get("html_url"), "event": kind})
        elif kind in ("referenced", "closed") and event.get("commit_id"):
            sha = event["commit_id"]
            commits.setdefault(sha, {"sha": sha, "url": event.get("commit_url"), "event": kind})
            if kind == "closed":
                closed_by_commit = sha

    for text in texts:
        if not text:
            continue
        for match in PULL_URL.finditer(text):
            if match.group(1).lower() == repo_lower:
                pull_requests.setdefault(int(match.group(2)), "text")
        for match in CLOSING_REF.finditer(text):
            pull_requests.setdefault(int(match.group(1)), "text")
        for match in DUPLICATE_REF.finditer(text):
            number = int(match.group(1))
            if number not in duplicates:
                duplicates.append(number)

    pull_requests.pop(issue_number, None)
    return {
        "pull_requests": [{"number": n, "source": s} for n, s in pull_requests.items()],
        "commits": list(commits.values()),
        "closed_by_commit": closed_by_commit,
        "duplicates": [n for n in duplicates if n != issue_number],
        "external_references": list(dict.fromkeys(external_references)),
        "event_counts": dict(Counter(e.get("event") for e in timeline if e.get("event"))),
    }


def extract_reference_urls(texts: Iterable[str], repo: str, docs_allowlist: Iterable[str] = ()) -> dict:
    """본문/댓글의 URL을 공식 문서, 같은 저장소 GitHub 링크, 기타로 분류한다.

    공식 문서 본문은 이 단계에서 수집하지 않고 URL만 기록한다.
    """
    allowlist = [domain.lower() for domain in docs_allowlist]
    repo_prefix = f"/{repo.lower()}/"
    found = {"official_docs": [], "github": [], "other": []}
    seen: set[str] = set()
    for text in texts:
        for match in URL.finditer(text or ""):
            url = match.group(0).rstrip(URL_TRAILING)
            if url in seen:
                continue
            seen.add(url)
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if any(host == d or host.endswith("." + d) for d in allowlist):
                found["official_docs"].append(url)
            elif host == "github.com" and (parsed.path.lower() + "/").startswith(repo_prefix):
                found["github"].append(url)
            else:
                found["other"].append(url)
    return found
