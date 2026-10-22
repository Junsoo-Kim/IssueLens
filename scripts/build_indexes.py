"""정제, RAG 1/RAG 2 문서 생성, 청킹, Chroma(Dense)와 BM25(Sparse) 색인, 정합성 검증.

    python scripts/build_indexes.py --skip-embed   # 문서·청크·BM25만 만들고 건수 확인 (API 호출 없음)
    python scripts/build_indexes.py                # Chroma 임베딩까지 (바뀐 레코드만 임베딩)
    python scripts/build_indexes.py --reset        # 컬렉션과 BM25를 지우고 처음부터

입력: data/manifests/final_selection.jsonl(split=index), tagged_issues.jsonl, deep_issues.jsonl, 상세 수집 원본
출력
    data/processed/issue_items.jsonl, issue_documents.jsonl, rejected_documents.jsonl, issue_chunks.jsonl (원문 포함, 로컬)
    chroma_db/, sparse_index/ (로컬)
    data/manifests/index_manifest.json (건수와 설정, 원문 없음)
"""
from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.collectors.cache import RawStore, read_jsonl, utc_now, write_json_atomic, write_jsonl_atomic  # noqa: E402
from src.config import load_yaml  # noqa: E402
from src.indexing import document_indexer, metadata_indexer  # noqa: E402
from src.indexing.bm25_indexer import TOKENIZER_VERSION, build_bm25_indexes, load_bm25_ids  # noqa: E402
from src.indexing.chroma_store import collection_ids, get_collection, open_client, sync_collection  # noqa: E402
from src.indexing.embeddings import LazyOpenAIEmbedder  # noqa: E402
from src.collectors.query_time import QUERY_TIME_DOCUMENTS  # noqa: E402
from src.preprocessing.build import build_processed  # noqa: E402
from src.preprocessing.tagger import primary_category  # noqa: E402

INDEX_VERSION = "1"


def reset_indexes(chroma_dir: Path, sparse_dir: Path) -> None:
    client = open_client(chroma_dir)
    existing = {c.name if hasattr(c, "name") else c for c in client.list_collections()}
    for name in (metadata_indexer.COLLECTION, document_indexer.COLLECTION):
        if name in existing:
            client.delete_collection(name)
    shutil.rmtree(sparse_dir, ignore_errors=True)


def format_report(report: dict) -> str:
    c = report["counts"]
    v = report["verification"]
    lines = [
        f"RAG 1 이슈: 로드 {c['issue_items']} / Chroma {v.get('issue_metadata_chroma', '-')} / "
        f"BM25 {v['issue_metadata_bm25']} / ID 일치 {v.get('issue_metadata_ids_match', '-')}",
        f"RAG 2 문서: {c['documents']} {c['documents_by_type']} / 탈락 {c['rejected_documents']} {c['rejected_by_reason']}",
        f"RAG 2 청크: {c['chunks']} {c['chunks_by_type']} / Chroma {v.get('issue_documents_chroma', '-')} / "
        f"BM25 {v['issue_documents_bm25']} / ID 일치 {v.get('issue_documents_ids_match', '-')}",
        f"모든 색인 이슈에 RAG 2 문서 존재: {v['every_issue_has_documents']} / 원본 누락 이슈 {len(report['missing_issues'])}",
        f"마스킹 적용 문서: {c['redacted_documents']} / 잘린 문서: {c['truncated_documents']}",
    ]
    for name, sync in report.get("sync", {}).items():
        lines.append(f"임베딩 {name}: 신규 {sync['added']} / 갱신 {sync['updated']} / 유지 {sync['unchanged']} / "
                     f"삭제 {sync['deleted']} (차원 {sync['dimensions'] or '-'})")
    if report.get("skip_embed"):
        lines.append("임베딩 건너뜀 (--skip-embed)")
    return "\n".join(lines)


def main(argv: list[str] | None = None, embedder=None) -> int:
    return build(argv, embedder)[0]


def build(argv: list[str] | None = None, embedder=None) -> tuple[int, dict | None]:
    """종료 코드와 리포트를 반환한다."""
    parser = argparse.ArgumentParser(description="정제와 RAG 1/RAG 2 색인")
    parser.add_argument("--selection", type=Path, default=ROOT / "data/manifests/final_selection.jsonl")
    parser.add_argument("--deep-selection", type=Path, default=ROOT / "data/manifests/deep_selection.jsonl")
    parser.add_argument("--tagged", type=Path, default=ROOT / "data/processed/tagged_issues.jsonl")
    parser.add_argument("--deep-issues", type=Path, default=ROOT / "data/processed/deep_issues.jsonl")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw/github")
    parser.add_argument("--processed-dir", type=Path, default=ROOT / "data/processed")
    parser.add_argument("--manifest-dir", type=Path, default=ROOT / "data/manifests")
    parser.add_argument("--chroma-dir", type=Path, default=ROOT / "chroma_db")
    parser.add_argument("--sparse-dir", type=Path, default=ROOT / "sparse_index")
    parser.add_argument("--retrieval-config", type=Path, default=ROOT / "configs/retrieval.yaml")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/repositories.yaml")
    parser.add_argument("--splits", nargs="+", default=["index"], help="색인할 final_split (평가용은 색인하지 않는다)")
    parser.add_argument("--skip-embed", action="store_true", help="Chroma 임베딩 없이 문서·청크·BM25만 만든다")
    parser.add_argument("--reset", action="store_true", help="기존 컬렉션과 BM25 인덱스를 지우고 다시 만든다")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(ROOT / ".env")

    for path in (args.selection, args.deep_selection, args.tagged, args.deep_issues):
        if not path.exists():
            print(f"입력 파일이 없습니다: {path}", file=sys.stderr)
            return 2, None
    if "evaluation" in args.splits:
        print("평가용 이슈는 색인하지 않습니다 (기획서 5.4절).", file=sys.stderr)
        return 2, None

    retrieval = load_yaml(args.retrieval_config)
    embedding = retrieval.get("embedding", {})
    chunking = retrieval.get("chunking", {})
    collections = retrieval.get("collections", {})

    selection = [r for r in read_jsonl(args.selection) if r["final_split"] in args.splits]
    # 범위 확정(부가 태그 전용 범주) 이전에 만든 태깅 결과에도 같은 대표 범주 규칙을 적용한다
    repo_config = load_yaml(args.config)
    tag_only = set(repo_config.get("tagging", {}).get("tag_only_categories", []))
    fallbacks = {r["repo"]: r.get("categories", []) for r in repo_config.get("repositories", [])}
    tags = {
        t["issue_id"]: {**t, "category": primary_category(
            t.get("categories", []), fallbacks.get(t["issue_id"].rsplit("#", 1)[0], []), tag_only)}
        for t in read_jsonl(args.tagged)
    }
    summaries = {s["issue_id"]: s for s in read_jsonl(args.deep_issues)}
    items, documents, rejected, missing, maintainers = build_processed(
        selection, tags, summaries, read_jsonl(args.deep_selection), RawStore(args.raw_dir))

    # 질문 시점에 수집해 색인한 문서도 함께 넣어, 재색인할 때 사라진 레코드로 지워지지 않게 한다
    query_time_path = args.processed_dir / QUERY_TIME_DOCUMENTS
    query_time_documents = read_jsonl(query_time_path) if query_time_path.exists() else []
    issue_records = metadata_indexer.issue_records(items)
    chunk_records = document_indexer.document_records(documents + query_time_documents, chunking)
    write_jsonl_atomic(args.processed_dir / "issue_items.jsonl", items)
    write_jsonl_atomic(args.processed_dir / "issue_documents.jsonl", documents)
    write_jsonl_atomic(args.processed_dir / "rejected_documents.jsonl", rejected)
    write_jsonl_atomic(args.processed_dir / "issue_chunks.jsonl", chunk_records)

    if args.reset:
        reset_indexes(args.chroma_dir, args.sparse_dir)

    metadata_name = collections.get("issue_metadata", metadata_indexer.COLLECTION)
    documents_name = collections.get("issue_documents", document_indexer.COLLECTION)
    bm25_counts = {
        metadata_name: build_bm25_indexes(issue_records, args.sparse_dir / metadata_name),
        documents_name: build_bm25_indexes(chunk_records, args.sparse_dir / documents_name),
    }
    verification = {
        "issue_metadata_bm25": sum(bm25_counts[metadata_name].values()),
        "issue_documents_bm25": sum(bm25_counts[documents_name].values()),
        "every_issue_has_documents": {i["issue_id"] for i in items} <= {d["issue_id"] for d in documents},
    }

    sync = {}
    if not args.skip_embed:
        embed = embedder or LazyOpenAIEmbedder(embedding.get("model", "text-embedding-3-small"))
        client = open_client(args.chroma_dir)
        distance = embedding.get("distance", "cosine")
        for name, records, key in ((metadata_name, issue_records, "issue_metadata"),
                                   (documents_name, chunk_records, "issue_documents")):
            collection = get_collection(client, name, distance)
            sync[name] = sync_collection(collection, records, embed).as_dict()
            chroma_ids = collection_ids(collection)
            expected = {r["id"] for r in records}
            verification[f"{key}_chroma"] = len(chroma_ids)
            verification[f"{key}_ids_match"] = chroma_ids == expected == load_bm25_ids(args.sparse_dir / name)

    report = {
        "generated_at": utc_now(),
        "index_version": INDEX_VERSION,
        "tokenizer_version": TOKENIZER_VERSION,
        "splits": args.splits,
        "skip_embed": args.skip_embed,
        "embedding": {**embedding, "dimensions": next((s["dimensions"] for s in sync.values() if s["dimensions"]), None)},
        "chunking": chunking,
        "collections": {"issue_metadata": metadata_name, "issue_documents": documents_name},
        "counts": {
            "issue_items": len(items),
            "issue_items_by_repo": dict(Counter(i["repo"] for i in items)),
            "documents": len(documents),
            "documents_by_type": dict(Counter(d["document_type"] for d in documents)),
            "query_time_documents": len(query_time_documents),
            "rejected_documents": len(rejected),
            "rejected_by_reason": dict(Counter(r["reject_reason"].split("(")[0] for r in rejected)),
            "chunks": len(chunk_records),
            "chunks_by_type": dict(Counter(r["metadata"]["document_type"] for r in chunk_records)),
            "redacted_documents": sum(bool(d.get("redaction_counts")) for d in documents),
            "truncated_documents": sum(bool(d.get("truncated")) for d in documents),
            "maintainers_by_repo": maintainers,
        },
        "bm25_by_repo": bm25_counts,
        "sync": sync,
        "verification": verification,
        "missing_issues": missing,
    }
    write_json_atomic(args.manifest_dir / "index_manifest.json", report)
    print(format_report(report))
    ok = verification["every_issue_has_documents"] and all(
        verification.get(f"{k}_ids_match", True) for k in ("issue_metadata", "issue_documents"))
    return (0 if ok else 1), report


if __name__ == "__main__":
    sys.exit(main())
