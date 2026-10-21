"""수집기 테스트."""
from __future__ import annotations

import json

import pytest
import requests

from src.collectors.cache import Manifest, RawStore, read_jsonl
from src.collectors.comment_collector import comment_priority, fetch_comments
from src.collectors.github_client import GitHubAPIError, strip_personal_fields
from src.collectors.issue_collector import (
    NotAnIssueError,
    build_search_query,
    fetch_issue,
    search_closed_issues,
)
from src.collectors.pull_request_collector import fetch_pull_request, summarize_pull_request
from src.collectors.runner import load_selection, run_deep, run_shallow
from src.collectors.timeline_collector import extract_links, extract_reference_urls
from tests.conftest import FakeResponse

REPO = "keycloak/keycloak"
SEARCH = "/search/issues"


def issue_item(number, **extra):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "TokenVerificationException after upgrade to 26.0.1",
        "labels": [{"name": "kind/bug"}],
        "state": "closed",
        "state_reason": "completed",
        "comments": 2,
        "author_association": "NONE",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-02-01T00:00:00Z",
        "closed_at": "2026-02-01T00:00:00Z",
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "user": {"login": "reporter", "avatar_url": "https://avatars/x", "email": "a@b.c"},
        **extra,
    }


def search_page(items, total=None, incomplete=False):
    return {"total_count": total if total is not None else len(items), "incomplete_results": incomplete, "items": items}


@pytest.fixture
def raw_store(tmp_path):
    return RawStore(tmp_path / "raw")


# ---------------------------------------------------------------- 클라이언트


def test_sends_github_headers_and_timeout(make_client):
    client, session = make_client({"/rate_limit": [FakeResponse(json_data={"resources": {}})]})
    client.get("/rate_limit")
    call = session.calls[0]
    assert call["headers"]["Authorization"] == "Bearer test-token"
    assert call["headers"]["Accept"] == "application/vnd.github+json"
    assert call["headers"]["X-GitHub-Api-Version"] == "2026-03-10"
    assert call["headers"]["User-Agent"] == "incident-rag-project/1.0"
    assert call["timeout"] == (5, 20)


def test_omits_authorization_without_token(make_client):
    client, session = make_client({"/rate_limit": [FakeResponse(json_data={})]}, token=None)
    client.get("/rate_limit")
    assert "Authorization" not in session.calls[0]["headers"]


def test_pagination_follows_next_link_and_respects_max_pages(make_client):
    page2 = "https://api.github.com/x?page=2"
    page3 = "https://api.github.com/x?page=3"
    routes = {
        "/x": [FakeResponse(json_data=[1], next_url=page2)],
        page2: [FakeResponse(json_data=[2], next_url=page3)],
        page3: [FakeResponse(json_data=[3])],
    }
    client, _ = make_client(routes)
    assert [p.data for p in client.paginate("/x")] == [[1], [2], [3]]

    client, session = make_client(routes, cache=False)
    assert [p.data for p in client.paginate("/x", max_pages=2)] == [[1], [2]]
    assert len(session.calls) == 2


def test_refuses_next_url_outside_api_host(make_client):
    client, _ = make_client({"/x": [FakeResponse(json_data=[1], next_url="https://evil.example/x")]})
    pages = client.paginate("/x")
    next(pages)
    with pytest.raises(ValueError):
        next(pages)


def test_retries_server_errors_with_exponential_backoff(make_client, clock):
    client, session = make_client({"/x": [FakeResponse(502), FakeResponse(503), FakeResponse(json_data={"ok": 1})]})
    assert client.get("/x").data == {"ok": 1}
    assert clock.sleeps == [1, 2]
    assert client.stats.retries == 2
    assert len(session.calls) == 3


def test_honors_retry_after_header(make_client, clock):
    client, _ = make_client({"/x": [FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(json_data={})]})
    client.get("/x")
    assert clock.sleeps == [7]


def test_waits_until_primary_rate_limit_reset(make_client, clock):
    reset = int(clock.time()) + 30
    limited = FakeResponse(403, {"message": "API rate limit exceeded"},
                           headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)})
    client, _ = make_client({"/x": [limited, FakeResponse(json_data={})]})
    client.get("/x")
    assert clock.sleeps == [31]


def test_secondary_rate_limit_waits_at_least_one_minute(make_client, clock):
    secondary = FakeResponse(403, {"message": "You have exceeded a secondary rate limit."})
    client, _ = make_client({"/x": [secondary, secondary, FakeResponse(json_data={})]})
    client.get("/x")
    assert clock.sleeps == [60, 120]


@pytest.mark.parametrize("status", [403, 404])
def test_permission_and_not_found_errors_are_not_retried(make_client, status):
    client, session = make_client({"/x": [FakeResponse(status, {"message": "nope"})]})
    with pytest.raises(GitHubAPIError) as info:
        client.get("/x")
    assert info.value.status == status
    assert info.value.retryable is False
    assert len(session.calls) == 1


def test_gives_up_after_max_retries(make_client):
    client, session = make_client({"/x": [FakeResponse(500)]})
    with pytest.raises(GitHubAPIError) as info:
        client.get("/x")
    assert info.value.retryable is True
    assert len(session.calls) == 4  # 최초 1회 + 재시도 3회


def test_retries_network_errors(make_client, clock):
    client, _ = make_client({"/x": [requests.ConnectionError("reset"), FakeResponse(json_data={"ok": 1})]})
    assert client.get("/x").data == {"ok": 1}
    assert clock.sleeps == [1]


def test_etag_cache_reuses_body_on_304(make_client):
    first = FakeResponse(json_data={"v": 1}, headers={"ETag": '"abc"'})
    client, _ = make_client({"/x": [first]})
    client.get("/x", {"page": 1})

    client, session = make_client({"/x": [FakeResponse(304)]})
    response = client.get("/x", {"page": 1})
    assert session.calls[0]["headers"]["If-None-Match"] == '"abc"'
    assert response.from_cache and response.data == {"v": 1}
    assert client.stats.cache_hits == 1


def test_refresh_skips_conditional_headers(make_client):
    client, _ = make_client({"/x": [FakeResponse(json_data={"v": 1}, headers={"ETag": '"abc"'})]})
    client.get("/x")
    client, session = make_client({"/x": [FakeResponse(json_data={"v": 2})]}, refresh=True)
    assert client.get("/x").data == {"v": 2}
    assert "If-None-Match" not in session.calls[0]["headers"]


def test_search_requests_use_longer_interval(make_client, clock):
    client, _ = make_client({SEARCH: [FakeResponse(json_data=search_page([]))]}, search_interval_seconds=2.0)
    client.get(SEARCH, use_cache=False)
    client.get(SEARCH, use_cache=False)
    assert clock.sleeps == [2.0]


def test_strip_personal_fields():
    data = {"user": {"login": "u", "type": "User", "avatar_url": "x", "html_url": "y"},
            "commit": {"author": {"name": "n", "email": "e@x"}}}
    assert strip_personal_fields(data) == {"user": {"login": "u", "type": "User"}, "commit": {"author": {"name": "n"}}}


# ---------------------------------------------------------------- 이슈 수집


def test_build_search_query():
    assert build_search_query(REPO) == f"repo:{REPO} is:issue is:closed"
    assert build_search_query(REPO, since="2026-01-01").endswith("updated:>=2026-01-01")


def test_search_excludes_pull_requests_and_saves_raw_pages(make_client, raw_store):
    page2 = "https://api.github.com/search/issues?page=2"
    routes = {
        SEARCH: [FakeResponse(json_data=search_page([issue_item(1), issue_item(2, pull_request={})], total=3),
                              next_url=page2)],
        page2: [FakeResponse(json_data=search_page([issue_item(3)], total=3))],
    }
    client, session = make_client(routes)
    result = search_closed_issues(client, REPO, raw_store)

    assert [i["number"] for i in result.issues] == [1, 3]
    assert result.skipped_pull_requests == 1
    assert result.pages == 2
    assert session.calls[0]["params"]["q"] == f"repo:{REPO} is:issue is:closed"
    assert session.calls[0]["params"]["per_page"] == 100
    assert raw_store.path(REPO, "search", "closed_page_001").exists()
    assert raw_store.path(REPO, "search", "closed_page_002").exists()
    record = result.issues[0]
    assert record["issue_id"] == f"{REPO}#1" and record["labels"] == ["kind/bug"]


def test_search_stops_at_limit_and_dedupes(make_client, raw_store):
    page2 = "https://api.github.com/search/issues?page=2"
    routes = {
        SEARCH: [FakeResponse(json_data=search_page([issue_item(1), issue_item(2)]), next_url=page2)],
        page2: [FakeResponse(json_data=search_page([issue_item(2), issue_item(3), issue_item(4)]))],
    }
    client, _ = make_client(routes)
    result = search_closed_issues(client, REPO, raw_store, limit=3)
    assert [i["number"] for i in result.issues] == [1, 2, 3]


def test_search_flags_incomplete_results(make_client, raw_store):
    client, _ = make_client({SEARCH: [FakeResponse(json_data=search_page([issue_item(1)], incomplete=True))]})
    assert search_closed_issues(client, REPO, raw_store).incomplete is True


def test_fetch_issue_rejects_pull_request_and_strips_personal_data(make_client, raw_store):
    client, _ = make_client({
        f"/repos/{REPO}/issues/1": [FakeResponse(json_data=issue_item(1))],
        f"/repos/{REPO}/issues/2": [FakeResponse(json_data=issue_item(2, pull_request={}))],
    })
    fetch_issue(client, REPO, 1, raw_store)
    saved = json.loads(raw_store.path(REPO, "issues", "1").read_text())
    assert saved["user"] == {"login": "reporter"}
    with pytest.raises(NotAnIssueError):
        fetch_issue(client, REPO, 2, raw_store)


# ---------------------------------------------------------------- 댓글, 타임라인, PR


def test_fetch_comments_collects_all_pages(make_client, raw_store):
    page2 = f"https://api.github.com/repos/{REPO}/issues/1/comments?page=2"
    client, _ = make_client({
        f"/repos/{REPO}/issues/1/comments": [FakeResponse(json_data=[{"id": 1}], next_url=page2)],
        page2: [FakeResponse(json_data=[{"id": 2}])],
    })
    assert [c["id"] for c in fetch_comments(client, REPO, 1, raw_store)] == [1, 2]
    assert raw_store.path(REPO, "comments", "1").exists()


def test_comment_priority_follows_planned_order():
    maintainer = {"id": 1, "author_association": "MEMBER", "body": "thanks"}
    technical = {"id": 2, "author_association": "NONE",
                 "body": "Fixed in 26.0.2 ```stack```", "reactions": {"total_count": 10}, "user": {"login": "reporter"}}
    thanks = {"id": 3, "author_association": "NONE", "body": "+1"}
    scores = {c["id"]: comment_priority(c, issue_author="reporter")["score"] for c in (maintainer, technical, thanks)}
    assert scores[1] > scores[2] > scores[3] == 0
    assert comment_priority(technical, "reporter")["signals"] == [
        "technical_detail", "resolution_signal", "reactions", "issue_author"]
    assert "resolution_signal" not in comment_priority({"body": "prefixed value"})["signals"]


def test_extract_links_from_timeline_and_text():
    timeline = [
        {"event": "cross-referenced", "source": {"issue": {
            "number": 101, "pull_request": {}, "repository": {"full_name": "Keycloak/keycloak"}}}},
        {"event": "cross-referenced", "source": {"issue": {
            "number": 7, "pull_request": {}, "html_url": "https://github.com/other/repo/pull/7"}}},
        {"event": "cross-referenced", "source": {"issue": {"number": 55, "repository": {"full_name": REPO}}}},
        {"event": "referenced", "commit_id": "abc", "commit_url": "https://api.github.com/c/abc"},
        {"event": "closed", "commit_id": "def", "commit_url": "https://api.github.com/c/def"},
        {"event": "labeled"},
    ]
    texts = [f"Fixed by #102. See https://github.com/{REPO}/pull/103 and #1", "Duplicate of #40"]
    links = extract_links(timeline, REPO, texts, issue_number=1)

    assert links["pull_requests"] == [
        {"number": 101, "source": "timeline"}, {"number": 103, "source": "text"}, {"number": 102, "source": "text"}]
    assert links["external_references"] == ["https://github.com/other/repo/pull/7"]
    assert [c["sha"] for c in links["commits"]] == ["abc", "def"]
    assert links["closed_by_commit"] == "def"
    assert links["duplicates"] == [40]
    assert links["event_counts"]["cross-referenced"] == 3


def test_extract_reference_urls_classifies_by_allowlist():
    texts = [
        "Docs: https://www.keycloak.org/docs/latest/server_admin/. "
        f"PR https://github.com/{REPO}/pull/9, blog https://medium.com/post",
        "https://www.keycloak.org/docs/latest/server_admin/",
    ]
    urls = extract_reference_urls(texts, REPO, ["www.keycloak.org"])
    assert urls == {
        "official_docs": ["https://www.keycloak.org/docs/latest/server_admin/"],
        "github": [f"https://github.com/{REPO}/pull/9"],
        "other": ["https://medium.com/post"],
    }


def test_fetch_pull_request_returns_none_for_non_pr(make_client, raw_store):
    client, _ = make_client({f"/repos/{REPO}/pulls/5": [FakeResponse(404, {"message": "Not Found"})]})
    assert fetch_pull_request(client, REPO, 5, raw_store) is None


def test_summarize_pull_request_marks_missing_patch():
    pr = {"number": 9, "title": "fix", "state": "closed", "merged_at": "2026-01-01T00:00:00Z", "html_url": "u"}
    files = [{"filename": "a.java", "status": "modified", "additions": 1, "deletions": 1, "patch": "@@"},
             {"filename": "big.bin", "status": "added", "additions": 0, "deletions": 0}]
    summary = summarize_pull_request(pr, files)
    assert summary["merged"] is True
    assert [f["patch_available"] for f in summary["changed_files"]] == [True, False]
    assert summary["patch_available"] is False


# ---------------------------------------------------------------- 실행 흐름


@pytest.fixture
def shallow_env(make_client, tmp_path, raw_store):
    def factory(repos: dict[str, list[int]], search_responses=None):
        routes = {}
        for repo in repos:
            routes[f"/repos/{repo}/license"] = [FakeResponse(json_data={
                "html_url": f"https://github.com/{repo}/blob/main/LICENSE",
                "license": {"spdx_id": "Apache-2.0", "name": "Apache License 2.0"}})]
        # 저장소 순서대로 검색 응답을 소비한다
        routes[SEARCH] = search_responses or [
            FakeResponse(json_data=search_page([issue_item(n) for n in numbers])) for numbers in repos.values()]
        routes[SEARCH].append(FakeResponse(500))  # 준비한 응답보다 많이 호출하면 실패하게 둔다
        client, session = make_client(routes, cache=False)
        manifest = Manifest(tmp_path / "manifests" / "shallow_manifest.jsonl")
        return client, session, manifest

    return factory


def test_run_shallow_writes_outputs_without_detail_calls(shallow_env, tmp_path, raw_store):
    client, session, manifest = shallow_env({REPO: [1, 2], "airbytehq/airbyte": [3]})
    repos = [{"repo": REPO, "shallow_candidate_limit": 1000}, {"repo": "airbytehq/airbyte", "shallow_candidate_limit": 1000}]
    stats = run_shallow(client, repos, raw_store, manifest, tmp_path / "processed")

    merged = read_jsonl(tmp_path / "processed" / "shallow_issues.jsonl")
    assert sorted(r["issue_id"] for r in merged) == ["airbytehq/airbyte#3", f"{REPO}#1", f"{REPO}#2"]
    assert stats["totals"]["shallow_candidates"] == 3
    assert manifest.is_done(f"shallow:{REPO}")
    # 얕은 수집에서는 댓글, 타임라인, PR API를 호출하지 않는다
    assert all(p == SEARCH or p.endswith("/license") for p in session.paths())
    source = json.loads((tmp_path / "manifests" / "source_manifest.json").read_text())
    assert source["repositories"][REPO]["license"]["spdx_id"] == "Apache-2.0"


def test_run_shallow_resume_skips_completed_repositories(shallow_env, tmp_path, raw_store):
    repos = [{"repo": REPO}]
    client, _, manifest = shallow_env({REPO: [1, 2]})
    run_shallow(client, repos, raw_store, manifest, tmp_path / "processed")

    client, session, manifest = shallow_env({REPO: [1, 2]})
    stats = run_shallow(client, repos, raw_store, manifest, tmp_path / "processed", resume=True)
    assert session.calls == []
    assert stats["repositories"][0]["status"] == "skipped"
    assert stats["totals"]["shallow_candidates"] == 2


def test_run_shallow_rerun_does_not_duplicate(shallow_env, tmp_path, raw_store):
    repos = [{"repo": REPO}]
    for _ in range(2):
        client, _, manifest = shallow_env({REPO: [1, 2]})
        run_shallow(client, repos, raw_store, manifest, tmp_path / "processed")
    assert len(read_jsonl(tmp_path / "processed" / "shallow_issues.jsonl")) == 2


def test_run_shallow_incomplete_results_not_marked_done(shallow_env, tmp_path, raw_store):
    client, _, manifest = shallow_env({REPO: []}, search_responses=[
        FakeResponse(json_data=search_page([issue_item(1)], incomplete=True))])
    stats = run_shallow(client, [{"repo": REPO}], raw_store, manifest, tmp_path / "processed")
    assert manifest.get(f"shallow:{REPO}")["status"] == "incomplete"
    assert stats["failures"][0]["reason"] == "incomplete_results"


def test_run_shallow_aborts_on_exhausted_retries(shallow_env, tmp_path, raw_store):
    client, session, manifest = shallow_env({REPO: [], "airbytehq/airbyte": []}, search_responses=[FakeResponse(503)])
    stats = run_shallow(client, [{"repo": REPO}, {"repo": "airbytehq/airbyte"}], raw_store, manifest, tmp_path / "processed")
    assert stats["aborted"]["key"] == f"shallow:{REPO}"
    assert manifest.get(f"shallow:{REPO}")["status"] == "failed"
    assert not any("airbyte" in p for p in session.paths())


def deep_routes(number, pr_number):
    base = f"/repos/{REPO}"
    return {
        f"{base}/issues/{number}": [FakeResponse(json_data=issue_item(number, body="Fixed by #999, docs https://www.keycloak.org/x"))],
        f"{base}/issues/{number}/comments": [FakeResponse(json_data=[{"id": 1, "body": "maintainer note"}])],
        f"{base}/issues/{number}/timeline": [FakeResponse(json_data=[{"event": "cross-referenced", "source": {"issue": {
            "number": pr_number, "pull_request": {}, "repository": {"full_name": REPO}}}}])],
        f"{base}/pulls/{pr_number}": [FakeResponse(json_data={"number": pr_number, "title": "fix", "merged": True})],
        f"{base}/pulls/{pr_number}/files": [FakeResponse(json_data=[{"filename": "A.java", "patch": "@@"}])],
        f"{base}/pulls/999": [FakeResponse(404, {"message": "Not Found"})],
    }


def test_run_deep_collects_details_and_resumes(make_client, tmp_path, raw_store):
    client, session = make_client(deep_routes(1, 50), cache=False)
    manifest = Manifest(tmp_path / "deep_manifest.jsonl")
    kwargs = dict(docs_allowlists={REPO: ["www.keycloak.org"]},
                  include_comments=True, include_timeline=True, include_linked_prs=True)
    stats = run_deep(client, [(REPO, 1)], raw_store, manifest, tmp_path / "processed", **kwargs)

    assert stats["issues"]["collected"] == 1
    record = read_jsonl(tmp_path / "processed" / "deep_issues.jsonl")[0]
    assert [pr["number"] for pr in record["linked_pull_requests"]] == [50]  # 999는 PR이 아니라 제외
    assert record["comments_collected"] == 1
    assert record["reference_urls"]["official_docs"] == ["https://www.keycloak.org/x"]
    for kind, name in [("issues", "1"), ("comments", "1"), ("timeline", "1"), ("pulls", "50"), ("pull_files", "50")]:
        assert raw_store.path(REPO, kind, name).exists()

    client, session = make_client(deep_routes(1, 50), cache=False)
    stats = run_deep(client, [(REPO, 1)], raw_store, Manifest(tmp_path / "deep_manifest.jsonl"),
                     tmp_path / "processed", resume=True, **kwargs)
    assert session.calls == [] and stats["issues"]["skipped"] == 1
    assert len(read_jsonl(tmp_path / "processed" / "deep_issues.jsonl")) == 1


def test_run_deep_without_flags_only_fetches_issue(make_client, tmp_path, raw_store):
    client, session = make_client(deep_routes(1, 50), cache=False)
    run_deep(client, [(REPO, 1)], raw_store, Manifest(tmp_path / "m.jsonl"), tmp_path / "processed")
    assert session.paths() == [f"/repos/{REPO}/issues/1"]


def test_run_deep_records_pull_request_objects_as_failed(make_client, tmp_path, raw_store):
    client, _ = make_client({f"/repos/{REPO}/issues/2": [FakeResponse(json_data=issue_item(2, pull_request={}))]})
    manifest = Manifest(tmp_path / "m.jsonl")
    stats = run_deep(client, [(REPO, 2)], raw_store, manifest, tmp_path / "processed")
    assert stats["issues"]["failed"] == 1 and stats["aborted"] is None
    assert manifest.get(f"deep:{REPO}#2")["status"] == "failed"


def test_load_selection_accepts_both_formats(tmp_path):
    path = tmp_path / "selection.jsonl"
    path.write_text('{"repo": "a/b", "number": 1}\n{"issue_id": "a/b#2"}\n{"issue_id": "a/b#1"}\n')
    assert load_selection(path) == [("a/b", 1), ("a/b", 2)]


def test_cli_shallow_end_to_end(shallow_env, tmp_path, monkeypatch, capsys):
    import scripts.collect_issues as cli

    client, _, _ = shallow_env({REPO: [1, 2]})
    monkeypatch.setattr(cli, "build_client", lambda *args, **kwargs: client)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    app_config = tmp_path / "app.yaml"
    app_config.write_text("paths:\n" + "".join(
        f"  {name}: {tmp_path / name}\n" for name in ("raw_dir", "processed_dir", "cache_dir", "manifest_dir")))

    code = cli.main(["--mode", "shallow", "--repo", REPO, "--app-config", str(app_config)])
    assert code == 0
    assert len(read_jsonl(tmp_path / "processed_dir" / "shallow_issues.jsonl")) == 2
    stats = json.loads((tmp_path / "processed_dir" / "collection_stats.json").read_text())
    assert stats["shallow"]["totals"]["shallow_candidates"] == 2
    assert "얕은 수집 후보: 2" in capsys.readouterr().out

    assert cli.main(["--mode", "shallow", "--repo", "no/such", "--app-config", str(app_config)]) == 2
