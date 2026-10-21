"""2단계 Hybrid 검색 (기획서 9장).

1. 질의 분석과 저장소 라우팅
2. RAG 1 issue_metadata: BM25와 Dense를 각각 검색해 RRF로 결합, 상위 이슈 선택
3. RAG 2 issue_documents: 상위 이슈로 범위를 좁혀 BM25·Dense·RRF를 반복
4. 근거 선별: 선택 이슈의 메타데이터와 본문은 항상 포함, 중복 제거와 다양성 제한, 근거 부족 판정

mode="sparse" / "dense"는 비교 실험(ablation)용으로 한쪽 검색만 쓴다. Cross-Encoder 재정렬은 아직 쓰지 않는다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from src.indexing import document_indexer, metadata_indexer
from src.indexing.chroma_store import get_collection, open_client
from src.indexing.ids import issue_record_id
from src.retrieval.dense_retriever import DenseRetriever, in_filter
from src.retrieval.evidence_selector import Evidence, assess_evidence, select_evidence
from src.retrieval.query_analyzer import QueryAnalysis, analyze_query
from src.retrieval.repository_router import RouteResult, fixed_route, issue_id_of_record, route_by_hits
from src.retrieval.rrf_fusion import FusedHit, reciprocal_rank_fusion
from src.retrieval.sparse_retriever import SparseRetriever

MODES = ("hybrid", "sparse", "dense")
# 해결 근거 후보를 가져올 상위 이슈 수. 관련성이 낮은 이슈의 PR이 근거로 들어오지 않게 좁게 둔다
RESOLUTION_ISSUES = 3


@dataclass
class RetrievalResult:
    analysis: QueryAnalysis
    route: RouteResult
    mode: str
    issues: list[FusedHit]
    documents: list[FusedHit]
    evidence: list[Evidence]
    assessment: dict
    trace: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class HybridRetriever:
    def __init__(self, chroma_dir: Path, sparse_dir: Path, retrieval_config: dict, repositories: list[dict],
                 embed_query: Callable[[str], list[float]]):
        self.config = retrieval_config.get("retrieval", {})
        collections = retrieval_config.get("collections", {})
        self.repositories = repositories
        self.all_repos = [r["repo"] for r in repositories]
        metadata_name = collections.get("issue_metadata", metadata_indexer.COLLECTION)
        documents_name = collections.get("issue_documents", document_indexer.COLLECTION)
        client = open_client(chroma_dir)
        self.issue_collection = get_collection(client, metadata_name)
        self.document_collection = get_collection(client, documents_name)
        self._query_vectors: dict[str, list[float]] = {}
        cached = self._cached_embed(embed_query)
        self.issue_dense = DenseRetriever(self.issue_collection, cached)
        self.document_dense = DenseRetriever(self.document_collection, cached)
        self.issue_sparse = SparseRetriever(Path(sparse_dir) / metadata_name)
        self.document_sparse = SparseRetriever(Path(sparse_dir) / documents_name)

    def _cached_embed(self, embed_query):
        def embed(text: str) -> list[float]:
            if text not in self._query_vectors:
                self._query_vectors[text] = embed_query(text)
            return self._query_vectors[text]
        return embed

    def _weights(self, query_type: str) -> dict[str, float]:
        weights = self.config.get("fusion", {}).get("weights", {}).get(query_type, {"sparse": 0.5, "dense": 0.5})
        return {"sparse": weights.get("sparse", 0.5), "dense": weights.get("dense", 0.5)}

    def _fuse(self, sparse_ids: list[str], dense_ids: list[str], query_type: str, mode: str) -> list[FusedHit]:
        rankings = {}
        if mode in ("hybrid", "sparse"):
            rankings["sparse"] = sparse_ids
        if mode in ("hybrid", "dense"):
            rankings["dense"] = dense_ids
        weights = self._weights(query_type) if mode == "hybrid" else {"sparse": 1.0, "dense": 1.0}
        return reciprocal_rank_fusion(rankings, weights, self.config.get("fusion", {}).get("rrf_k", 60))

    def _search_issues(self, analysis: QueryAnalysis, repos: list[str], mode: str) -> tuple[list[FusedHit], dict]:
        cfg = self.config.get("issue", {})
        sparse = self.issue_sparse.search(analysis.sparse_query(), repos, cfg.get("sparse_top_k", 30)) if mode != "dense" else []
        dense = (self.issue_dense.search(analysis.question, cfg.get("dense_top_k", 30), in_filter("repo", repos))
                 if mode != "sparse" else [])
        fused = self._fuse([i for i, _ in sparse], [i for i, _, _ in dense], analysis.query_type, mode)
        return fused[: cfg.get("fused_top_k", 30)], {"sparse": [i for i, _ in sparse], "dense": [i for i, _, _ in dense]}

    def _route(self, analysis: QueryAnalysis, repo: str | None, selected_issue_id: str | None, mode: str) -> RouteResult:
        max_repos = self.config.get("route", {}).get("max_repositories", 2)
        if repo:
            return fixed_route([repo], "사용자가 선택한 저장소", max_repos)
        if selected_issue_id:
            return fixed_route([selected_issue_id.rsplit("#", 1)[0]], "선택 이슈의 저장소", max_repos)
        if analysis.explicit_repos:
            return fixed_route(analysis.explicit_repos, "질문에 명시된 저장소", max_repos)
        hits, _ = self._search_issues(analysis, self.all_repos, mode)
        return route_by_hits(hits, max_repos)

    def _fetch(self, collection, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        got = collection.get(ids=ids, include=["documents", "metadatas"])
        return {i: {"id": i, "text": t, "metadata": m} for i, t, m in zip(got["ids"], got["documents"], got["metadatas"])}

    def _resolution_candidates(self, issue_ids: list[str]) -> list[dict]:
        """상위 이슈의 병합 PR 설명과 유지보수자 댓글 첫 청크. 이슈 순위, PR, 해결 신호 댓글, 일반 유지보수자 댓글 순."""
        if not issue_ids:
            return []
        got = self.document_collection.get(
            where={"$and": [in_filter("issue_id", issue_ids), {"chunk_index": 0},
                            {"$or": [{"document_type": "pull_request"}, {"author_role": "maintainer"}]}]},
            include=["documents", "metadatas"])
        order = {issue_id: i for i, issue_id in enumerate(issue_ids)}

        def priority(metadata: dict) -> int:
            if metadata.get("document_type") == "pull_request":
                return 0
            return 1 if "resolution_signal" in (metadata.get("selection_reason") or "") else 2

        candidates = [{"id": i, "text": t, "metadata": m} for i, t, m in zip(got["ids"], got["documents"], got["metadatas"])]
        return sorted(candidates, key=lambda c: (order.get(c["metadata"]["issue_id"], len(order)),
                                                 priority(c["metadata"]), c["id"]))

    def _forced_evidence(self, selected_issue_id: str) -> list[dict]:
        """선택 이슈의 RAG 1 메타데이터와 이슈 본문 청크는 점수와 무관하게 근거에 넣는다."""
        record = self._fetch(self.issue_collection, [issue_record_id(selected_issue_id)])
        forced = []
        for hit in record.values():
            forced.append({**hit, "metadata": {**hit["metadata"], "document_type": "issue_metadata",
                                               "document_title": hit["metadata"].get("title", "")}})
        body = self.document_collection.get(
            where={"$and": [{"issue_id": selected_issue_id}, {"document_type": "issue_body"}]},
            include=["documents", "metadatas"])
        chunks = sorted(zip(body["ids"], body["documents"], body["metadatas"]), key=lambda x: x[2].get("chunk_index", 0))
        forced.extend({"id": i, "text": t, "metadata": m} for i, t, m in chunks[:1])
        return forced

    def retrieve(self, question: str, repo: str | None = None, selected_issue_id: str | None = None,
                 mode: str = "hybrid") -> RetrievalResult:
        if mode not in MODES:
            raise ValueError(f"mode는 {MODES} 중 하나여야 합니다: {mode}")
        analysis = analyze_query(question, self.repositories, selected_issue_id)
        route = self._route(analysis, repo, selected_issue_id, mode)
        issues, issue_rankings = self._search_issues(analysis, route.repos, mode) if route.repos else ([], {})

        doc_cfg = self.config.get("document", {})
        top_issue_ids = [issue_id_of_record(h.id) for h in issues[: self.config.get("issue", {}).get("rerank_top_k", 8)]]
        if selected_issue_id and selected_issue_id not in top_issue_ids:
            top_issue_ids.insert(0, selected_issue_id)

        documents, document_rankings = [], {}
        if top_issue_ids:
            prefixes = tuple(f"doc:{issue_id}:" for issue_id in top_issue_ids)
            doc_repos = sorted({i.rsplit("#", 1)[0] for i in top_issue_ids})
            sparse = (self.document_sparse.search(analysis.sparse_query(), doc_repos, doc_cfg.get("sparse_top_k", 40),
                                                  allow=lambda i: i.startswith(prefixes)) if mode != "dense" else [])
            dense = (self.document_dense.search(analysis.question, doc_cfg.get("dense_top_k", 40),
                                                in_filter("issue_id", top_issue_ids)) if mode != "sparse" else [])
            document_rankings = {"sparse": [i for i, _ in sparse], "dense": [i for i, _, _ in dense]}
            documents = self._fuse(document_rankings["sparse"], document_rankings["dense"], analysis.query_type,
                                   mode)[: doc_cfg.get("fused_top_k", 30)]

        fetched = self._fetch(self.document_collection, [h.id for h in documents])
        hits = [{**fetched[h.id], "score": h.score, "ranks": h.ranks} for h in documents if h.id in fetched]
        diversity = self.config.get("diversity", {})
        evidence = select_evidence(
            hits,
            limit=doc_cfg.get("final_evidence_limit", 8),
            max_per_issue=diversity.get("max_evidence_per_issue", 3),
            max_per_type=diversity.get("max_evidence_per_document_type", 4),
            forced=self._forced_evidence(selected_issue_id) if selected_issue_id else None,
            resolution_candidates=self._resolution_candidates(top_issue_ids[:RESOLUTION_ISSUES]),
        )
        return RetrievalResult(
            analysis=analysis,
            route=route,
            mode=mode,
            issues=issues,
            documents=documents,
            evidence=evidence,
            assessment=assess_evidence(evidence, analysis.exact_tokens),
            trace={
                "issue_rankings": issue_rankings,
                "document_rankings": document_rankings,
                "top_issue_ids": top_issue_ids,
                "weights": self._weights(analysis.query_type) if mode == "hybrid" else None,
            },
        )
