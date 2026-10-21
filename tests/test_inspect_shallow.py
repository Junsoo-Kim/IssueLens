"""얕은 수집 점검 스크립트 테스트."""
from __future__ import annotations

import json

from scripts.inspect_shallow import inspect, main


def record(repo, number, body="", labels=(), comments=0, state_reason="completed", url=None):
    return {
        "issue_id": f"{repo}#{number}", "repo": repo, "number": number, "title": f"t{number}", "body": body,
        "labels": list(labels), "state": "closed", "state_reason": state_reason, "comments": comments,
        "author_association": "NONE", "created_at": f"2026-01-0{number}T00:00:00Z",
        "updated_at": "2026-02-01T00:00:00Z", "closed_at": f"2026-02-0{number}T00:00:00Z",
        "html_url": url or f"https://github.com/{repo}/issues/{number}",
    }


def test_inspect_detects_integrity_problems():
    records = [
        record("a/b", 1),
        record("a/b", 1),
        record("a/b", 2, url="https://github.com/a/b/pull/2", state_reason=None),
    ]
    integrity = inspect(records)["integrity"]
    assert integrity["duplicate_issue_ids"] == ["a/b#1"]
    assert integrity["pull_request_urls"] == ["a/b#2"]
    assert integrity["state_reason_missing_ratio"] == 0.333


def test_inspect_summarizes_signals_and_labels():
    body = "### Steps to reproduce\n1. run\n### Version: 26.0.1\n```\nNullPointerException\n```"
    records = [record("a/b", 1, body=body, labels=["bug"], comments=3), record("a/b", 2, labels=["bug", "ui"])]
    summary = inspect(records)["repositories"]["a/b"]
    assert summary["empty_body_ratio"] == 0.5
    assert summary["signals"]["reproduction"] == 0.5
    assert summary["signals"]["error_text"] == 0.5
    assert summary["signals"]["two_or_more_signals"] == 0.5
    assert summary["labels"]["top"][0] == ("bug", 2)
    assert summary["comments"]["zero_ratio"] == 0.5


def test_main_writes_report_without_issue_text(tmp_path, capsys):
    source = tmp_path / "shallow.jsonl"
    source.write_text(json.dumps(record("a/b", 1, body="secret body text")) + "\n", encoding="utf-8")
    output = tmp_path / "report.json"
    assert main(["--input", str(source), "--output", str(output)]) == 0
    assert "secret body text" not in output.read_text(encoding="utf-8")
    assert "secret body text" not in capsys.readouterr().out
    assert main(["--input", str(tmp_path / "missing.jsonl")]) == 2
