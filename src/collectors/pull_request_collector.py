"""연결 PR 상세와 변경 파일(patch) 수집."""
from __future__ import annotations

from src.collectors.cache import RawStore
from src.collectors.github_client import GitHubAPIError, GitHubClient


def fetch_pull_request(client: GitHubClient, repo: str, number: int, raw_store: RawStore) -> dict | None:
    """PR 상세를 조회한다. 텍스트에서 찾은 번호가 PR이 아니면(404) None을 반환한다."""
    try:
        data = client.get(f"/repos/{repo}/pulls/{number}").data
    except GitHubAPIError as exc:
        if exc.status == 404:
            return None
        raise
    raw_store.save(repo, "pulls", str(number), data)
    return data


def fetch_pull_files(client: GitHubClient, repo: str, number: int, raw_store: RawStore) -> list[dict]:
    files: list[dict] = []
    for page in client.paginate(f"/repos/{repo}/pulls/{number}/files", {"per_page": 100}):
        files.extend(page.data)
    raw_store.save(repo, "pull_files", str(number), files)
    return files


def summarize_pull_request(pr: dict, files: list[dict]) -> dict:
    """patch가 없거나 잘린 파일은 복원하지 않고 patch_available=false로 기록한다."""
    changed_files = [
        {
            "filename": f.get("filename"),
            "status": f.get("status"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "patch_available": bool(f.get("patch")),
        }
        for f in files
    ]
    return {
        "number": pr["number"],
        "title": pr.get("title"),
        "state": pr.get("state"),
        "merged": bool(pr.get("merged") or pr.get("merged_at")),
        "merged_at": pr.get("merged_at"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "html_url": pr.get("html_url"),
        "changed_files": changed_files,
        "patch_available": bool(changed_files) and all(f["patch_available"] for f in changed_files),
    }
