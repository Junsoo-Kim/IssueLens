"""에이전트 단계 F: PR 코멘트 작성, 게시·갱신, 포크 PR 건너뜀, A 방식 코퍼스 구축."""
from __future__ import annotations

import json

from agent.comment import MARKER, NO_FINDINGS, compose, publish, safe
from agent.diff_reader import DiffBundle
from agent.main import is_fork_pr, main
from agent.researcher import Research
from agent.reviewer import Finding, Review
from src.retrieval.evidence_selector import Evidence
from tests.conftest import FakeResponse
from tests.test_agent import FAKE_KEY
from tests.test_indexing import FakeEmbedder
from tests.test_library_corpus import REPO, routes
from tests.test_reviewer import FakeLLM

TARGET = "octo/app"
SHA = "abc1234def5678"


def evidence(number, issue, kind="comment", role="maintainer"):
    return Evidence(number, f"doc:{issue}:{kind}:{number}", issue, kind, "t", "x", f"https://github.com/{issue.replace('#', '/issues/')}",
                    author_role=role)


def finding(fid, kind="library_usage", **extra):
    base = {"id": fid, "file": "app/llm.py", "line": 5, "kind": kind, "summary": "스트리밍 호출", "library": "openai",
            "repo": REPO, "concern": "타임아웃"}
    if kind == "general":
        base.update(library=None, repo=None, summary="예외를 삼키고 빈 문자열을 반환", concern="호출부가 실패를 구분할 수 없음")
    return Finding(**{**base, **extra})


def sample():
    shared = evidence(2, f"{REPO}#1")
    first = Research("f1", "grounded", draft="재시도가 추가됐습니다 [근거 2]. @maintainer 가 설명함 [근거 3].\n확인: timeout 지정",
                     evidence=[evidence(1, f"{REPO}#9"), shared, evidence(3, f"{REPO}#2", "discussion_answer")], cited=[2, 3])
    second = Research("f2", "grounded", draft="같은 원인입니다 [근거 1].\n확인: 버전 확인",
                      evidence=[Evidence(1, shared.id, shared.issue_id, "comment", "t", "x", shared.source_url,
                                         author_role="maintainer")], cited=[1])
    review = Review([finding("f1"), finding("f2", kind="dependency", file="requirements.txt", line=2, summary="openai 0.28 -> 1.51"),
                     finding("f3", summary="모르는 호출"), finding("f4", kind="general")], "llm")
    third = Research("f3", "no_evidence", reason="근거 0건")
    return review, {"f1": first, "f2": second, "f3": third}


def test_compose_sections_renumbers_citations_and_links():
    review, researches = sample()
    body = compose(DiffBundle(f"{TARGET}#7", SHA, []), review, researches, TARGET)
    assert body.startswith(MARKER) and "## IssueLens 리뷰 (`abc1234` 기준)" in body
    rag, rest = body.split("### 일반 코드 리뷰 (RAG 미사용, 근거 없음)")
    assert f"**[app/llm.py:5](https://github.com/{TARGET}/blob/{SHA}/app/llm.py#L5)** `스트리밍 호출`" in rag
    # 초안마다 1부터 매긴 번호를 코멘트 전체 번호로 바꾸고, 같은 근거(#1 댓글)는 같은 번호를 쓴다
    assert "재시도가 추가됐습니다 [근거 1]." in rag and "[근거 2]." in rag and "같은 원인입니다 [근거 1]." in rag
    assert "  - 확인: timeout 지정" in rag
    assert "@​maintainer" in rag and "@maintainer" not in rag  # 멘션 알림 방지
    assert "모르는 호출. 타임아웃 (관련 라이브러리 이슈를 찾지 못함)" in rest  # 근거 부족 후보는 일반 리뷰로
    assert "예외를 삼키고 빈 문자열을 반환. 호출부가 실패를 구분할 수 없음" in rest
    assert f"[근거 1] {REPO}#1 · 이슈 댓글, 유지보수자 · https://github.com/{REPO}/issues/1" in rest
    assert f"[근거 2] {REPO}#2 · Discussions 채택 답변, 유지보수자" in rest and "[근거 3]" not in rest
    assert f"{REPO}#9" not in body  # 인용하지 않은 근거는 싣지 않는다


def test_compose_without_findings_and_notes():
    assert compose(DiffBundle("x", SHA, []), Review([], "llm"), {}) is None
    body = compose(DiffBundle("local.patch", None, [], truncated=True), Review([finding("f1", kind="general")], "rule"), {},
                   notes=["코퍼스 준비 중인 라이브러리: a/b"])
    assert "**app/llm.py:5**" in body and "(`" not in body.splitlines()[1]
    assert "> 코퍼스 준비 중인 라이브러리: a/b" in body and "> 변경이 커서 앞부분 파일만 리뷰했습니다." in body
    assert safe("a\n@b `@c` d@e") == "a @​b `@c` d@e"


def comment_routes(existing):
    base = f"/repos/{TARGET}/issues"
    return {f"{base}/7/comments": [FakeResponse(json_data=existing)],
            f"POST {base}/7/comments": [FakeResponse(201, {"id": 99})],
            f"PATCH {base}/comments/5": [FakeResponse(200, {"id": 5})]}


def test_publish_creates_updates_clears_and_skips(make_client):
    other = {"id": 4, "body": "LGTM"}
    mine = {"id": 5, "body": f"{MARKER}\n예전 리뷰"}
    client, session = make_client(comment_routes([other]), cache=False)
    assert publish(client, TARGET, 7, "새 리뷰", SHA) == "created"
    assert session.writes() == [("POST", f"/repos/{TARGET}/issues/7/comments", {"body": "새 리뷰"})]
    assert publish(client, TARGET, 7, None, SHA) == "skipped"  # 처음부터 지적이 없으면 코멘트를 만들지 않는다

    client, session = make_client(comment_routes([other, mine]), cache=False)
    assert publish(client, TARGET, 7, "갱신된 리뷰", SHA) == "updated"
    assert publish(client, TARGET, 7, None, SHA) == "cleared"
    (_, _, updated), (_, _, cleared) = session.writes()
    assert updated == {"body": "갱신된 리뷰"} and NO_FINDINGS in cleared["body"] and MARKER in cleared["body"]


def event(tmp_path, head_repo=TARGET):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"pull_request": {"number": 7, "head": {"repo": {"full_name": head_repo}},
                                                 "base": {"repo": {"full_name": TARGET}}}}), encoding="utf-8")
    return {"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": TARGET}


def test_fork_pr_is_skipped_without_api_calls(tmp_path, capsys):
    env = event(tmp_path, head_repo="someone/app")
    assert is_fork_pr(env) and not is_fork_pr(event(tmp_path / "..", TARGET))
    assert main(["--no-pypi", "--target-root", str(tmp_path), "--cache-dir", str(tmp_path)], env=env) == 0
    assert "포크에서 온 PR" in capsys.readouterr().out


PATCH = ("@@ -1,2 +1,4 @@\n import openai\n+def ask(p):\n+    return openai.ChatCompletion.create(model='m', "
         f"messages=[p], api_key='{FAKE_KEY}')\n")


def test_action_run_publishes_masked_comment(tmp_path, make_client, monkeypatch, capsys):
    routes_ = {f"/repos/{TARGET}/pulls/7": [FakeResponse(json_data={"number": 7, "head": {"sha": SHA}})],
               f"/repos/{TARGET}/pulls/7/files": [FakeResponse(json_data=[
                   {"filename": "app/llm.py", "status": "modified", "additions": 2, "deletions": 0, "patch": PATCH}])],
               **comment_routes([])}
    client, session = make_client(routes_, cache=False)
    monkeypatch.setattr("agent.main.github_client", lambda env, cache=None: client)
    (tmp_path / "requirements.txt").write_text("openai==1.51.0\n", encoding="utf-8")
    llm = FakeLLM(findings=[{"file": "app/llm.py", "line": 3, "kind": "general", "summary": f"키 {FAKE_KEY}를 코드에 둠",
                             "severity": "high"}])
    argv = ["--no-pypi", "--target-root", str(tmp_path), "--cache-dir", str(tmp_path), "--index", str(tmp_path / "idx")]
    assert main(argv, env=event(tmp_path), llm=llm, fast_llm=llm, embedder=FakeEmbedder()) == 0
    out = capsys.readouterr().out
    assert "게시: created" in out
    (method, path, body), = session.writes()
    assert method == "POST" and "app/llm.py:3" in body["body"]
    assert FAKE_KEY not in body["body"] and FAKE_KEY not in llm.prompts[0]  # patch는 마스킹 뒤에만 LLM에 들어간다


def test_build_corpus_mode_builds_needed_library_first(tmp_path, make_client, monkeypatch, capsys):
    corpus_client, _ = make_client(routes(), cache=False)
    monkeypatch.setattr("agent.main.github_client", lambda env, cache=None: corpus_client)
    monkeypatch.setattr("agent.library_corpus.default_post", lambda token: lambda url, body: None)
    patch = tmp_path / "changes.patch"
    patch.write_text("diff --git a/app/llm.py b/app/llm.py\n--- a/app/llm.py\n+++ b/app/llm.py\n" + PATCH, encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("openai\n", encoding="utf-8")
    llm = FakeLLM(findings=[{"file": "app/llm.py", "line": 3, "kind": "library_usage", "library": "openai",
                             "summary": "스트리밍 호출", "search_queries": ["stream timeout"], "exact_tokens": ["APITimeoutError"]}])
    argv = ["--diff", str(patch), "--dry-run", "--json", "--no-pypi", "--no-collect", "--build-corpus",
            "--target-root", str(tmp_path), "--cache-dir", str(tmp_path), "--index", str(tmp_path / "idx")]
    assert main(argv, env={}, llm=llm, fast_llm=llm, embedder=FakeEmbedder()) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(c["repo"], c["status"]) for c in data["corpus"]] == [(REPO, "built")]
    assert data["research"][0]["status"] == "grounded" and "### 알려진 이슈 기반 (RAG)" in data["comment"]


def test_action_files_keep_security_constraints():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / "docs/issuelens_review_workflow.yml").read_text(encoding="utf-8"))
    action_text = (root / "action.yml").read_text(encoding="utf-8")
    triggers = workflow[True] if True in workflow else workflow["on"]  # YAML 1.1은 on을 True로 읽는다
    assert set(triggers) == {"pull_request"} and "pull_request_target" not in action_text
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert "head.repo.full_name == github.repository" in workflow["jobs"]["review"]["if"]
    review_step = yaml.safe_load(action_text)["runs"]["steps"][-1]
    assert "${{" not in review_step["run"]  # 입력값은 env로만 넘겨 셸에 직접 끼워 넣지 않는다
