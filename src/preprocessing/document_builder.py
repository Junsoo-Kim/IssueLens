"""이슈 본문, 댓글, 연결 PR, PR 변경 내용을 RAG 2(issue_documents) 문서로 만든다 (기획서 5.10절).

이슈 본문은 모든 색인 이슈의 필수 문서다. 댓글과 PR은 규칙으로 1차 선별하고,
탈락한 문서는 reject_reason과 함께 기록한다. LLM 선별(프롬프트 2)은 질의 시점에 적용한다.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from src.collectors.comment_collector import comment_priority
from src.preprocessing.metadata_builder import content_hash
from src.preprocessing.normalizer import normalize_markdown
from src.preprocessing.resolution import is_bot
from src.preprocessing.secret_redactor import redact

MIN_COMMENT_CHARS = 30
MAX_COMMENTS_PER_ISSUE = 15
MAX_PATCH_FILES_PER_PR = 5
MAX_PATCH_CHARS_PER_FILE = 6_000
NOISE_COMMENT = re.compile(r"^\s*(\+1|same( here| issue)?|me too|thanks?( you)?!*|thank you!*|any update\??|bump)\W*$", re.I)
SKIPPED_PATCH_FILES = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|go\.sum|Cargo\.lock)$"
    r"|\.(snap|svg|png|jpe?g|gif|ico|min\.js|map|lock)$"
    r"|(^|/)(locales?|i18n|translations?)/",
    re.I,
)
LICENSE_NOTE = "공개 GitHub 이슈와 PR. 교육 목적의 필요한 범위만 사용하며 원문을 재배포하지 않는다."


def _clean(text: str | None) -> tuple[str, bool, dict[str, int]]:
    redacted, counts = redact(text or "")
    normalized, truncated = normalize_markdown(redacted)
    return normalized, truncated, counts


def _document(issue_item: dict, document_id: str, document_type: str, title: str, body: str, **fields) -> dict:
    return {
        "document_id": document_id,
        "issue_id": issue_item["issue_id"],
        "repo": issue_item["repo"],
        "issue_title": issue_item["title"],
        "component": issue_item["component"],
        "document_type": document_type,
        "title": title,
        "body": body,
        "content_hash": content_hash(f"{title}\n{body}"),
        "license_note": LICENSE_NOTE,
        **fields,
    }


def _role(user_login: str | None, association: str | None, issue_author: str | None, maintainers: set[str]) -> str:
    if association in {"OWNER", "MEMBER", "COLLABORATOR"} or user_login in maintainers:
        return "maintainer"
    if user_login and user_login == issue_author:
        return "reporter"
    return "other"


def build_issue_body(issue: dict, issue_item: dict) -> dict:
    body, truncated, counts = _clean(issue.get("body"))
    return _document(
        issue_item, f"{issue_item['issue_id']}:issue_body", "issue_body", issue_item["title"], body,
        author_association=issue.get("author_association"), author_role="reporter",
        source_url=issue.get("html_url"), source_type="github_issue",
        source_updated_at=issue.get("updated_at"), selection_reason="필수 문서: 이슈 본문",
        truncated=truncated, redaction_counts=counts,
    )


def build_comment_documents(comments: list[dict], issue_item: dict, issue_author: str | None,
                            maintainers: set[str]) -> tuple[list[dict], list[dict]]:
    candidates, rejected = [], []
    for c in comments:
        login = (c.get("user") or {}).get("login")
        reject = None
        body, truncated, counts = _clean(c.get("body"))
        if is_bot(c):
            reject = "bot_comment"
        elif len(body) < MIN_COMMENT_CHARS or NOISE_COMMENT.match(body):
            reject = "noise_or_too_short"
        if reject:
            rejected.append({"document_id": f"{issue_item['issue_id']}:comment:{c.get('id')}",
                             "issue_id": issue_item["issue_id"], "document_type": "comment", "reject_reason": reject})
            continue
        role = _role(login, c.get("author_association"), issue_author, maintainers)
        priority = comment_priority(c, issue_author)
        # 추정 유지보수자도 유지보수자 가중치를 받도록 점수를 보정한다
        score = priority["score"] + (16 if role == "maintainer" and "maintainer" not in priority["signals"] else 0)
        candidates.append((score, c, body, truncated, counts, role, priority["signals"]))

    # 점수 상위만 남기되 문서 순서는 작성 시각 순으로 둔다
    kept = sorted(candidates, key=lambda x: -x[0])[:MAX_COMMENTS_PER_ISSUE]
    kept_ids = {id(x[1]) for x in kept}
    for score, c, *_ in candidates:
        if id(c) not in kept_ids:
            rejected.append({"document_id": f"{issue_item['issue_id']}:comment:{c.get('id')}",
                             "issue_id": issue_item["issue_id"], "document_type": "comment",
                             "reject_reason": f"over_limit(score={score})"})
    documents = []
    for score, c, body, truncated, counts, role, signals in sorted(kept, key=lambda x: x[1].get("created_at") or ""):
        documents.append(_document(
            issue_item, f"{issue_item['issue_id']}:comment:{c.get('id')}", "comment",
            f"{issue_item['title']} — 댓글", body,
            author_association=c.get("author_association"), author_role=role,
            source_url=c.get("html_url") or issue_item["source_url"], source_type="github_comment",
            source_updated_at=c.get("updated_at"), priority_score=score,
            selection_reason=", ".join(signals) or "일반 댓글", truncated=truncated, redaction_counts=counts,
        ))
    return documents, rejected


def build_pull_request_documents(pr: dict, files: list[dict], issue_item: dict) -> tuple[list[dict], list[dict]]:
    """병합된 연결 PR의 설명과 변경 파일 patch를 문서로 만든다."""
    issue_id = issue_item["issue_id"]
    number = pr["number"]
    if not (pr.get("merged") or pr.get("merged_at")):
        return [], [{"document_id": f"{issue_id}:pull_request:{number}", "issue_id": issue_id,
                     "document_type": "pull_request", "reject_reason": "pr_not_merged"}]

    body, truncated, counts = _clean(pr.get("body"))
    file_list = "\n".join(f"- {f.get('filename')} ({f.get('status')}, +{f.get('additions', 0)}/-{f.get('deletions', 0)})"
                          for f in files)
    description = f"{body}\n\n변경 파일:\n{file_list}".strip()
    documents = [_document(
        issue_item, f"{issue_id}:pull_request:{number}", "pull_request", f"PR #{number}: {pr.get('title') or ''}",
        description, author_association=pr.get("author_association"), author_role="contributor",
        source_url=pr.get("html_url"), source_type="github_pull_request", source_updated_at=pr.get("updated_at"),
        merged_at=pr.get("merged_at"), selection_reason="병합된 연결 PR", truncated=truncated, redaction_counts=counts,
    )]
    rejected = []
    patch_files = 0
    for f in files:
        filename = f.get("filename") or ""
        document_id = f"{issue_id}:pull_patch:{number}:{filename}"
        if not f.get("patch"):
            reason = "patch_unavailable"
        elif SKIPPED_PATCH_FILES.search(filename):
            reason = "generated_or_binary_file"
        elif patch_files >= MAX_PATCH_FILES_PER_PR:
            reason = "over_limit"
        else:
            reason = None
        if reason:
            rejected.append({"document_id": document_id, "issue_id": issue_id, "document_type": "pull_patch",
                             "reject_reason": reason})
            continue
        patch, patch_counts = redact(f["patch"])
        truncated = len(patch) > MAX_PATCH_CHARS_PER_FILE
        patch = patch[:MAX_PATCH_CHARS_PER_FILE]
        patch_files += 1
        documents.append(_document(
            issue_item, document_id, "pull_patch", f"PR #{number} 변경: {filename}",
            f"파일: {filename} ({PurePosixPath(filename).suffix or 'no-ext'}, {f.get('status')})\n{patch}",
            author_role="contributor", source_url=f"{pr.get('html_url')}/files", source_type="github_pull_patch",
            source_updated_at=pr.get("updated_at"), selection_reason="병합 PR 변경 내용",
            truncated=truncated, redaction_counts=patch_counts,
        ))
    return documents, rejected
