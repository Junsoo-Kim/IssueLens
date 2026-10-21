"""해결 근거 판정과 근거 기반 최종 분리 테스트."""
from __future__ import annotations

import json

import pytest

from src.collectors.cache import RawStore, write_jsonl_atomic
from src.preprocessing.resolution import FinalTargets, assign_splits, infer_maintainers, judge_evidence

REPO = "keycloak/keycloak"
DOCS = ["www.keycloak.org"]


def comment(body, association="NONE", login="someone", user_type="User"):
    return {"id": hash(body) % 10_000, "body": body, "author_association": association,
            "user": {"login": login, "type": user_type}}


@pytest.mark.parametrize("summary,comments,expected", [
    ({"linked_pull_requests": [{"number": 5, "merged": True}]}, [], "strong"),
    ({"closed_by_commit": "abc"}, [], "strong"),
    ({"linked_pull_requests": [{"number": 5, "merged": False}]}, [], "none"),
    ({}, [comment("Fixed in 26.0.2, please upgrade", "MEMBER")], "medium"),
    ({}, [comment("thanks", "MEMBER")], "weak"),
    ({}, [comment("this was resolved for me", "NONE")], "weak"),
    ({}, [comment("See https://www.keycloak.org/docs/x")], "weak"),
    ({}, [comment("Closing as stale, fixed?", "NONE", "github-actions[bot]", "Bot")], "none"),
    ({}, [], "none"),
])
def test_judge_evidence_levels(summary, comments, expected):
    assert judge_evidence({"repo": REPO, **summary}, comments, docs_allowlist=DOCS)["evidence_level"] == expected


def test_inferred_maintainer_counts_as_maintainer():
    detail = [comment("Fixed in 26.0.2", "CONTRIBUTOR", login="core-dev")]
    assert judge_evidence({"repo": REPO}, detail)["evidence_level"] == "weak"  # 해결 신호 댓글로만 인정
    assert judge_evidence({"repo": REPO}, detail, maintainers={"core-dev"})["evidence_level"] == "medium"


def test_docs_link_in_issue_body_is_not_evidence():
    summary = {"repo": REPO, "reference_urls": {"official_docs": ["https://www.keycloak.org/docs/x"]}}
    assert judge_evidence(summary, [], docs_allowlist=DOCS)["evidence_level"] == "none"


def test_connected_without_pr_and_bot_counts():
    result = judge_evidence({"repo": REPO, "timeline_events": {"connected": 1}},
                            [comment("stale", login="stale[bot]"), comment("ok")])
    assert result["connected_without_pr"] is True
    assert (result["human_comments"], result["bot_comments"]) == (1, 1)


def test_infer_maintainers_from_closers():
    issues = ([{"closed_by": {"login": "triager", "type": "User"}}] * 3
              + [{"closed_by": {"login": "reporter", "type": "User"}}]
              + [{"closed_by": {"login": "stale[bot]", "type": "Bot"}}] * 5
              + [{"closed_by": None}])
    assert infer_maintainers(issues) == {"triager"}


def row(number, level, day, quality=5):
    return {"issue_id": f"{REPO}#{number}", "repo": REPO, "number": number, "evidence_level": level,
            "closed_at": f"2026-01-{day:02d}T00:00:00Z", "quality_score": quality, "signal_count": 3}


def splits(rows):
    return {r["number"]: r for r in rows}


def test_assign_splits_evaluation_newest_and_index_strictly_older():
    rows = [
        row(1, "strong", 1), row(2, "none", 2), row(3, "weak", 3), row(4, "medium", 4), row(5, "strong", 5),
        row(6, "weak", 6), row(7, "none", 7), row(8, "medium", 8), row(10, "none", 10), row(9, "weak", 9),
    ]
    result = splits(assign_splits(rows, FinalTargets(index=2, evaluation=2, reserve=1)))
    # 평가: weak 이상 중 가장 최근 2건 (none인 #10은 건너뜀)
    assert {n for n, r in result.items() if r["final_split"] == "evaluation"} == {9, 8}
    assert result[9]["flags"] == ["weak_evaluation"]
    # 평가 기준일(#8, 1/8) 이후에 종료된 #10은 색인 후보가 될 수 없다
    assert result[10]["final_split"] == "after_cutoff"
    assert result[7]["final_split"] == "unused"
    # 색인: 근거가 강한 순
    assert {n for n, r in result.items() if r["final_split"] == "index"} == {1, 5}
    assert result[4]["final_split"] == "reserve"
    assert result[2]["final_split"] == "unused"


def test_assign_splits_fills_index_with_weak_and_never_indexes_none():
    rows = [row(1, "medium", 1), row(2, "weak", 2), row(3, "none", 3), row(4, "none", 4), row(10, "strong", 10)]
    result = splits(assign_splits(rows, FinalTargets(index=4, evaluation=1, reserve=0)))
    assert result[10]["final_split"] == "evaluation"
    indexed = {n for n, r in result.items() if r["final_split"] == "index"}
    assert indexed == {1, 2}
    assert result[2]["flags"] == ["below_threshold"]
    assert result[3]["final_split"] == "unused"


def test_cli_end_to_end(tmp_path, capsys):
    from scripts.inspect_deep import main

    raw = RawStore(tmp_path / "raw")
    selection, summaries = [], []
    for number in range(1, 7):
        selection.append({"issue_id": f"{REPO}#{number}", "repo": REPO, "number": number, "split": "index",
                          "rank": number, "quality_score": 5, "signal_count": 3,
                          "closed_at": f"2026-01-{number:02d}T00:00:00Z"})
        summaries.append({"issue_id": f"{REPO}#{number}", "repo": REPO,
                          "linked_pull_requests": [{"number": 90 + number, "merged": True}] if number <= 2 else []})
        raw.save(REPO, "issues", str(number), {"user": {"login": "reporter"},
                                               "closed_by": {"login": "core-dev", "type": "User"}})
        raw.save(REPO, "comments", str(number), [])
    raw.save(REPO, "comments", "5", [comment("Fixed by upgrading to 26.0.2", "CONTRIBUTOR", login="core-dev")])
    raw.save(REPO, "comments", "6", [comment("this is resolved now")])
    write_jsonl_atomic(tmp_path / "sel.jsonl", selection)
    write_jsonl_atomic(tmp_path / "deep.jsonl", summaries)
    config = tmp_path / "repos.yaml"
    config.write_text(
        "selection_profiles:\n  tiny: {deep_target_count: 4, index_target_count: 2, evaluation_holdout_count: 1, reserve_count: 1}\n"
        f"repositories:\n  - repo: {REPO}\n    docs_allowlist: [www.keycloak.org]\n", encoding="utf-8")

    code = main(["--selection", str(tmp_path / "sel.jsonl"), "--deep-issues", str(tmp_path / "deep.jsonl"),
                 "--raw-dir", str(tmp_path / "raw"), "--processed-dir", str(tmp_path / "p"),
                 "--manifest-dir", str(tmp_path / "m"), "--config", str(config), "--profile", "tiny"])
    assert code == 0

    final = {json.loads(l)["number"]: json.loads(l)
             for l in (tmp_path / "m" / "final_selection.jsonl").read_text(encoding="utf-8").splitlines()}
    assert final[6]["final_split"] == "evaluation"  # 가장 최근의 weak 이상
    assert final[5]["evidence_level"] == "medium"  # core-dev는 이슈를 3번 이상 닫아 유지보수자로 추정
    assert {n for n, r in final.items() if r["final_split"] == "index"} == {1, 2}
    assert final[5]["final_split"] == "reserve"
    assert "Fixed by upgrading" not in (tmp_path / "m" / "final_selection.jsonl").read_text(encoding="utf-8")
    report = json.loads((tmp_path / "m" / "resolution_report.json").read_text(encoding="utf-8"))
    assert report["repositories"][0]["maintainers_inferred"] == 1
    assert "합계: 색인 2" in capsys.readouterr().out


AIRBYTE_NOTICE = ("At Airbyte, we seek to be clear about the project priorities and roadmap. This issue has not had "
                  "any activity for 365 days, suggesting that it's not as critical as others. It's possible it has "
                  "already been fixed.")


@pytest.mark.parametrize("body", [
    AIRBYTE_NOTICE,
    "This issue has been automatically marked as stale because it has not had recent activity.",
    "Closing due to inactivity. Feel free to reopen.",
    "This issue will be closed in 7 days if no further activity occurs.",
])
def test_automated_notice_is_not_evidence(body):
    from src.preprocessing.resolution import is_automated_notice

    notice = comment(body, "MEMBER", login="triager")
    assert is_automated_notice(notice)
    result = judge_evidence({"repo": REPO}, [notice])
    assert result["evidence_level"] == "none" and result["human_comments"] == 0


def test_real_fix_comment_is_not_a_notice():
    from src.preprocessing.resolution import is_automated_notice

    assert not is_automated_notice(comment("Fixed in chart 2.2.0. The issue was auto-closed when the fix merged."))


def test_inspect_deep_compare_only_does_not_write(tmp_path, capsys):
    from scripts.inspect_deep import main

    raw = RawStore(tmp_path / "raw")
    selection = [{"issue_id": f"{REPO}#{n}", "repo": REPO, "number": n, "quality_score": 5, "signal_count": 3,
                  "closed_at": f"2026-01-{n:02d}T00:00:00Z"} for n in (1, 2)]
    for n in (1, 2):
        raw.save(REPO, "issues", str(n), {"user": {"login": "reporter"}})
    raw.save(REPO, "comments", "1", [comment(AIRBYTE_NOTICE, "MEMBER")])
    raw.save(REPO, "comments", "2", [comment("Fixed in 26.0.2, please upgrade", "MEMBER")])
    write_jsonl_atomic(tmp_path / "sel.jsonl", selection)
    write_jsonl_atomic(tmp_path / "deep.jsonl", [{"issue_id": f"{REPO}#{n}", "repo": REPO} for n in (1, 2)])
    manifest = tmp_path / "m"
    write_jsonl_atomic(manifest / "final_selection.jsonl", [
        {"issue_id": f"{REPO}#1", "final_split": "index", "evidence_level": "weak"},
        {"issue_id": f"{REPO}#2", "final_split": "evaluation", "evidence_level": "medium"}])
    before = (manifest / "final_selection.jsonl").read_text(encoding="utf-8")
    config = tmp_path / "repos.yaml"
    config.write_text("selection_profiles:\n  tiny: {deep_target_count: 2, index_target_count: 1, "
                      f"evaluation_holdout_count: 1, reserve_count: 0}}\nrepositories:\n  - repo: {REPO}\n", encoding="utf-8")
    code = main(["--selection", str(tmp_path / "sel.jsonl"), "--deep-issues", str(tmp_path / "deep.jsonl"),
                 "--raw-dir", str(tmp_path / "raw"), "--processed-dir", str(tmp_path / "p"), "--manifest-dir", str(manifest),
                 "--config", str(config), "--profile", "tiny", "--compare-only"])
    assert code == 0
    out = capsys.readouterr().out
    assert "index: weak → none  1건" in out and "index → unused  1건" in out
    assert (manifest / "final_selection.jsonl").read_text(encoding="utf-8") == before
    assert not (tmp_path / "p").exists()
