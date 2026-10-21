"""결정적 이슈 ID, 청크 ID, 레코드 해시 생성. 같은 입력이면 항상 같은 ID가 나와 upsert 시 중복이 생기지 않는다."""
from __future__ import annotations

import hashlib
import json


def issue_record_id(issue_id: str) -> str:
    """`owner/repo#123` -> `issue:owner:repo:123` (기획서 6.1절)."""
    repo, number = issue_id.rsplit("#", 1)
    owner, name = repo.split("/", 1)
    return f"issue:{owner}:{name}:{number}"


def chunk_id(document_id: str, chunk_index: int) -> str:
    """document_id에 이미 issue_id와 문서 유형이 들어 있어 기획서 6.2절 형식을 줄여 쓴다."""
    return f"doc:{document_id}:{chunk_index}"


def record_hash(text: str, metadata: dict) -> str:
    """텍스트나 메타데이터가 바뀌면 달라지는 해시. 바뀐 레코드만 다시 임베딩하는 데 쓴다."""
    payload = json.dumps({"text": text, "metadata": {k: v for k, v in metadata.items() if k != "record_hash"}},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
