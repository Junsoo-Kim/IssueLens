"""상세 수집 원본으로 RAG 1 항목과 RAG 2 문서를 만든다. 사전 색인(build_indexes.py)과 질문 시점 수집이 함께 쓴다."""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Iterable

from src.collectors.cache import RawStore
from src.preprocessing.document_builder import (
    build_comment_documents,
    build_issue_body,
    build_pull_request_documents,
)
from src.preprocessing.metadata_builder import build_issue_item
from src.preprocessing.resolution import infer_maintainers


def load_raw(raw_store: RawStore, repo: str, kind: str, number: int):
    path = raw_store.path(repo, kind, str(number))
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def repo_maintainers(raw_store: RawStore, pool: Iterable[dict]) -> dict[str, set[str]]:
    """pool: [{"repo", "number"}] 상세 수집 이슈. 저장소별로 이슈를 여러 번 닫은 계정을 유지보수자로 본다."""
    numbers: dict[str, list[int]] = defaultdict(list)
    for row in pool:
        numbers[row["repo"]].append(row["number"])
    return {repo: infer_maintainers(filter(None, (load_raw(raw_store, repo, "issues", n) for n in ns)))
            for repo, ns in numbers.items()}


def build_issue_documents(issue: dict, comments: list[dict], summary: dict, item: dict, raw_store: RawStore,
                          maintainers: set[str]) -> tuple[list[dict], list[dict]]:
    """이슈 1건의 RAG 2 문서(본문, 댓글, 병합된 연결 PR)와 탈락 문서."""
    repo, issue_id = item["repo"], item["issue_id"]
    documents, rejected = [build_issue_body(issue, item)], []
    comment_docs, comment_rejected = build_comment_documents(
        comments, item, (issue.get("user") or {}).get("login"), maintainers)
    documents.extend(comment_docs)
    rejected.extend(comment_rejected)
    for pr_summary in summary.get("linked_pull_requests", []):
        pr = load_raw(raw_store, repo, "pulls", pr_summary["number"])
        files = load_raw(raw_store, repo, "pull_files", pr_summary["number"]) or []
        if pr is None:
            rejected.append({"document_id": f"{issue_id}:pull_request:{pr_summary['number']}", "issue_id": issue_id,
                             "document_type": "pull_request", "reject_reason": "raw_missing"})
            continue
        pr_docs, pr_rejected = build_pull_request_documents(pr, files, item)
        documents.extend(pr_docs)
        rejected.extend(pr_rejected)
    return documents, rejected


def build_processed(selection: list[dict], tags: dict, summaries: dict, deep_pool: list[dict], raw_store: RawStore):
    """색인 대상 이슈로 RAG 1 항목, RAG 2 문서, 탈락 문서를 만든다."""
    maintainers = repo_maintainers(raw_store, deep_pool)
    items, documents, rejected, missing = [], [], [], []
    for row in selection:
        repo, number, issue_id = row["repo"], row["number"], row["issue_id"]
        issue = load_raw(raw_store, repo, "issues", number)
        comments = load_raw(raw_store, repo, "comments", number)
        if issue is None or comments is None or issue_id not in summaries:
            missing.append(issue_id)
            continue
        summary = summaries[issue_id]
        item = build_issue_item(issue, tags.get(issue_id, {}), row, summary)
        items.append(item)
        issue_documents, issue_rejected = build_issue_documents(
            issue, comments, summary, item, raw_store, maintainers.get(repo, set()))
        documents.extend(issue_documents)
        rejected.extend(issue_rejected)
    return items, documents, rejected, missing, {repo: len(m) for repo, m in maintainers.items()}
