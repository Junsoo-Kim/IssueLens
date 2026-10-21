"""해결 근거 재판정과 예비 교체 테스트."""
from __future__ import annotations

import json

import pytest

from src.collectors.cache import RawStore, write_jsonl_atomic
from src.preprocessing.resolution import finalize_split, judge_evidence

REPO = "keycloak/keycloak"


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
    ({"reference_urls": {"official_docs": ["https://www.keycloak.org/x"]}}, [], "weak"),
    ({}, [comment("Closing as stale, fixed?", "NONE", "github-actions[bot]", "Bot")], "none"),
    ({}, [], "none"),
])
def test_judge_evidence_levels(summary, comments, expected):
    assert judge_evidence(summary, comments)["evidence_level"] == expected


def test_judge_evidence_counts_bot_comments_separately():
    result = judge_evidence({}, [comment("stale", login="stale[bot]"), comment("ok")])
    assert (result["human_comments"], result["bot_comments"]) == (1, 1)


def row(number, split, level, rank=1):
    return {"issue_id": f"{REPO}#{number}", "repo": REPO, "number": number, "split": split, "rank": rank,
            "evidence_level": level}


def by_id(rows):
    return {r["number"]: r for r in rows}


def test_finalize_replaces_weakest_index_with_strongest_reserve():
    rows = [
        row(1, "index", "strong", 1), row(2, "index", "none", 2), row(3, "index", "weak", 3),
        row(10, "evaluation", "none"),
        row(20, "reserve", "medium", 1), row(21, "reserve", "strong", 2), row(22, "reserve", "none", 3),
    ]
    result = by_id(finalize_split(rows, "medium"))
    assert result[2]["final_split"] == "dropped" and result[2]["replaced_by"] == f"{REPO}#21"
    assert result[3]["final_split"] == "dropped" and result[3]["replaced_by"] == f"{REPO}#20"
    assert result[21]["final_split"] == "index" and result[20]["final_split"] == "index"
    assert result[22]["final_split"] == "reserve"
    # 평가 이슈는 교체하지 않고 표시만 한다
    assert result[10]["final_split"] == "evaluation" and result[10]["flags"] == ["weak_evaluation"]


def test_finalize_flags_index_when_reserves_run_out():
    rows = [row(1, "index", "none", 1), row(2, "index", "weak", 2), row(20, "reserve", "strong")]
    result = by_id(finalize_split(rows, "medium"))
    assert result[1]["final_split"] == "dropped"  # 가장 약한 이슈가 먼저 교체된다
    assert result[2]["final_split"] == "index" and result[2]["flags"] == ["below_threshold"]


def test_finalize_with_weak_threshold_keeps_weak():
    rows = [row(1, "index", "weak"), row(20, "reserve", "strong")]
    result = by_id(finalize_split(rows, "weak"))
    assert result[1]["final_split"] == "index" and result[20]["final_split"] == "reserve"


def test_cli_end_to_end(tmp_path, capsys):
    from scripts.inspect_deep import main

    raw = RawStore(tmp_path / "raw")
    selection = [row(1, "index", None, 1), row(2, "index", None, 2), row(3, "evaluation", None), row(4, "reserve", None)]
    for r in selection:
        r.pop("evidence_level")
    summaries = [
        {"issue_id": f"{REPO}#1", "linked_pull_requests": [{"number": 9, "merged": True}]},
        {"issue_id": f"{REPO}#2"},
        {"issue_id": f"{REPO}#3"},
        {"issue_id": f"{REPO}#4"},
    ]
    for number in (1, 2, 3, 4):
        raw.save(REPO, "issues", str(number), {"user": {"login": "reporter"}})
    raw.save(REPO, "comments", "1", [])
    raw.save(REPO, "comments", "2", [comment("+1")])
    raw.save(REPO, "comments", "3", [])
    raw.save(REPO, "comments", "4", [comment("Fixed by upgrading to 26.0.2", "COLLABORATOR")])
    write_jsonl_atomic(tmp_path / "sel.jsonl", selection)
    write_jsonl_atomic(tmp_path / "deep.jsonl", summaries)

    code = main(["--selection", str(tmp_path / "sel.jsonl"), "--deep-issues", str(tmp_path / "deep.jsonl"),
                 "--raw-dir", str(tmp_path / "raw"), "--processed-dir", str(tmp_path / "p"),
                 "--manifest-dir", str(tmp_path / "m")])
    assert code == 0

    final = {json.loads(l)["number"]: json.loads(l)
             for l in (tmp_path / "m" / "final_selection.jsonl").read_text(encoding="utf-8").splitlines()}
    assert final[2]["final_split"] == "dropped" and final[4]["final_split"] == "index"
    assert final[3]["flags"] == ["weak_evaluation"]
    report = json.loads((tmp_path / "m" / "resolution_report.json").read_text(encoding="utf-8"))
    assert report["totals"] == {"index": 2, "evaluation": 1, "reserve": 0, "replaced": 1,
                                "below_threshold_index": 0, "weak_evaluation": 1}
    assert "Fixed by upgrading" not in (tmp_path / "m" / "final_selection.jsonl").read_text(encoding="utf-8")
    assert "합계: 색인 2" in capsys.readouterr().out


def test_cli_marks_missing_raw(tmp_path):
    from scripts.inspect_deep import main

    write_jsonl_atomic(tmp_path / "sel.jsonl", [{k: v for k, v in row(1, "index", None).items() if k != "evidence_level"}])
    write_jsonl_atomic(tmp_path / "deep.jsonl", [])
    main(["--selection", str(tmp_path / "sel.jsonl"), "--deep-issues", str(tmp_path / "deep.jsonl"),
          "--raw-dir", str(tmp_path / "raw"), "--processed-dir", str(tmp_path / "p"), "--manifest-dir", str(tmp_path / "m")])
    final = json.loads((tmp_path / "m" / "final_selection.jsonl").read_text(encoding="utf-8"))
    assert final["flags"] == ["raw_missing", "below_threshold"]
