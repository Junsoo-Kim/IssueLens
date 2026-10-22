"""얕은 수집 결과 점검: 응답 형식 가정, 데이터 품질, 태깅 규칙 설계용 분포.

    python scripts/inspect_shallow.py
    python scripts/inspect_shallow.py --top-labels 20 --samples 3

이슈 원문은 출력하지 않는다. --samples를 주면 저장소별 제목만 일부 출력한다.
집계 결과는 data/processed/shallow_inspection.json에도 저장한다.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collectors.cache import read_jsonl, write_json_atomic  # noqa: E402
from src.collectors.comment_collector import CODE_BLOCK, ERROR_TEXT, VERSION  # noqa: E402

REQUIRED_FIELDS = ("issue_id", "repo", "number", "title", "body", "labels", "state", "html_url",
                   "created_at", "updated_at", "closed_at")
REPRODUCTION = re.compile(r"steps? to reproduce|how to reproduce|reproduc(e|tion) steps|to reproduce", re.IGNORECASE)
ENVIRONMENT = re.compile(r"\b(version|environment|os|docker|kubernetes|helm|python|java|node)\b\s*[:：]", re.IGNORECASE)
TEMPLATE_HEADING = re.compile(r"^#{2,4}\s+\S", re.MULTILINE)
SHORT_BODY_CHARS = 200


def ratio(count: int, total: int) -> float:
    return round(count / total, 3) if total else 0.0


def quantiles(values: list[int]) -> dict:
    if not values:
        return {}
    if len(values) == 1:
        return {"p25": values[0], "p50": values[0], "p75": values[0], "p90": values[0], "max": values[0]}
    q = statistics.quantiles(values, n=20, method="inclusive")
    return {"p25": round(q[4]), "p50": round(q[9]), "p75": round(q[14]), "p90": round(q[17]), "max": max(values)}


def summarize_repo(records: list[dict], top_labels: int) -> dict:
    total = len(records)
    bodies = [r.get("body") or "" for r in records]
    lengths = [len(b) for b in bodies]
    comments = [r.get("comments") or 0 for r in records]
    labels = Counter(label for r in records for label in r.get("labels", []))
    created = sorted(r["created_at"] for r in records if r.get("created_at"))
    closed = sorted(r["closed_at"] for r in records if r.get("closed_at"))

    has_error = sum(bool(ERROR_TEXT.search(b)) for b in bodies)
    has_code = sum(bool(CODE_BLOCK.search(b)) for b in bodies)
    has_version = sum(bool(VERSION.search(b)) for b in bodies)
    has_repro = sum(bool(REPRODUCTION.search(b)) for b in bodies)
    has_env = sum(bool(ENVIRONMENT.search(b)) for b in bodies)
    # 기획서 5.11절 포함 조건 근사: 오류, 재현, 환경, 해결 이력(댓글 존재) 중 2종 이상
    signal_counts = [
        sum((bool(ERROR_TEXT.search(b) or CODE_BLOCK.search(b)), bool(REPRODUCTION.search(b)),
             bool(ENVIRONMENT.search(b) or VERSION.search(b)), c > 0))
        for b, c in zip(bodies, comments)
    ]

    return {
        "count": total,
        "state": dict(Counter(r.get("state") for r in records)),
        "state_reason": dict(Counter(str(r.get("state_reason")) for r in records)),
        "author_association": dict(Counter(str(r.get("author_association")) for r in records).most_common()),
        "empty_body_ratio": ratio(sum(not b.strip() for b in bodies), total),
        "short_body_ratio": ratio(sum(len(b.strip()) < SHORT_BODY_CHARS for b in bodies), total),
        "body_length": quantiles(lengths),
        "template_body_ratio": ratio(sum(len(TEMPLATE_HEADING.findall(b)) >= 2 for b in bodies), total),
        "comments": {**quantiles(comments), "zero_ratio": ratio(sum(c == 0 for c in comments), total)},
        "signals": {
            "error_text": ratio(has_error, total),
            "code_block": ratio(has_code, total),
            "version": ratio(has_version, total),
            "reproduction": ratio(has_repro, total),
            "environment": ratio(has_env, total),
            "two_or_more_signals": ratio(sum(n >= 2 for n in signal_counts), total),
        },
        "labels": {
            "unlabeled_ratio": ratio(sum(not r.get("labels") for r in records), total),
            "distinct": len(labels),
            "top": labels.most_common(top_labels),
        },
        "created_range": [created[0], created[-1]] if created else None,
        "closed_range": [closed[0], closed[-1]] if closed else None,
    }


def check_integrity(records: list[dict]) -> dict:
    ids = Counter(r.get("issue_id") for r in records)
    missing = Counter(f for r in records for f in REQUIRED_FIELDS if f not in r)
    return {
        "total": len(records),
        "duplicate_issue_ids": [i for i, n in ids.items() if n > 1],
        "pull_request_urls": [r["issue_id"] for r in records if "/pull/" in (r.get("html_url") or "")],
        "not_closed": [r["issue_id"] for r in records if r.get("state") != "closed"],
        "missing_fields": dict(missing),
        "state_reason_missing_ratio": ratio(sum(r.get("state_reason") is None for r in records), len(records)),
    }


def inspect(records: list[dict], top_labels: int = 15) -> dict:
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_repo[record["repo"]].append(record)
    return {
        "integrity": check_integrity(records),
        "repositories": {repo: summarize_repo(items, top_labels) for repo, items in by_repo.items()},
    }


def format_report(report: dict) -> str:
    integrity = report["integrity"]
    lines = [
        "== 무결성",
        f"전체 {integrity['total']}건 / 중복 ID {len(integrity['duplicate_issue_ids'])} / "
        f"PR URL {len(integrity['pull_request_urls'])} / closed 아님 {len(integrity['not_closed'])}",
        f"누락 필드: {integrity['missing_fields'] or '없음'}",
        f"state_reason 없음 비율: {integrity['state_reason_missing_ratio']}",
    ]
    for repo, s in report["repositories"].items():
        sig = s["signals"]
        lines += [
            "",
            f"== {repo} ({s['count']}건)",
            f"state_reason: {s['state_reason']}",
            f"작성자 관계: {s['author_association']}",
            f"본문: 빈 본문 {s['empty_body_ratio']} / {SHORT_BODY_CHARS}자 미만 {s['short_body_ratio']} / "
            f"템플릿형 {s['template_body_ratio']} / 길이 {s['body_length']}",
            f"댓글: {s['comments']}",
            f"신호: 오류 {sig['error_text']} / 코드블록 {sig['code_block']} / 버전 {sig['version']} / "
            f"재현 {sig['reproduction']} / 환경 {sig['environment']} / 2종 이상 {sig['two_or_more_signals']}",
            f"라벨: 없음 {s['labels']['unlabeled_ratio']} / 종류 {s['labels']['distinct']}",
            "  " + ", ".join(f"{name}({n})" for name, n in s["labels"]["top"]),
            f"생성일 {s['created_range']} / 종료일 {s['closed_range']}",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="얕은 수집 결과 점검")
    parser.add_argument("--input", type=Path, default=ROOT / "data/processed/shallow_issues.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "data/processed/shallow_inspection.json")
    parser.add_argument("--top-labels", type=int, default=15)
    parser.add_argument("--samples", type=int, default=0, help="저장소별로 출력할 제목 수")
    args = parser.parse_args(argv)
    # Windows cp949 콘솔에서 라벨과 제목의 이모지 때문에 출력이 중단되지 않게 한다
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not args.input.exists():
        print(f"입력 파일이 없습니다: {args.input}", file=sys.stderr)
        return 2
    records = read_jsonl(args.input)
    report = inspect(records, args.top_labels)
    write_json_atomic(args.output, report)
    print(format_report(report))

    if args.samples:
        print("\n== 제목 샘플")
        by_repo: dict[str, list[dict]] = defaultdict(list)
        for record in records:
            by_repo[record["repo"]].append(record)
        for repo, items in by_repo.items():
            step = max(len(items) // args.samples, 1)
            for record in items[::step][: args.samples]:
                print(f"{repo}#{record['number']} [{', '.join(record['labels'])}] {record['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
