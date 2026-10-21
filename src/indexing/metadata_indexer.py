"""RAG 1 issue_metadata 레코드 구성. 이슈 1건이 레코드 1개다 (기획서 6.1절)."""
from __future__ import annotations

from src.indexing.ids import issue_record_id

COLLECTION = "issue_metadata"


def issue_records(items: list[dict]) -> list[dict]:
    records = []
    for item in items:
        records.append({
            "id": issue_record_id(item["issue_id"]),
            "text": item["index_text"],
            "metadata": {
                "issue_id": item["issue_id"],
                "repo": item["repo"],
                "issue_number": item["number"],
                "title": item["title"],
                "state": item["state"],
                "state_reason": item["state_reason"],
                "issue_type": item["issue_type"],
                "labels_csv": item["labels"],
                "component": item["component"],
                "category": item["category"],
                "categories_csv": item["categories"],
                "error_types_csv": item["error_types"],
                "versions_csv": item["versions"],
                "platforms_csv": item["platforms"],
                "evidence_level": item["evidence_level"],
                "created_at": item["created_at"],
                "closed_at": item["closed_at"],
                "source_url": item["source_url"],
                "content_hash": item["content_hash"],
            },
        })
    return records
