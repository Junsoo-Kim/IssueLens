"""태깅과 선별 테스트."""
from __future__ import annotations

import json

import pytest

from src.config import PROJECT_ROOT, load_yaml
from src.preprocessing.selection import SplitTargets, pick_diverse, reject_reasons, select_repository, split_by_time
from src.preprocessing.tagger import (
    TaggingRules,
    classify_categories,
    normalize_label,
    parse_sections,
    tag_issue,
)

CONFIG = load_yaml(PROJECT_ROOT / "configs/repositories.yaml")
REPO = "keycloak/keycloak"

BUG_BODY = """### Describe the bug
After upgrading to 26.0.1 the token validation fails.

### Version
26.0.1

### Steps to reproduce
1. Upgrade
2. Login

### Relevant log output
```
org.keycloak.TokenVerificationException: invalid token
    at org.keycloak.Foo.bar(Foo.java:10)
```

### Anything else?
_No response_
"""


def rules(repo_config=None):
    return TaggingRules.from_config(CONFIG["tagging"], repo_config)


def issue(number=1, title="Token validation fails after upgrade", body=BUG_BODY, labels=("kind/bug",),
          comments=2, state_reason="completed", author="CONTRIBUTOR", closed_at="2026-01-01T00:00:00Z"):
    return {
        "issue_id": f"{REPO}#{number}", "repo": REPO, "number": number, "title": title, "body": body,
        "labels": list(labels), "state": "closed", "state_reason": state_reason, "comments": comments,
        "author_association": author, "closed_at": closed_at,
    }


# ---------------------------------------------------------------- 태깅


def test_normalize_label_strips_emoji_and_case():
    assert normalize_label("🐞 bug") == "bug"
    assert normalize_label("Product Area: Issues") == "product area: issues"
    assert normalize_label("🤔 cant-reproduce") == "cant-reproduce"


def test_parse_sections_treats_no_response_as_empty():
    sections = parse_sections(BUG_BODY)
    assert sections.filled["anything else?"] == ""
    assert sections.has("reproduction") and sections.has("environment") and sections.has("logs")


@pytest.mark.parametrize("labels,title,expected", [
    (["kind/bug"], "Login fails", "bug"),
    (["🐞 bug"], "Something", "bug"),
    (["Bug", "Feature"], "Something", "bug"),
    (["Bug", "Tests"], "Something", "test"),
    (["Bug"], "Flaky test: tests/sentry/test_x.py::test_y", "test"),
    (["kind/cve"], "CVE-2026-1 in netty", "task"),
    (["question"], "How to configure LDAP?", "question"),
    (["not us"], "Flaky test elsewhere", "invalid"),
    ([], "Feature request: add webhook retries", "feature"),
    ([], "Sync crashes with NullPointerException", "bug"),
    ([], "Is there a way to export dashboards?", "question"),
])
def test_issue_type_classification(labels, title, expected):
    tag = tag_issue(issue(title=title, labels=labels, body="plain text " * 20), rules())
    assert tag["issue_type"] == expected


def test_unlabeled_issue_with_reproduction_is_bug():
    tag = tag_issue(issue(title="Upgrade notes", labels=[], body=BUG_BODY), rules())
    assert (tag["issue_type"], tag["type_source"]) == ("bug", "body")


def test_tag_extracts_signals_versions_and_errors():
    tag = tag_issue(issue(), rules(), {"categories": ["auth", "deployment_upgrade"]})
    assert tag["error_types"] == ["TokenVerificationException"]
    assert tag["versions"] == ["26.0.1"]
    assert tag["has_error_log"] and tag["has_reproduction"] and tag["has_environment"] and tag["has_resolution_signal"]
    assert tag["signal_count"] == 4
    assert tag["category"] in ("auth", "deployment_upgrade")
    assert "java" in tag["languages"]
    assert "body" not in tag and "title" not in tag  # 원문을 태깅 결과에 남기지 않는다


def test_quality_score_penalizes_stale_not_planned():
    good = tag_issue(issue(), rules())
    stale = tag_issue(issue(labels=["kind/bug", "stale"], state_reason="not_planned"), rules())
    assert good["quality_score"] - stale["quality_score"] == 5  # completed +1, not_planned -2, stale -2


def test_components_from_labels_repo_patterns_and_title_prefix():
    repo_rules = rules({"component_patterns": ["^(ingestion|platform)$"]})
    tag = tag_issue(issue(title="[source-postgres] sync fails", labels=["bug", "ingestion", "area/oidc"]), repo_rules)
    assert tag["components"] == ["ingestion", "area/oidc", "title:source-postgres"]
    assert tag["component"] == "ingestion"
    version_prefix = tag_issue(issue(title="[3.32.0] UI bug", labels=["bug"]), rules())
    assert version_prefix["component"] == "general"


def test_categories_fall_back_to_repo_default():
    assert classify_categories("Something odd", "", [], ["async_event", "auth"]) == ["async_event"]
    assert classify_categories("Kafka consumer lag", "", [], ["auth"])[0] == "async_event"


# ---------------------------------------------------------------- 선별


def tag(number, score=5, signals=3, component="general", issue_type="bug", closed="2026-01-01", **extra):
    return {
        "issue_id": f"{REPO}#{number}", "repo": REPO, "number": number, "issue_type": issue_type,
        "component": component, "category": "auth", "quality_score": score, "signal_count": signals,
        "content_length": 500, "comments": 1, "stale": False, "state_reason": "completed",
        "closed_at": f"{closed}T00:00:00Z", **extra,
    }


def test_split_targets_validation_and_scaling():
    with pytest.raises(ValueError):
        SplitTargets(100, 70, 20, 5)
    scaled = SplitTargets(100, 75, 20, 5).scaled(60)
    assert (scaled.deep, scaled.index, scaled.evaluation, scaled.reserve) == (60, 45, 12, 3)


@pytest.mark.parametrize("overrides,allow_questions,expected", [
    ({}, False, []),
    ({"issue_type": "feature"}, False, ["type:feature"]),
    ({"issue_type": "question"}, False, ["type:question"]),
    ({"issue_type": "question"}, True, []),
    ({"issue_type": "question", "comments": 0}, True, ["type:question"]),
    ({"state_reason": "duplicate"}, False, ["duplicate"]),
    ({"stale": True, "state_reason": "not_planned"}, False, ["auto_closed"]),
    ({"stale": True}, False, []),
    ({"content_length": 50, "signal_count": 1}, False, ["body_too_short", "insufficient_signals"]),
])
def test_reject_reasons(overrides, allow_questions, expected):
    assert reject_reasons(tag(1, **overrides), allow_questions) == expected


def test_pick_diverse_caps_single_component():
    candidates = [tag(i, score=9, component="ui") for i in range(10)] + [
        tag(100 + i, score=5, component=f"c{i}") for i in range(10)]
    picked = pick_diverse(candidates, 10, max_share=0.4)
    assert sum(t["component"] == "ui" for t in picked) == 4
    # 다른 컴포넌트가 모자라면 상한을 넘어서라도 채운다
    assert len(pick_diverse(candidates[:10], 10)) == 10
    # general(컴포넌트 불명)은 상한 없이 점수순으로 뽑힌다
    unknown = [tag(i, score=9) for i in range(10)] + [tag(100 + i, score=5, component=f"c{i}") for i in range(10)]
    assert sum(t["component"] == "general" for t in pick_diverse(unknown, 10)) == 10


def test_split_by_time_puts_newest_in_evaluation():
    selected = [tag(i, score=10 - i, closed=f"2026-01-{i + 10:02d}") for i in range(10)]
    splits = split_by_time(selected, SplitTargets(10, 6, 3, 1))
    assert [t["number"] for t in splits["evaluation"]] == [9, 8, 7]
    assert max(t["closed_at"] for t in splits["index"]) < min(t["closed_at"] for t in splits["evaluation"])
    assert [t["number"] for t in splits["reserve"]] == [6]  # 남은 것 중 점수가 가장 낮은 이슈


def test_select_repository_reports_rejections_and_shortfall():
    tags = [tag(i) for i in range(8)] + [tag(50, issue_type="test"), tag(51, signals=1)]
    result = select_repository(tags, SplitTargets(20, 15, 4, 1))
    assert len(result["eligible"]) == 8 and len(result["rejected"]) == 2
    assert result["targets"].deep == 8
    assert sum(len(v) for v in result["splits"].values()) == 8
    selected_ids = {t["issue_id"] for t in result["selected"]}
    assert f"{REPO}#50" not in selected_ids


# ---------------------------------------------------------------- CLI


def test_cli_writes_selection_without_issue_text(tmp_path, capsys):
    from scripts.tag_and_select import main

    rows = [issue(number=i, closed_at=f"2026-01-{i:02d}T00:00:00Z") for i in range(1, 21)]
    rows.append(issue(number=99, title="Flaky test: something", labels=["Tests"]))
    source = tmp_path / "shallow.jsonl"
    source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    code = main(["--input", str(source), "--processed-dir", str(tmp_path / "p"), "--manifest-dir", str(tmp_path / "m"),
                 "--deep-target-count", "10", "--index-target-count", "6", "--evaluation-count", "3", "--reserve-count", "1"])
    assert code == 0

    selection = [json.loads(l) for l in (tmp_path / "m" / "deep_selection.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(selection) == 10
    assert {s["split"] for s in selection} == {"index", "evaluation", "reserve"}
    rejected = (tmp_path / "p" / "rejected_issues.jsonl").read_text(encoding="utf-8")
    assert "type:test" in rejected
    for path in [tmp_path / "m" / "deep_selection.jsonl", tmp_path / "p" / "tagged_issues.jsonl",
                 tmp_path / "m" / "selection_report.json"]:
        assert "token validation fails" not in path.read_text(encoding="utf-8").lower()
    assert "합계: 상세 수집 10" in capsys.readouterr().out

    # deep 수집 입력으로 그대로 읽힌다
    from src.collectors.runner import load_selection
    assert len(load_selection(tmp_path / "m" / "deep_selection.jsonl")) == 10

    assert main(["--input", str(source), "--profile", "nope"]) == 2
