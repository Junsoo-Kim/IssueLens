"""상세 수집 결과 점검과 해결 근거 재판정, 근거 없는 색인 이슈의 예비 이슈 교체안.

    python scripts/inspect_deep.py                      # 기준: medium 이상
    python scripts/inspect_deep.py --min-evidence weak

입력
    data/manifests/deep_selection.jsonl     tag_and_select.py 결과
    data/processed/deep_issues.jsonl        collect_issues.py --mode deep 결과
    data/raw/github/.../comments, issues    상세 수집 원본
출력
    data/processed/resolution_evidence.jsonl  이슈별 증거 판정 (원문 없음)
    data/manifests/final_selection.jsonl      교체 반영한 최종 색인/평가/예비 (원문 없음)
    data/manifests/resolution_report.json     저장소별 집계
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collectors.cache import RawStore, read_jsonl, utc_now, write_json_atomic, write_jsonl_atomic  # noqa: E402
from src.preprocessing.resolution import EVIDENCE_LEVELS, finalize_split, judge_evidence  # noqa: E402

SPLITS = ("index", "evaluation", "reserve")
FLAG_NAMES = ("merged_pr", "closed_by_commit", "maintainer_detail", "maintainer_comment",
              "resolution_comment", "official_doc")


def load_raw(raw_store: RawStore, repo: str, kind: str, number: int):
    path = raw_store.path(repo, kind, str(number))
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def ratio(count: int, total: int) -> float:
    return round(count / total, 3) if total else 0.0


def summarize(repo: str, rows: list[dict]) -> dict:
    by_split = {split: Counter(r["evidence_level"] for r in rows if r["split"] == split) for split in SPLITS}
    final = Counter(r["final_split"] for r in rows)
    return {
        "repo": repo,
        "evidence_by_split": {split: {lvl: c[lvl] for lvl in reversed(EVIDENCE_LEVELS)} for split, c in by_split.items()},
        "signal_ratio": {name: ratio(sum(r[name] for r in rows), len(rows)) for name in FLAG_NAMES},
        "bot_only_ratio": ratio(sum(r["human_comments"] == 0 and r["bot_comments"] > 0 for r in rows), len(rows)),
        "no_comments_ratio": ratio(sum(r["human_comments"] + r["bot_comments"] == 0 for r in rows), len(rows)),
        "linked_prs_per_issue": round(sum(r["linked_prs"] for r in rows) / len(rows), 2) if rows else 0,
        "replaced": sum(1 for r in rows if r["final_split"] == "dropped"),
        "below_threshold_index": sum("below_threshold" in r["flags"] for r in rows),
        "weak_evaluation": sum("weak_evaluation" in r["flags"] for r in rows),
        "raw_missing": sum("raw_missing" in r["flags"] for r in rows),
        "final": {split: final[split] for split in (*SPLITS, "dropped")},
    }


def format_report(report: dict) -> str:
    lines = [f"기준: {report['min_evidence']} 이상 (strong: 병합 PR/커밋 종료, medium: 유지보수자 상세 댓글, "
             "weak: 단서만, none: 근거 없음)"]
    for s in report["repositories"]:
        ev = s["evidence_by_split"]
        sig = s["signal_ratio"]
        final = s["final"]
        lines += [
            "",
            f"== {s['repo']}",
            *(f"{name}: " + " / ".join(f"{lvl} {n}" for lvl, n in ev[split].items())
              for split, name in (("index", "색인"), ("evaluation", "평가"), ("reserve", "예비"))),
            f"신호: 병합 PR {sig['merged_pr']} / 커밋 종료 {sig['closed_by_commit']} / 유지보수자 상세 {sig['maintainer_detail']}"
            f" / 유지보수자 댓글 {sig['maintainer_comment']} / 해결 신호 댓글 {sig['resolution_comment']}"
            f" / 공식 문서 {sig['official_doc']}",
            f"댓글: 없음 {s['no_comments_ratio']} / 봇 댓글만 {s['bot_only_ratio']} / 이슈당 연결 PR {s['linked_prs_per_issue']}",
            f"교체 {s['replaced']}건 / 기준 미달 색인 잔류 {s['below_threshold_index']} / 기준 미달 평가 {s['weak_evaluation']}"
            + (f" / 원본 없음 {s['raw_missing']}" if s["raw_missing"] else ""),
            f"최종: 색인 {final['index']} / 평가 {final['evaluation']} / 예비 {final['reserve']} / 제외 {final['dropped']}",
        ]
    t = report["totals"]
    lines += ["", f"합계: 색인 {t['index']} (기준 미달 {t['below_threshold_index']}) / 평가 {t['evaluation']} "
                  f"(기준 미달 {t['weak_evaluation']}) / 예비 {t['reserve']} / 교체 {t['replaced']}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="상세 수집 점검과 해결 근거 재판정")
    parser.add_argument("--selection", type=Path, default=ROOT / "data/manifests/deep_selection.jsonl")
    parser.add_argument("--deep-issues", type=Path, default=ROOT / "data/processed/deep_issues.jsonl")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw/github")
    parser.add_argument("--processed-dir", type=Path, default=ROOT / "data/processed")
    parser.add_argument("--manifest-dir", type=Path, default=ROOT / "data/manifests")
    parser.add_argument("--min-evidence", choices=EVIDENCE_LEVELS, default="medium")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    for path in (args.selection, args.deep_issues):
        if not path.exists():
            print(f"입력 파일이 없습니다: {path}", file=sys.stderr)
            return 2

    summaries = {r["issue_id"]: r for r in read_jsonl(args.deep_issues)}
    raw_store = RawStore(args.raw_dir)
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for row in read_jsonl(args.selection):
        summary = summaries.get(row["issue_id"])
        comments = load_raw(raw_store, row["repo"], "comments", row["number"])
        issue = load_raw(raw_store, row["repo"], "issues", row["number"]) or {}
        flags = [] if summary is not None and comments is not None else ["raw_missing"]
        evidence = judge_evidence(summary or {}, comments or [], (issue.get("user") or {}).get("login"))
        by_repo[row["repo"]].append({**row, **evidence, "_flags": flags})

    final_rows, repo_summaries = [], []
    for repo, rows in by_repo.items():
        finalized = finalize_split(rows, args.min_evidence)
        for row in finalized:
            row["flags"] = row.pop("_flags") + row["flags"]
        final_rows.extend(finalized)
        repo_summaries.append(summarize(repo, finalized))

    evidence_fields = ("issue_id", "repo", "number", "split", "evidence_level", *FLAG_NAMES,
                       "merged_prs", "linked_prs", "human_comments", "bot_comments")
    write_jsonl_atomic(args.processed_dir / "resolution_evidence.jsonl",
                       ({k: r[k] for k in evidence_fields} for r in final_rows))
    write_jsonl_atomic(args.manifest_dir / "final_selection.jsonl", final_rows)

    final = Counter(r["final_split"] for r in final_rows)
    report = {
        "generated_at": utc_now(),
        "min_evidence": args.min_evidence,
        "repositories": repo_summaries,
        "totals": {
            "index": final["index"],
            "evaluation": final["evaluation"],
            "reserve": final["reserve"],
            "replaced": final["dropped"],
            "below_threshold_index": sum(s["below_threshold_index"] for s in repo_summaries),
            "weak_evaluation": sum(s["weak_evaluation"] for s in repo_summaries),
        },
    }
    write_json_atomic(args.manifest_dir / "resolution_report.json", report)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
