"""RAG 파이프라인 조립: 검색기, LLM, 질문 시점 수집기, LangGraph를 설정에서 만든다. CLI와 Streamlit이 함께 쓴다."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from src.collectors.cache import RawStore, ResponseCache, read_jsonl
from src.collectors.github_client import DEFAULT_API_VERSION, DEFAULT_USER_AGENT, GitHubClient, HttpSettings
from src.collectors.query_time import QueryTimeCollector
from src.config import load_repositories, load_yaml
from src.graph.nodes import PipelineSettings, RAGPipeline
from src.graph.workflow import build_graph
from src.indexing import document_indexer
from src.indexing.bm25_indexer import upsert_bm25
from src.indexing.chroma_store import upsert_records
from src.indexing.embeddings import LazyOpenAIEmbedder
from src.prompts.llm import OpenAIChat
from src.retrieval.hybrid import HybridRetriever


@dataclass
class Assistant:
    pipeline: RAGPipeline
    graph: object
    retriever: HybridRetriever
    app_config: dict

    def ask(self, question: str, selected_issue_id: str | None = None, repo: str | None = None,
            allow_general_answer: bool | None = None) -> dict:
        if allow_general_answer is None:
            allow_general_answer = self.app_config.get("rag", {}).get("allow_general_answer", False)
        return self.graph.invoke({"question": question, "selected_issue_id": selected_issue_id, "repo": repo,
                                  "allow_general_answer": allow_general_answer})


def _selection_ids(manifest_dir: Path) -> tuple[set[str], set[str]]:
    """(색인 이슈, 평가 이슈). 질문 시점 수집에서 평가 이슈를 제외하고 이미 색인된 이슈는 다시 수집하지 않는다."""
    path = manifest_dir / "final_selection.jsonl"
    rows = read_jsonl(path) if path.exists() else []
    return ({r["issue_id"] for r in rows if r["final_split"] == "index"},
            {r["issue_id"] for r in rows if r["final_split"] == "evaluation"})


def build_assistant(root: Path, llm=None, embedder=None, github_client: GitHubClient | None = None,
                    enable_collection: bool | None = None) -> Assistant:
    app_config = load_yaml(root / "configs/app.yaml")
    retrieval_config = load_yaml(root / "configs/retrieval.yaml")
    repo_config_path = root / "configs/repositories.yaml"
    repositories = load_repositories(repo_config_path)
    paths = {name: Path(value) if Path(value).is_absolute() else root / value
             for name, value in app_config.get("paths", {}).items()}

    embedding_model = retrieval_config.get("embedding", {}).get("model", "text-embedding-3-small")
    embedder = embedder or LazyOpenAIEmbedder(embedding_model)
    embed_query = getattr(embedder, "embed_query", None) or (lambda text: embedder([text])[0])
    retriever = HybridRetriever(paths["chroma_dir"], paths["sparse_index_dir"], retrieval_config, repositories, embed_query)
    if llm is None and os.getenv("OPENAI_API_KEY"):
        llm = OpenAIChat(app_config.get("llm", {}).get("model", "gpt-5-mini"))

    collection_config = app_config.get("query_time_collection", {})
    if enable_collection is None:
        enable_collection = collection_config.get("enabled", True)
    collector = None
    if enable_collection:
        documents_name = retrieval_config.get("collections", {}).get("issue_documents", document_indexer.COLLECTION)
        chunking = retrieval_config.get("chunking", {})

        def index_documents(documents: list[dict]) -> int:
            records = document_indexer.document_records(documents, chunking)
            upsert_records(retriever.document_collection, records, embedder)
            upsert_bm25(records, paths["sparse_index_dir"] / documents_name)
            retriever.refresh_sparse()
            return len(records)

        client = github_client or GitHubClient(
            token=os.getenv("GITHUB_TOKEN") or None,
            api_version=os.getenv("GITHUB_API_VERSION") or DEFAULT_API_VERSION,
            user_agent=os.getenv("GITHUB_USER_AGENT") or DEFAULT_USER_AGENT,
            settings=HttpSettings.from_dict(app_config.get("http")),
            cache=ResponseCache(paths["cache_dir"]),
        )
        indexed_ids, evaluation_ids = _selection_ids(paths["manifest_dir"])
        collector = QueryTimeCollector(
            client, RawStore(paths["raw_dir"]), paths["cache_dir"], paths["processed_dir"],
            indexed_ids=indexed_ids, excluded_ids=evaluation_ids, index_documents=index_documents,
            repo_configs={r["repo"]: r for r in repositories}, tagging=load_yaml(repo_config_path).get("tagging", {}),
            results_per_query=collection_config.get("results_per_query", 10),
            max_selected=collection_config.get("max_selected", 3),
            max_linked_prs=app_config.get("collection", {}).get("max_linked_prs", 5),
        )

    rag = app_config.get("rag", {})
    settings = PipelineSettings(max_attempts=rag.get("max_attempts", 3), evidence_chars=rag.get("evidence_chars", 1200),
                                citation_retries=rag.get("citation_retries", 1))
    pipeline = RAGPipeline(retriever, llm, repositories, collector, settings)
    return Assistant(pipeline, build_graph(pipeline), retriever, app_config)
