"""RAG 2 issue_documents 문서 유형별 청킹과 레코드 구성 (기획서 6.2절, 6.3절)."""
from __future__ import annotations

import hashlib

from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

from src.indexing.ids import chunk_id

COLLECTION = "issue_documents"
DEFAULT_CHUNKING = {"chunk_size": 900, "chunk_overlap": 150}


def make_splitter(document_type: str, chunking: dict) -> RecursiveCharacterTextSplitter:
    settings = chunking.get(document_type, DEFAULT_CHUNKING)
    size, overlap = settings["chunk_size"], settings["chunk_overlap"]
    if document_type == "pull_patch":
        # patch는 hunk(@@) 단위를 우선해 자른다
        return RecursiveCharacterTextSplitter(separators=["\n@@", "\n", ""], chunk_size=size, chunk_overlap=overlap)
    # Markdown 제목·코드 블록 경계를 우선해 자른다
    return RecursiveCharacterTextSplitter.from_language(Language.MARKDOWN, chunk_size=size, chunk_overlap=overlap)


def chunk_header(document: dict) -> str:
    """임베딩과 BM25 입력에 저장소, 이슈 제목, 컴포넌트, 문서 유형을 붙인다 (기획서 6.6절)."""
    number = document["issue_id"].rsplit("#", 1)[1]
    return (f"[{document['repo']}#{number}] {document['issue_title']}\n"
            f"컴포넌트: {document['component']} | 문서: {document['document_type']} | {document['title']}")


def document_records(documents: list[dict], chunking: dict) -> list[dict]:
    splitters: dict[str, RecursiveCharacterTextSplitter] = {}
    records = []
    for document in documents:
        document_type = document["document_type"]
        splitter = splitters.setdefault(document_type, make_splitter(document_type, chunking))
        header = chunk_header(document)
        pieces = splitter.split_text(document["body"]) or [""]
        for index, piece in enumerate(pieces):
            records.append({
                "id": chunk_id(document["document_id"], index),
                "text": f"{header}\n\n{piece}".strip(),
                "metadata": {
                    "issue_id": document["issue_id"],
                    "repo": document["repo"],
                    "issue_number": int(document["issue_id"].rsplit("#", 1)[1]),
                    "document_id": document["document_id"],
                    "document_type": document_type,
                    "document_title": document["title"],
                    "component": document["component"],
                    "author_role": document.get("author_role"),
                    "source_url": document.get("source_url"),
                    "source_type": document.get("source_type"),
                    "source_updated_at": document.get("source_updated_at"),
                    "selection_reason": document.get("selection_reason"),
                    "chunk_index": index,
                    "chunk_count": len(pieces),
                    "content_hash": hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                    # 질문 시점 외부 수집 문서만 표시한다. 사전 수집 문서에 키를 추가하면 레코드 해시가 바뀌어
                    # 전체가 다시 임베딩되므로 기존 문서의 메타데이터는 그대로 둔다
                    **({"collection_source": document["collection_source"]} if document.get("collection_source") else {}),
                },
            })
    return records
