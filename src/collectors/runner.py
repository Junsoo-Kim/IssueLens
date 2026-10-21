"""수집 실행 흐름: 얕은 수집, 상세 수집, dry-run과 결과 파일 기록."""
from __future__ import annotations

import json
from pathlib import Path

from src.collectors.cache import (
    Manifest,
    RawStore,
    read_jsonl,
    repo_slug,
    utc_now,
    write_json_atomic,
    write_jsonl_atomic,
)
from src.collectors.comment_collector import fetch_comments
from src.collectors.github_client import GitHubAPIError, GitHubClient
from src.collectors.issue_collector import (
    DEFAULT_QUERY,
    SEARCH_RESULT_CAP,
    NotAnIssueError,
    build_search_query,
    fetch_issue,
    fetch_repository_license,
    search_closed_issues,
)
from src.collectors.pull_request_collector import (
    fetch_pull_files,
    fetch_pull_request,
    summarize_pull_request,
)
from src.collectors.timeline_collector import extract_links, extract_reference_urls, fetch_timeline

SHALLOW_OUTPUT = "shallow_issues.jsonl"
DEEP_OUTPUT = "deep_issues.jsonl"
SOURCE_MANIFEST = "source_manifest.json"
DEFAULT_MAX_LINKED_PRS = 5


def _failure(key: str, exc: GitHubAPIError) -> dict:
    return {"key": key, "url": exc.url, "http_status": exc.status, "reason": exc.reason, "retryable": exc.retryable}


def _record_failure(stats: dict, manifest: Manifest, key: str, exc: GitHubAPIError) -> bool:
    """실패를 manifest와 통계에 남긴다. 일시적 오류가 재시도 한도를 넘으면 True(수집 중단)."""
    failure = _failure(key, exc)
    manifest.mark(key, "failed", **{k: v for k, v in failure.items() if k != "key"})
    stats["failures"].append(failure)
    if exc.retryable:
        stats["aborted"] = failure
        return True
    return False


def run_shallow(
    client: GitHubClient,
    repositories: list[dict],
    raw_store: RawStore,
    manifest: Manifest,
    processed_dir: Path,
    *,
    query: str = DEFAULT_QUERY,
    candidate_limit: int | None = None,
    max_pages: int | None = None,
    since: str | None = None,
    resume: bool = False,
) -> dict:
    """저장소별 종료 이슈 후보를 얕게 수집하고 shallow_issues.jsonl로 합친다."""
    processed_dir = Path(processed_dir)
    per_repo_dir = processed_dir / "shallow"
    stats = {"mode": "shallow", "started_at": utc_now(), "query": query, "repositories": [], "failures": [], "aborted": None}

    for repo_config in repositories:
        repo = repo_config["repo"]
        key = f"shallow:{repo}"
        if resume and manifest.is_done(key):
            record = manifest.get(key)
            stats["repositories"].append({"repo": repo, "status": "skipped", "collected": record.get("collected", 0)})
            continue

        limit = candidate_limit or repo_config.get("shallow_candidate_limit", SEARCH_RESULT_CAP)
        try:
            license_info = fetch_repository_license(client, repo, raw_store)
            result = search_closed_issues(
                client, repo, raw_store, query=query, limit=limit, max_pages=max_pages, since=since
            )
        except GitHubAPIError as exc:
            if _record_failure(stats, manifest, key, exc):
                break
            stats["repositories"].append({"repo": repo, "status": "failed", "collected": 0})
            continue

        write_jsonl_atomic(per_repo_dir / f"{repo_slug(repo)}.jsonl", result.issues)
        # incomplete_results가 있으면 완료로 취급하지 않아 --resume 시 다시 수집한다
        status = "incomplete" if result.incomplete else "done"
        entry = {
            "collected": len(result.issues),
            "target": limit,
            "total_closed": result.total_count,
            "pages": result.pages,
            "skipped_pull_requests": result.skipped_pull_requests,
            "license": license_info,
        }
        manifest.mark(key, status, **entry)
        _update_source_manifest(manifest.path.parent / SOURCE_MANIFEST, repo, query, license_info)
        stats["repositories"].append({"repo": repo, "status": status, **entry})
        if result.incomplete:
            stats["failures"].append({"key": key, "reason": "incomplete_results", "retryable": True})

    merged = _merge_shallow_outputs(per_repo_dir, processed_dir / SHALLOW_OUTPUT)
    stats["totals"] = {
        "shallow_candidates": merged,
        "below_target": [
            r["repo"] for r in stats["repositories"]
            if r["status"] in ("done", "incomplete") and r["collected"] < min(r["target"], r["total_closed"])
        ],
    }
    stats["client"] = client.stats.as_dict()
    stats["finished_at"] = utc_now()
    return stats


def _merge_shallow_outputs(per_repo_dir: Path, output_path: Path) -> int:
    records: list[dict] = []
    for path in sorted(per_repo_dir.glob("*.jsonl")):
        records.extend(read_jsonl(path))
    return write_jsonl_atomic(output_path, records)


def _update_source_manifest(path: Path, repo: str, query: str, license_info: dict) -> None:
    """출처와 이용 조건 기록 (기획서 5.13절)."""
    data = {"repositories": {}}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    data["repositories"][repo] = {
        "source": f"https://github.com/{repo}/issues",
        "query": query,
        "license": license_info,
        "collected_at": utc_now(),
        "note": "코드 라이선스가 이슈와 댓글의 사용자 작성 내용에 자동 적용된다고 가정하지 않는다.",
    }
    write_json_atomic(path, data)


def load_selection(path: Path) -> list[tuple[str, int]]:
    """상세 수집 대상 파일을 읽는다. 각 줄은 {"repo", "number"} 또는 {"issue_id": "owner/repo#n"}."""
    selection: list[tuple[str, int]] = []
    for record in read_jsonl(path):
        if "issue_id" in record:
            repo, number = record["issue_id"].rsplit("#", 1)
        else:
            repo, number = record["repo"], record["number"]
        item = (repo, int(number))
        if item not in selection:
            selection.append(item)
    return selection


def collect_issue_detail(
    client: GitHubClient,
    repo: str,
    number: int,
    raw_store: RawStore,
    *,
    docs_allowlist: list[str] = (),
    include_comments: bool = False,
    include_timeline: bool = False,
    include_linked_prs: bool = False,
    max_linked_prs: int = DEFAULT_MAX_LINKED_PRS,
) -> dict:
    issue = fetch_issue(client, repo, number, raw_store)
    comments = fetch_comments(client, repo, number, raw_store) if include_comments else []
    timeline = fetch_timeline(client, repo, number, raw_store) if include_timeline else []

    texts = [issue.get("body") or ""] + [c.get("body") or "" for c in comments]
    links = extract_links(timeline, repo, texts, issue_number=number)

    pull_requests = []
    if include_linked_prs:
        for ref in links["pull_requests"][:max_linked_prs]:
            pr = fetch_pull_request(client, repo, ref["number"], raw_store)
            if pr is None:
                continue
            files = fetch_pull_files(client, repo, ref["number"], raw_store)
            pull_requests.append({**summarize_pull_request(pr, files), "link_source": ref["source"]})

    return {
        "issue_id": f"{repo}#{number}",
        "repo": repo,
        "number": number,
        "state_reason": issue.get("state_reason"),
        "updated_at": issue.get("updated_at"),
        "comments_collected": len(comments),
        "timeline_events": links["event_counts"],
        "linked_pull_requests": pull_requests,
        "commits": links["commits"],
        "closed_by_commit": links["closed_by_commit"],
        "duplicates": links["duplicates"],
        "external_references": links["external_references"],
        "reference_urls": extract_reference_urls(texts, repo, docs_allowlist),
        "collected_at": utc_now(),
    }


def run_deep(
    client: GitHubClient,
    selection: list[tuple[str, int]],
    raw_store: RawStore,
    manifest: Manifest,
    processed_dir: Path,
    *,
    docs_allowlists: dict[str, list[str]] | None = None,
    include_comments: bool = False,
    include_timeline: bool = False,
    include_linked_prs: bool = False,
    max_linked_prs: int = DEFAULT_MAX_LINKED_PRS,
    resume: bool = False,
) -> dict:
    """선택된 이슈만 상세 수집하고 deep_issues.jsonl을 만든다."""
    docs_allowlists = docs_allowlists or {}
    counts = {"selected": len(selection), "collected": 0, "skipped": 0, "failed": 0}
    stats = {"mode": "deep", "started_at": utc_now(), "issues": counts, "failures": [], "aborted": None}

    for repo, number in selection:
        key = f"deep:{repo}#{number}"
        if resume and manifest.is_done(key):
            counts["skipped"] += 1
            continue
        try:
            summary = collect_issue_detail(
                client, repo, number, raw_store,
                docs_allowlist=docs_allowlists.get(repo, []),
                include_comments=include_comments,
                include_timeline=include_timeline,
                include_linked_prs=include_linked_prs,
                max_linked_prs=max_linked_prs,
            )
        except NotAnIssueError as exc:
            manifest.mark(key, "failed", reason=str(exc), retryable=False)
            stats["failures"].append({"key": key, "reason": str(exc), "retryable": False})
            counts["failed"] += 1
            continue
        except GitHubAPIError as exc:
            counts["failed"] += 1
            if _record_failure(stats, manifest, key, exc):
                break
            continue
        manifest.mark(key, "done", summary=summary)
        counts["collected"] += 1

    records = [
        manifest.get(f"deep:{repo}#{number}")["summary"]
        for repo, number in selection
        if manifest.is_done(f"deep:{repo}#{number}")
    ]
    stats["totals"] = {"deep_issues": write_jsonl_atomic(Path(processed_dir) / DEEP_OUTPUT, records)}
    stats["client"] = client.stats.as_dict()
    stats["finished_at"] = utc_now()
    return stats


def dry_run(client: GitHubClient, repositories: list[dict], query: str = DEFAULT_QUERY, since: str | None = None) -> dict:
    """저장소별 검색 건수와 Rate Limit만 확인한다. 원본과 캐시는 기록하지 않는다."""
    results = []
    for repo_config in repositories:
        repo = repo_config["repo"]
        params = {"q": build_search_query(repo, query, since), "per_page": 1}
        data = client.get("/search/issues", params, use_cache=False).data
        results.append({
            "repo": repo,
            "total_closed": data.get("total_count", 0),
            "incomplete": bool(data.get("incomplete_results")),
            "candidate_limit": repo_config.get("shallow_candidate_limit", SEARCH_RESULT_CAP),
        })
    resources = client.rate_limit().get("resources", {})
    return {
        "mode": "dry-run",
        "query": query,
        "repositories": results,
        "rate_limit": {name: resources[name] for name in ("core", "search") if name in resources},
    }
