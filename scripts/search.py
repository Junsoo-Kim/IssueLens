"""질문으로 2단계 Hybrid 검색을 실행하고 라우팅, 상위 이슈, 근거를 출력한다.

    python scripts/search.py "Keycloak 26 업그레이드 후 TokenVerificationException"
    python scripts/search.py "sync job fails with timeout" --repo airbytehq/airbyte
    python scripts/search.py "같은 문제가 다른 버전에도 있나요?" --issue keycloak/keycloak#12345
    python scripts/search.py "..." --mode sparse      # 비교용: BM25만 (임베딩 API 호출 없음)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.config import load_repositories, load_yaml  # noqa: E402
from src.indexing.embeddings import LazyOpenAIEmbedder  # noqa: E402
from src.retrieval.hybrid import MODES, HybridRetriever, RetrievalResult  # noqa: E402
from src.retrieval.repository_router import issue_id_of_record  # noqa: E402

SNIPPET_CHARS = 220


def snippet(text: str) -> str:
    body = text.split("\n\n", 1)[-1]  # 청크 앞의 저장소·제목 헤더는 뺀다
    body = " ".join(body.split())
    return body[:SNIPPET_CHARS] + ("…" if len(body) > SNIPPET_CHARS else "")


def format_result(result: RetrievalResult, issue_titles: dict[str, str]) -> str:
    a, r = result.analysis, result.route
    lines = [
        f"질의 유형: {a.query_type} / 정확 토큰: {a.exact_tokens or '-'} / 명시 저장소: {a.explicit_repos or '-'}",
        f"라우팅: {r.repos} (신뢰도 {r.confidence}) — {r.reason}",
        f"검색 방식: {result.mode}" + (f" / RRF 가중치 {result.trace['weights']}" if result.trace.get("weights") else ""),
        "",
        "== 상위 이슈 (RRF 순위, 괄호는 sparse·dense 순위)",
    ]
    for rank, hit in enumerate(result.issues[:8], start=1):
        issue_id = issue_id_of_record(hit.id)
        ranks = ", ".join(f"{k} {v}" for k, v in sorted(hit.ranks.items()))
        lines.append(f"  {rank}. {issue_id} ({ranks}) {issue_titles.get(issue_id, '')}")
    lines += ["", "== 근거"]
    for e in result.evidence:
        tag = " [선택 이슈]" if e.forced else ""
        role = f", {e.author_role}" if e.author_role else ""
        lines.append(f"[근거 {e.number}] {e.issue_id} · {e.document_type}{role}{tag}")
        lines.append(f"    {e.source_url}")
        lines.append(f"    {snippet(e.text)}")
    s = result.assessment
    lines += ["", f"근거 판정: {'부족' if s['insufficient'] else '충분'} — 근거 {s['evidence_count']}건, "
                  f"해결 근거 {s['resolution_evidence_count']}건, 이슈 {s['issues']}개"
                  + (f" ({'; '.join(s['reasons'])})" if s["reasons"] else "")]
    return "\n".join(lines)


def main(argv: list[str] | None = None, embed_query=None) -> int:
    parser = argparse.ArgumentParser(description="2단계 Hybrid 검색")
    parser.add_argument("question")
    parser.add_argument("--repo", help="저장소 고정 (owner/repo)")
    parser.add_argument("--issue", help="선택 이슈 (owner/repo#번호)")
    parser.add_argument("--mode", choices=MODES, default="hybrid")
    parser.add_argument("--json", action="store_true", help="결과 전체를 JSON으로 출력")
    parser.add_argument("--chroma-dir", type=Path, default=ROOT / "chroma_db")
    parser.add_argument("--sparse-dir", type=Path, default=ROOT / "sparse_index")
    parser.add_argument("--retrieval-config", type=Path, default=ROOT / "configs/retrieval.yaml")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/repositories.yaml")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    load_dotenv(ROOT / ".env")

    retrieval_config = load_yaml(args.retrieval_config)
    if embed_query is None:
        embedder = LazyOpenAIEmbedder(retrieval_config.get("embedding", {}).get("model", "text-embedding-3-small"))
        embed_query = embedder.embed_query
    retriever = HybridRetriever(args.chroma_dir, args.sparse_dir, retrieval_config,
                                load_repositories(args.config), embed_query)
    if retriever.issue_collection.count() == 0:
        print("색인이 비어 있습니다. 먼저 scripts/build_indexes.py를 실행하세요.", file=sys.stderr)
        return 2

    result = retriever.retrieve(args.question, repo=args.repo, selected_issue_id=args.issue, mode=args.mode)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0
    titles = {}
    ids = [h.id for h in result.issues[:8]]
    if ids:
        got = retriever.issue_collection.get(ids=ids, include=["metadatas"])
        titles = {m["issue_id"]: m.get("title", "") for m in got["metadatas"]}
    print(format_result(result, titles))
    return 0


if __name__ == "__main__":
    sys.exit(main())
