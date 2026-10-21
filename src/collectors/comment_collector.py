"""이슈 댓글 수집과 색인 후보 우선순위 계산."""
from __future__ import annotations

import re

from src.collectors.cache import RawStore
from src.collectors.github_client import GitHubClient

MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
CODE_BLOCK = re.compile(r"```")
VERSION = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
ERROR_TEXT = re.compile(r"\b\w+(?:Exception|Error)\b|Traceback \(most recent call last\)")
RESOLUTION_SIGNAL = re.compile(r"\b(fixed|resolved|workaround|reproduc\w*|released)\b", re.IGNORECASE)
MANY_REACTIONS = 3

# 기획서 5.5절 단계 C의 우선순위를 그대로 따르도록, 상위 신호가 하위 신호 합보다 항상 크게 둔다
PRIORITY_WEIGHTS = {
    "maintainer": 16,
    "technical_detail": 8,
    "resolution_signal": 4,
    "reactions": 2,
    "issue_author": 1,
}


def fetch_comments(client: GitHubClient, repo: str, number: int, raw_store: RawStore) -> list[dict]:
    comments: list[dict] = []
    for page in client.paginate(f"/repos/{repo}/issues/{number}/comments", {"per_page": 100}):
        comments.extend(page.data)
    raw_store.save(repo, "comments", str(number), comments)
    return comments


def comment_priority(comment: dict, issue_author: str | None = None) -> dict:
    """후보 정렬용 점수. 낮은 점수의 댓글도 삭제하지 않고 프롬프트 2의 선별 대상으로 남긴다."""
    body = comment.get("body") or ""
    signals = []
    if comment.get("author_association") in MAINTAINER_ASSOCIATIONS:
        signals.append("maintainer")
    if CODE_BLOCK.search(body) or VERSION.search(body) or ERROR_TEXT.search(body):
        signals.append("technical_detail")
    if RESOLUTION_SIGNAL.search(body):
        signals.append("resolution_signal")
    if (comment.get("reactions") or {}).get("total_count", 0) >= MANY_REACTIONS:
        signals.append("reactions")
    author = (comment.get("user") or {}).get("login")
    if issue_author and author == issue_author:
        signals.append("issue_author")
    return {
        "comment_id": comment.get("id"),
        "score": sum(PRIORITY_WEIGHTS[s] for s in signals),
        "signals": signals,
    }
