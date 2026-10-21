"""검색 테스트: 질의 분석, RRF, 라우팅, 근거 선별, BM25 필터, 2단계 Hybrid 검색, 검색 CLI."""
from __future__ import annotations

import pytest

from src.collectors.cache import RawStore, write_jsonl_atomic
from src.config import PROJECT_ROOT, load_repositories, load_yaml
from src.indexing.bm25_indexer import build_bm25_indexes
from src.retrieval.evidence_selector import assess_evidence, select_evidence
from src.retrieval.query_analyzer import analyze_query
from src.retrieval.repository_router import issue_id_of_record, route_by_hits
from src.retrieval.rrf_fusion import FusedHit, reciprocal_rank_fusion
from src.retrieval.sparse_retriever import SparseRetriever
from tests.test_indexing import FakeEmbedder

REPOS = [{"repo": "keycloak/keycloak"}, {"repo": "airbytehq/airbyte"}, {"repo": "open-metadata/OpenMetadata"}]


# ---------------------------------------------------------------- 질의 분석


@pytest.mark.parametrize("question,query_type", [
    ("Keycloak 26.0.1 업그레이드 후 TokenVerificationException 발생", "error_log"),
    ("2026-01-01 ERROR [worker] sync aborted", "error_log"),
    ("open metadata 종료된 버그 찾아줘", "condition_search"),
    ("동기화 작업이 반복 실패할 때 원인은?", "natural_language"),
])
def test_query_type(question, query_type):
    assert analyze_query(question, REPOS).query_type == query_type


def test_query_extracts_exact_tokens_and_repos():
    analysis = analyze_query("Keycloak KC_DB_POOL_MAX_SIZE 401 org.keycloak.protocol.oidc 26.0.1 TokenVerificationException",
                             REPOS)
    assert analysis.explicit_repos == ["keycloak/keycloak"]
    for token in ["TokenVerificationException", "26.0.1", "KC_DB_POOL_MAX_SIZE", "org.keycloak.protocol.oidc", "401"]:
        assert token in analysis.exact_tokens
    assert "TokenVerificationException" in analysis.sparse_query()
    assert analyze_query("x", REPOS, selected_issue_id="keycloak/keycloak#1").query_type == "selected_issue"
    assert analyze_query("OpenMetadata ingestion fails", REPOS).explicit_repos == ["open-metadata/OpenMetadata"]


# ---------------------------------------------------------------- RRF, 라우팅


def test_rrf_weights_and_ranks():
    hits = reciprocal_rank_fusion({"sparse": ["a", "b"], "dense": ["b", "c"]}, {"sparse": 0.6, "dense": 0.4}, k=60)
    assert [h.id for h in hits] == ["b", "a", "c"]
    assert hits[0].score == pytest.approx(0.6 / 62 + 0.4 / 61)
    assert hits[0].ranks == {"sparse": 2, "dense": 1}
    assert reciprocal_rank_fusion({"sparse": ["a", "a"]}, {"sparse": 1})[0].ranks == {"sparse": 1}


def test_route_by_hits_picks_one_or_two():
    k, a = "issue:keycloak:keycloak:", "issue:airbytehq:airbyte:"
    confident = [FusedHit(f"{k}{i}", 1.0) for i in range(8)] + [FusedHit(f"{a}1", 1.0)]
    assert route_by_hits(confident).repos == ["keycloak/keycloak"]
    mixed = [FusedHit(f"{k}{i}", 1.0) for i in range(5)] + [FusedHit(f"{a}{i}", 1.0) for i in range(4)]
    assert route_by_hits(mixed).repos == ["keycloak/keycloak", "airbytehq/airbyte"]
    assert route_by_hits([]).repos == []
    assert issue_id_of_record("issue:keycloak:keycloak:12") == "keycloak/keycloak#12"


# ---------------------------------------------------------------- 근거 선별


def hit(id_, issue, doc_type, text=None, role="", score=1.0):
    return {"id": id_, "text": text or id_, "score": score,
            "metadata": {"issue_id": issue, "document_type": doc_type, "content_hash": text or id_,
                         "source_url": f"u/{id_}", "author_role": role}}


def test_select_evidence_dedupes_limits_and_numbers():
    hits = [
        hit("a1", "i1", "comment", role="maintainer"), hit("a2", "i1", "comment"), hit("a3", "i1", "comment"),
        hit("a4", "i1", "comment"), hit("dup", "i2", "comment", text="a1"),
        hit("b1", "i2", "pull_request"), hit("b2", "i2", "comment"), hit("c1", "i3", "comment"),
    ]
    forced = [hit("f1", "i9", "issue_metadata")]
    evidence = select_evidence(hits, limit=8, max_per_issue=3, max_per_type=4, forced=forced)
    assert [e.id for e in evidence] == ["f1", "a1", "a2", "a3", "b1", "b2"]
    assert [e.number for e in evidence] == list(range(1, 7))
    assert evidence[0].forced and not evidence[1].forced
    assessment = assess_evidence(evidence)
    assert not assessment["insufficient"] and assessment["resolution_evidence_count"] == 2


def test_assess_evidence_flags_insufficient():
    weak = select_evidence([hit("a", "i1", "comment")])
    assessment = assess_evidence(weak)
    assert assessment["insufficient"] and len(assessment["reasons"]) == 2


# ---------------------------------------------------------------- BM25 필터


def test_sparse_retriever_filters_and_merges_by_score(tmp_path):
    # BM25는 대부분 문서에 있는 단어의 IDF가 0 근처라, 질의어가 일부 문서에만 있는 현실적인 말뭉치로 둔다
    filler = [f"unrelated topic number {i} about dashboards" for i in range(8)]
    records = [{"id": f"doc:keycloak/keycloak#{i}:issue_body:0", "text": text, "metadata": {"repo": "keycloak/keycloak"}}
               for i, text in enumerate(["TokenVerificationException token invalid", "token refresh slow", *filler], start=1)]
    records += [{"id": f"doc:airbytehq/airbyte#{i}:issue_body:0", "text": text, "metadata": {"repo": "airbytehq/airbyte"}}
                for i, text in enumerate(["api token rotated", *filler], start=1)]
    build_bm25_indexes(records, tmp_path)
    retriever = SparseRetriever(tmp_path)
    hits = retriever.search("TokenVerificationException token", ["airbytehq/airbyte", "keycloak/keycloak"], top_k=3)
    assert hits[0][0] == "doc:keycloak/keycloak#1:issue_body:0"  # 저장소 나열 순서와 무관하게 강한 매치가 먼저
    assert [h[1] for h in hits] == sorted((h[1] for h in hits), reverse=True)
    only = retriever.search("token", ["keycloak/keycloak"], top_k=10, allow=lambda i: i.startswith("doc:keycloak/keycloak#2:"))
    assert [h[0] for h in only] == ["doc:keycloak/keycloak#2:issue_body:0"]
    assert retriever.search("token", ["no/such"], top_k=5) == []


# ---------------------------------------------------------------- 2단계 Hybrid 검색


ISSUES = [
    ("keycloak/keycloak", 1, "Admin console theme not refreshed", "Theme cache keeps old CSS after upgrade."),
    ("keycloak/keycloak", 2, "Login fails with TokenVerificationException",
     "After upgrade to 26.0.1 login fails.\n```\norg.keycloak.TokenVerificationException: invalid token\n```"),
    ("keycloak/keycloak", 3, "Realm export misses client scopes", "Exported realm JSON lacks client scopes."),
    ("airbytehq/airbyte", 4, "Postgres source sync times out", "Sync job fails with SocketTimeoutException on large tables."),
    ("airbytehq/airbyte", 5, "BigQuery destination quota error", "Load job fails with quotaExceeded for BigQuery."),
    ("airbytehq/airbyte", 6, "Connector builder UI freezes", "The builder page freezes when testing streams."),
]


@pytest.fixture
def index_dirs(tmp_path):
    from scripts import build_indexes

    raw = RawStore(tmp_path / "raw")
    selection, tagged, summaries = [], [], []
    for repo, number, title, body in ISSUES:
        issue_id = f"{repo}#{number}"
        selection.append({"issue_id": issue_id, "repo": repo, "number": number, "final_split": "index",
                          "evidence_level": "strong", "merged_pr": True, "merged_prs": [100 + number]})
        tagged.append({"issue_id": issue_id, "repo": repo, "issue_type": "bug", "component": "general", "components": [],
                       "categories": ["auth"], "error_types": [], "versions": [], "languages": [], "dependencies": []})
        summaries.append({"issue_id": issue_id, "repo": repo, "linked_pull_requests": [{"number": 100 + number, "merged": True}]})
        raw.save(repo, "issues", str(number), {
            "number": number, "title": title, "body": f"### Describe the bug\n{body}", "state": "closed",
            "state_reason": "completed", "labels": [], "html_url": f"https://github.com/{repo}/issues/{number}",
            "user": {"login": "reporter"}, "closed_by": {"login": "dev", "type": "User"}})
        raw.save(repo, "comments", str(number), [
            {"id": number * 10, "body": f"Fixed in the next release, root cause was {title.lower()}",
             "user": {"login": "dev", "type": "User"}, "author_association": "MEMBER"}])
        raw.save(repo, "pulls", str(100 + number), {
            "number": 100 + number, "title": f"Fix {title}", "body": f"Fixes #{number}",
            "merged_at": "2026-01-01T00:00:00Z", "html_url": f"https://github.com/{repo}/pull/{100 + number}"})
        raw.save(repo, "pull_files", str(100 + number), [])
    write_jsonl_atomic(tmp_path / "final.jsonl", selection)
    write_jsonl_atomic(tmp_path / "tagged.jsonl", tagged)
    write_jsonl_atomic(tmp_path / "deep.jsonl", summaries)
    code, _ = build_indexes.build([
        "--selection", str(tmp_path / "final.jsonl"), "--deep-selection", str(tmp_path / "final.jsonl"),
        "--tagged", str(tmp_path / "tagged.jsonl"), "--deep-issues", str(tmp_path / "deep.jsonl"),
        "--raw-dir", str(tmp_path / "raw"), "--processed-dir", str(tmp_path / "p"), "--manifest-dir", str(tmp_path / "m"),
        "--chroma-dir", str(tmp_path / "chroma"), "--sparse-dir", str(tmp_path / "sparse")], embedder=FakeEmbedder())
    assert code == 0
    return tmp_path


def make_retriever(index_dirs):
    from src.retrieval.hybrid import HybridRetriever

    embed = FakeEmbedder()
    return HybridRetriever(index_dirs / "chroma", index_dirs / "sparse",
                           load_yaml(PROJECT_ROOT / "configs/retrieval.yaml"),
                           load_repositories(PROJECT_ROOT / "configs/repositories.yaml"),
                           lambda text: embed([text])[0])


def test_sparse_retrieval_routes_and_finds_exact_error(index_dirs):
    retriever = make_retriever(index_dirs)
    result = retriever.retrieve("TokenVerificationException after upgrade", mode="sparse")
    assert result.route.repos[0] == "keycloak/keycloak"
    assert issue_id_of_record(result.issues[0].id) == "keycloak/keycloak#2"
    assert result.evidence and result.evidence[0].issue_id == "keycloak/keycloak#2"
    assert {e.document_type for e in result.evidence} >= {"issue_body", "pull_request"}
    assert not result.assessment["insufficient"]


def test_retrieval_respects_fixed_repo_and_selected_issue(index_dirs):
    retriever = make_retriever(index_dirs)
    fixed = retriever.retrieve("sync job timeout", repo="airbytehq/airbyte", mode="sparse")
    assert fixed.route.repos == ["airbytehq/airbyte"]
    assert all(issue_id_of_record(h.id).startswith("airbytehq/airbyte#") for h in fixed.issues)

    selected = retriever.retrieve("이 문제 다른 버전에도 있나요?", selected_issue_id="keycloak/keycloak#3")
    assert selected.route.repos == ["keycloak/keycloak"]
    assert [e.document_type for e in selected.evidence[:2]] == ["issue_metadata", "issue_body"]
    assert selected.evidence[0].issue_id == "keycloak/keycloak#3" and selected.evidence[0].forced
    assert "keycloak/keycloak#3" in selected.trace["top_issue_ids"]


def test_hybrid_mode_uses_both_rankings_and_restricts_documents(index_dirs):
    retriever = make_retriever(index_dirs)
    result = retriever.retrieve("Keycloak login TokenVerificationException")
    assert result.route.reason == "질문에 명시된 저장소"
    assert result.trace["issue_rankings"]["sparse"] and result.trace["issue_rankings"]["dense"]
    assert result.trace["weights"] == {"sparse": 0.6, "dense": 0.4}  # error_log 가중치
    top = set(result.trace["top_issue_ids"])
    assert all(e.issue_id in top for e in result.evidence)
    with pytest.raises(ValueError):
        retriever.retrieve("x", mode="bm42")


def test_search_cli(index_dirs, capsys):
    from scripts.search import main

    embed = FakeEmbedder()
    args = ["TokenVerificationException after upgrade", "--mode", "sparse",
            "--chroma-dir", str(index_dirs / "chroma"), "--sparse-dir", str(index_dirs / "sparse")]
    assert main(args, embed_query=lambda t: embed([t])[0]) == 0
    out = capsys.readouterr().out
    assert "라우팅: ['keycloak/keycloak'" in out and "[근거 1] keycloak/keycloak#2" in out
    assert main(args + ["--json"], embed_query=lambda t: embed([t])[0]) == 0
    assert '"query_type": "error_log"' in capsys.readouterr().out
