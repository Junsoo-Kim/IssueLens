"""태깅 결과, 상세 수집 결과, 해결 근거를 결합해 RAG 1(issue_metadata) 항목을 만든다 (기획서 5.10절, 6.1절)."""
from __future__ import annotations

import hashlib
import re

from src.preprocessing.normalizer import FENCE, normalize_markdown
from src.preprocessing.secret_redactor import redact
from src.preprocessing.tagger import parse_sections

SYMPTOM_SECTION = re.compile(r"describe|bug|problem|issue|what happened|current behavio|summary|description", re.I)
SYMPTOM_CHARS = 500
EVIDENCE_LABELS = {
    "merged_pr": "병합 PR",
    "closed_by_commit": "커밋 종료",
    "maintainer_detail": "유지보수자 상세 댓글",
    "resolution_comment": "해결 신호 댓글",
}


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def symptom_summary(body: str, limit: int = SYMPTOM_CHARS) -> str:
    """LLM 없이 증상 설명 항목(없으면 본문 앞부분)에서 코드 블록을 뺀 텍스트를 잘라 쓴다."""
    sections = parse_sections(body)
    text = next((t for name, t in sections.filled.items() if t and SYMPTOM_SECTION.search(name)), "") or body
    text = FENCE.sub(" ", text)
    text = re.sub(r"^#{1,4}\s.*$", " ", text, flags=re.M)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def build_issue_item(issue: dict, tag: dict, selection: dict, summary: dict) -> dict:
    """issue: 원본 이슈 상세, tag: tagged_issues 행, selection: final_selection 행, summary: deep_issues 행."""
    repo, number = selection["repo"], selection["number"]
    body, redactions = redact(issue.get("body") or "")
    body, _ = normalize_markdown(body)
    title, title_redactions = redact(issue.get("title") or "")
    labels = [label["name"] if isinstance(label, dict) else label for label in issue.get("labels", [])]
    platforms = tag.get("languages", []) + tag.get("dependencies", [])
    evidence = [
        f"{label} {', '.join(f'#{n}' for n in selection.get('merged_prs', []))}".strip() if flag == "merged_pr" else label
        for flag, label in EVIDENCE_LABELS.items()
        if selection.get(flag)
    ]

    index_text = "\n".join([
        f"저장소: {repo}",
        f"이슈 번호: {number}",
        f"제목: {title}",
        f"상태: {issue.get('state')} ({issue.get('state_reason')})",
        f"라벨: {', '.join(labels)}",
        f"유형: {tag.get('issue_type')}",
        f"컴포넌트: {', '.join(tag.get('components') or [tag.get('component', 'general')])}",
        f"범주: {', '.join(tag.get('categories', []))}",
        f"오류 유형: {', '.join(tag.get('error_types', []))}",
        f"버전: {', '.join(tag.get('versions', []))}",
        f"실행 환경: {', '.join(platforms)}",
        f"증상 요약: {symptom_summary(body)}",
        f"해결 근거: {', '.join(evidence) or '없음'} (증거 수준 {selection.get('evidence_level')})",
    ])
    merged_counts = {k: redactions.get(k, 0) + title_redactions.get(k, 0) for k in {*redactions, *title_redactions}}
    return {
        "issue_id": selection["issue_id"],
        "repo": repo,
        "number": number,
        "title": title,
        "labels": labels,
        "issue_type": tag.get("issue_type"),
        "component": tag.get("component", "general"),
        "components": tag.get("components", []),
        "category": tag.get("category"),
        "categories": tag.get("categories", []),
        "error_types": tag.get("error_types", []),
        "versions": tag.get("versions", []),
        "platforms": platforms,
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "created_at": issue.get("created_at"),
        "closed_at": issue.get("closed_at"),
        "source_url": issue.get("html_url"),
        "source_updated_at": issue.get("updated_at"),
        "evidence_level": selection.get("evidence_level"),
        "merged_prs": selection.get("merged_prs", []),
        "official_doc_urls": (summary.get("reference_urls") or {}).get("official_docs", []),
        "split": selection["final_split"],
        "redaction_applied": bool(merged_counts),
        "redaction_counts": merged_counts,
        "index_text": index_text,
        "content_hash": content_hash(index_text),
    }
