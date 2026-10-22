"""에이전트 코퍼스 구축 (기획서 17.12절 단계 D)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agent.library_corpus import (LibraryCorpusBuilder, LibraryIndex, discussion_item_and_documents,
                                  ensure_libraries, fetch_discussions, load_state, needs_build, prefilter,
                                  resolution_kind)
from agent.registry import Corpus, Library, Resolution
from src.config import load_yaml
from src.indexing.bm25_indexer import load_bm25_ids
from tests.conftest import FakeResponse

ROOT_CONFIGS = __import__("pathlib").Path(__file__).resolve().parents[1] / "configs"
REPO = "openai/openai-python"
BASE = f"/repos/{REPO}"
LONG = "Calling client.chat.completions.create raises openai.APITimeoutError on 1.40.0 with stream=True. " * 2
TAGGING = load_yaml(ROOT_CONFIGS / "repositories.yaml")["tagging"]
RETRIEVAL = load_yaml(ROOT_CONFIGS / "retrieval.yaml")


def issue(number, labels=("bug",), state_reason="completed", comments=2, body=LONG, closed="2026-09-01T00:00:00Z"):
    return {"number": number, "title": f"Issue {number}", "body": body, "labels": [{"name": n} for n in labels],
            "state": "closed", "state_reason": state_reason, "comments": comments, "author_association": "NONE",
            "created_at": "2026-08-01T00:00:00Z", "updated_at": closed, "closed_at": closed,
            "html_url": f"https://github.com/{REPO}/issues/{number}", "user": {"login": "reporter"}}


def detail_routes(number, comments, timeline=(), prs=(), data=None):
    routes = {f"{BASE}/issues/{number}": [FakeResponse(json_data=data or issue(number))],
              f"{BASE}/issues/{number}/comments": [FakeResponse(json_data=comments)],
              f"{BASE}/issues/{number}/timeline": [FakeResponse(json_data=list(timeline))]}
    for pr in prs:
        routes[f"{BASE}/pulls/{pr}"] = [FakeResponse(json_data={"number": pr, "title": "Fix timeout", "merged": True,
                                                                 "body": "Retry on timeout", "html_url": "u"})]
        routes[f"{BASE}/pulls/{pr}/files"] = [FakeResponse(json_data=[{"filename": "src/openai/_client.py",
                                                                        "patch": "@@ -1 +1 @@\n-a\n+b"}])]
    return routes


def maintainer(body):
    return {"id": 1, "body": body, "author_association": "MEMBER", "user": {"login": "maint"}}


def routes():
    search = [issue(1), issue(2, labels=("question",), state_reason="not_planned"), issue(3, labels=("enhancement",)),
              issue(4, comments=0), issue(5), issue(6, body="short")]
    merged = [{"event": "cross-referenced", "source": {"issue": {"number": 50, "pull_request": {},
                                                                   "repository": {"full_name": REPO}}}}]
    return {"/search/issues": [FakeResponse(json_data={"total_count": len(search), "items": search})],
            **detail_routes(1, [maintainer("Fixed in #50")], merged, prs=[50]),
            **detail_routes(2, [maintainer("Pass `timeout=httpx.Timeout(60.0)`; this is expected on 1.40.0.")],
                            data=search[1]),
            **detail_routes(5, [{"id": 2, "body": "same here", "author_association": "NONE", "user": {"login": "x"}}])}


DISCUSSION = {"number": 900, "title": "How do I retry on RateLimitError?", "body": "I get openai.RateLimitError on 1.40.0",
              "url": "https://github.com/openai/openai-python/discussions/900", "createdAt": "2026-08-01T00:00:00Z",
              "updatedAt": "2026-08-02T00:00:00Z", "category": {"name": "Q&A", "isAnswerable": True},
              "author": {"login": "asker"},
              "answer": {"body": "Use `max_retries=5` on the client.", "url": "https://github.com/x#answer",
                         "createdAt": "2026-08-02T00:00:00Z", "authorAssociation": "MEMBER", "author": {"login": "maint"}}}


def fake_post(nodes, calls=None):
    def post(url, body):
        if calls is not None:
            calls.append(body["variables"])
        return {"data": {"repository": {"discussions": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}}
    return post


def builder(make_client, tmp_path, post=None, **kwargs):
    client, session = make_client(routes(), cache=False)
    index = LibraryIndex(tmp_path / "agent_index", RETRIEVAL)  # 임베딩 없이 BM25만
    return LibraryCorpusBuilder(client, index, TAGGING, post=post, **kwargs), session


def test_prefilter_and_resolution_kind():
    tag = {"issue_type": "bug", "stale": False}
    assert prefilter({"issue_type": "feature"}, {}) == "type:feature"
    assert prefilter(tag, {"state_reason": "duplicate"}) == "duplicate"
    assert prefilter(tag, {"comments": 0, "body": LONG}) == "no_comments"
    assert prefilter(tag, {"comments": 1, "body": "x"}) == "body_too_short"
    assert prefilter(tag, {"comments": 1, "body": LONG}) is None

    strong = {"evidence_level": "strong", "merged_pr": True, "maintainer_detail": False}
    detail = {"evidence_level": "medium", "maintainer_detail": True}
    assert resolution_kind({"state_reason": "completed"}, tag, strong) == "fix"
    assert resolution_kind({"state_reason": "completed"}, tag, {"evidence_level": "weak"}) is None
    # 질문·not_planned는 병합 PR이 있어도 유지보수자 설명이 있어야 사용법 근거가 된다
    assert resolution_kind({"state_reason": "not_planned"}, tag, strong) is None
    assert resolution_kind({"state_reason": "not_planned"}, tag, detail) == "usage"
    assert resolution_kind({"labels": ["Works As Intended"], "state_reason": "completed"}, tag, detail) == "usage"


def test_build_indexes_fix_usage_and_discussion(make_client, tmp_path):
    corpus_builder, session = builder(make_client, tmp_path, post=fake_post([DISCUSSION]))
    state = {}
    report = corpus_builder.build(Library(REPO, "llm_sdk", ["openai"]), deep_target=10, state=state)

    assert report.searched == 6 and report.prefiltered == 3
    assert report.rejected == {"type:feature": 1, "no_comments": 1, "body_too_short": 1,
                               "no_resolution:completed/none": 1}
    assert report.kinds == {"fix": 1, "usage": 1, "discussion": 1}
    assert report.indexed_issues == 2 and report.discussions == 1 and report.chunks > 0
    assert f"{BASE}/issues/3" not in session.paths()  # 사전 필터에서 빠진 이슈는 상세 수집하지 않는다

    entry = state[REPO]
    assert entry["issues"] == [f"{REPO}#1", f"{REPO}#2", f"{REPO}#900"]
    sparse = tmp_path / "agent_index" / "sparse"
    assert load_bm25_ids(sparse / "issue_metadata") == {"issue:openai:openai-python:1", "issue:openai:openai-python:2",
                                                        "issue:openai:openai-python:900"}
    documents_ids = load_bm25_ids(sparse / "issue_documents")
    assert any(":discussion_answer:" in i for i in documents_ids)
    assert any(":pull_request:50" in i for i in documents_ids)


def test_corpus_fields_pass_through_only_when_present():
    from src.indexing.document_indexer import document_records
    from src.indexing.metadata_indexer import issue_records

    item, documents = discussion_item_and_documents(REPO, DISCUSSION)
    meta = issue_records([item])[0]["metadata"]
    assert (meta["corpus"], meta["resolution_kind"], meta["library"]) == ("library", "discussion", REPO)
    records = document_records(documents, RETRIEVAL["chunking"])
    roles = {r["metadata"]["document_type"]: r["metadata"]["author_role"] for r in records}
    assert roles == {"discussion_question": "reporter", "discussion_answer": "maintainer"}
    assert all(r["metadata"]["corpus"] == "library" for r in records)

    plain = dict(documents[0])
    for key in ("corpus", "resolution_kind", "library"):
        plain.pop(key)
    assert "corpus" not in document_records([plain], {})[0]["metadata"]  # 챗봇 색인 레코드 해시는 그대로


def test_holdout_keeps_every_nth_accepted_issue_out_of_index(make_client, tmp_path):
    corpus_builder, _ = builder(make_client, tmp_path, holdout_every=2)
    state = {}
    report = corpus_builder.build(Library(REPO, "llm_sdk", ["openai"], discussions=False), 10, state)
    assert report.held_out == 1 and report.indexed_issues == 1
    assert state[REPO]["held_out"] == [f"{REPO}#2"]
    assert not any(f"{REPO}#2:" in i for i in load_bm25_ids(tmp_path / "agent_index" / "sparse" / "issue_documents"))


def test_deep_target_and_request_budget_stop_early(make_client, tmp_path):
    corpus_builder, session = builder(make_client, tmp_path)
    report = corpus_builder.build(Library(REPO, "llm_sdk", ["openai"], discussions=False), 1, {})
    assert report.indexed_issues == 1 and f"{BASE}/issues/2" not in session.paths()

    corpus_builder, session = builder(make_client, tmp_path / "b", request_budget=1)
    report = corpus_builder.build(Library(REPO, "llm_sdk", ["openai"], discussions=False), 10, {})
    assert report.stopped and report.detailed == 0


def test_incremental_build_skips_known_and_searches_since(make_client, tmp_path):
    corpus_builder, session = builder(make_client, tmp_path)
    state = {REPO: {"built_at": "2026-09-10T00:00:00Z", "issues": [f"{REPO}#1"], "held_out": [f"{REPO}#2"], "kinds": {"fix": 1}}}
    report = corpus_builder.build(Library(REPO, "llm_sdk", ["openai"], discussions=False), 10, state, mode="incremental")
    search = next(c for c in session.calls if c["url"].endswith("/search/issues"))
    assert "closed:>=2026-09-10" in search["params"]["q"]
    assert report.indexed_issues == 0 and f"{BASE}/issues/1" not in session.paths()
    assert state[REPO]["issues"] == [f"{REPO}#1"] and state[REPO]["held_out"] == [f"{REPO}#2"]


def test_fetch_discussions_pages_and_drops_unanswered():
    pages = iter([
        {"data": {"repository": {"discussions": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                                 "nodes": [DISCUSSION, {**DISCUSSION, "number": 901, "answer": None}]}}}},
        {"data": {"repository": {"discussions": {"pageInfo": {"hasNextPage": False},
                                                 "nodes": [{**DISCUSSION, "number": 902}]}}}},
    ])
    calls = []

    def post(url, body):
        calls.append(body["variables"])
        return next(pages)
    assert [n["number"] for n in fetch_discussions(post, REPO, 5)] == [900, 902]
    assert calls[1]["after"] == "c1" and calls[0]["owner"] == "openai"
    errors = []
    assert fetch_discussions(lambda url, body: {"errors": [{"message": "HTTP 502"}]}, REPO, 5, errors) == []
    assert errors == ["Discussions 조회 실패: HTTP 502"]  # 실패해도 구축은 계속하고 사유만 남긴다


def test_needs_build_and_ensure_libraries(make_client, tmp_path):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    fresh = {"built_at": (now - timedelta(days=1)).isoformat(), "embedded": True}
    old = {"built_at": (now - timedelta(days=8)).isoformat().replace("+00:00", "Z"), "embedded": True}
    assert needs_build({}, REPO, 7, now) == "full"
    assert needs_build({REPO: fresh}, REPO, 7, now) is None
    assert needs_build({REPO: old}, REPO, 7, now) == "incremental"
    bm25_only = {"built_at": fresh["built_at"]}  # 임베딩 없이 만든 구축 (기록이 없는 예전 상태 포함)
    assert needs_build({REPO: bm25_only}, REPO, 7, now) == "full"
    assert needs_build({REPO: bm25_only}, REPO, 7, now, embedded=False) is None

    corpus = Corpus([Library(REPO, "llm_sdk", ["openai"], discussions=False),
                     Library("pydantic/pydantic", "core", ["pydantic"], discussions=False),
                     Library("fastapi/fastapi", "core", ["fastapi"], discussions=False)], {})
    resolutions = [Resolution("openai", REPO, "curated", True), Resolution("pydantic", "pydantic/pydantic", "curated", True),
                   Resolution("fastapi", "fastapi/fastapi", "curated", True), Resolution("left-pad", None, "unknown", False)]
    root = tmp_path / "agent_index"
    (root).mkdir()
    (root / "corpus_state.json").write_text(json.dumps({"fastapi/fastapi": {"built_at": datetime.now(timezone.utc).isoformat()}}))
    corpus_builder, _ = builder(make_client, tmp_path)
    results = ensure_libraries(corpus_builder, corpus, resolutions, root, deep_target=5, max_new=1, refresh_days=7)
    assert [(r["repo"], r["status"]) for r in results] == [
        (REPO, "built"), ("pydantic/pydantic", "deferred"), ("fastapi/fastapi", "ready")]
    assert load_state(root)[REPO]["issues"]


def test_build_script_runs_one_library_and_skips_when_fresh(make_client, tmp_path, capsys):
    from scripts.build_agent_corpus import main

    client, _ = make_client(routes(), cache=False)
    out = tmp_path / "agent_index"
    argv = ["--repo", REPO, "--deep", "5", "--out", str(out), "--skip-embed"]
    assert main(argv, client=client, post=fake_post([DISCUSSION])) == 0
    assert "fix 1" in capsys.readouterr().out and load_state(out)[REPO]["kinds"]["usage"] == 1
    assert main(argv, client=client, post=fake_post([])) == 0
    assert "최신 (건너뜀)" in capsys.readouterr().out
    # 임베딩 없이 만든 구축은 임베딩 실행 때 처음부터 다시 한다
    from tests.test_indexing import FakeEmbedder
    client, _ = make_client(routes(), cache=False)
    assert main(argv[:-1], client=client, post=fake_post([DISCUSSION]), embedder=FakeEmbedder()) == 0
    assert "처음 구축" in capsys.readouterr().out and load_state(out)[REPO]["embedded"] is True
    assert main(["--repo", "nobody/nothing", "--out", str(out), "--skip-embed"], client=client) == 2


def test_build_embeds_into_chroma_with_corpus_metadata(make_client, tmp_path):
    from src.indexing.chroma_store import get_collection, open_client
    from tests.test_indexing import FakeEmbedder

    client, _ = make_client(routes(), cache=False)
    index = LibraryIndex(tmp_path / "agent_index", RETRIEVAL, FakeEmbedder())
    LibraryCorpusBuilder(client, index, TAGGING, post=fake_post([DISCUSSION])).build(Library(REPO, "llm_sdk", ["openai"]), 10, {})
    stored = get_collection(open_client(index.chroma_dir), "issue_metadata").get(include=["metadatas"])
    kinds = {m["issue_id"]: (m["corpus"], m["resolution_kind"]) for m in stored["metadatas"]}
    assert kinds == {f"{REPO}#1": ("library", "fix"), f"{REPO}#2": ("library", "usage"),
                     f"{REPO}#900": ("library", "discussion")}


def test_diagnose_script_rejudges_from_raw_without_network(make_client, tmp_path, capsys):
    from scripts.diagnose_agent_corpus import main

    corpus_builder, _ = builder(make_client, tmp_path)
    corpus_builder.build(Library(REPO, "llm_sdk", ["openai"], discussions=False), 10, {})
    assert main(["--repo", REPO, "--index", str(tmp_path / "agent_index")]) == 0
    out = capsys.readouterr().out
    assert "kind: fix 1, usage 1, None 1" in out and "level: " in out
    assert LONG.split()[0] not in out  # 원문은 출력하지 않는다
    assert main(["--repo", "nobody/nothing", "--index", str(tmp_path / "agent_index")]) == 1


def test_maintainer_answer_accepts_long_or_linked_explanations_only():
    from agent.library_corpus import candidate_order, maintainer_answer

    def comment(body, association="COLLABORATOR", login="maint"):
        return {"body": body, "author_association": association, "user": {"login": login}}
    explanation = "This is expected: the provider rejects tool messages without a matching call id. " * 3
    assert maintainer_answer([comment(explanation)], "reporter", set())
    assert maintainer_answer([comment("See https://python.langchain.com/docs/x")], "reporter", set())
    assert not maintainer_answer([comment("Closing, thanks!")], "reporter", set())  # 짧은 종료 안내
    assert not maintainer_answer([comment(explanation, "NONE", "someone")], "reporter", set())
    assert maintainer_answer([comment(explanation, "NONE", "closer")], "reporter", {"closer"})
    assert not maintainer_answer([comment(explanation, login="reporter")], "reporter", set())  # 작성자 본인

    tag = {"issue_type": "bug"}
    assert resolution_kind({"state_reason": "not_planned"}, tag, {"evidence_level": "weak", "maintainer_answer": True}) == "usage"
    assert resolution_kind({"state_reason": "completed"}, tag, {"evidence_level": "weak", "maintainer_answer": True}) == "fix"

    bot_closed = {"comments": 1, "state_reason": "not_planned", "closed_at": "2026-10-20"}
    discussed = {"comments": 4, "state_reason": "not_planned", "closed_at": "2026-09-01"}
    assert sorted([bot_closed, discussed], key=candidate_order, reverse=True)[0] is discussed


def test_bot_closed_issue_reports_its_own_reason(make_client, tmp_path):
    bot = {"login": "triage[bot]", "type": "Bot"}
    data = {**issue(5), "closed_by": bot}
    client, _ = make_client({"/search/issues": [FakeResponse(json_data={"items": [issue(5)]})],
                             **detail_routes(5, [{"id": 1, "body": "Closing automatically", "user": bot}], data=data)},
                            cache=False)
    report = LibraryCorpusBuilder(client, LibraryIndex(tmp_path, RETRIEVAL), TAGGING).build(
        Library(REPO, "llm_sdk", ["openai"], discussions=False), 5, {})
    assert report.rejected == {"no_resolution:bot_closed": 1}
