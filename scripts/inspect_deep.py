"""상세 수집 결과 점검, 해결 근거 판정, 근거를 반영한 최종 색인/평가/예비 분리.

    python scripts/inspect_deep.py                         # 최종 규모: mvp (색인 75 / 평가 20 / 예비 5)
    python scripts/inspect_deep.py --min-evidence weak

입력
    data/manifests/deep_selection.jsonl     tag_and_select.py 결과 (상세 수집 후보 풀)
    data/processed/deep_issues.jsonl        collect_issues.py --mode deep 결과
    data/raw/github/.../comments, issues    상세 수집 원본
출력
    data/processed/resolution_evidence.jsonl  이슈별 증거 판정 (원문 없음)
    data/manifests/final_selection.jsonl      최종 색인/평가/예비 (원문 없음)
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
from src.config import load_repositories, load_yaml  # noqa: E402
from src.preprocessing.resolution import (  # noqa: E402
    EVIDENCE_LEVELS,
    FinalTargets,
    assign_splits,
    infer_maintainers,
    judge_evidence,
)

FINAL_SPLITS = ("index", "evaluation", "reserve", "after_cutoff", "unused")
FLAG_NAMES = ("merged_pr", "closed_by_commit", "maintainer_detail", "maintainer_comment",
              "resolution_comment", "official_doc", "connected_without_pr")
SELECTION_FIELDS = ("issue_id", "repo", "number", "issue_type", "component", "category",
                    "quality_score", "signal_count", "state_reason", "closed_at")


def load_raw(raw_store: RawStore, repo: str, kind: str, number: int):
    path = raw_store.path(repo, kind, str(number))
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def ratio(count: int, total: int) -> float:
    return round(count / total, 3) if total else 0.0


def summarize(repo: str, rows: list[dict], maintainers: set[str], targets: FinalTargets) -> dict:
    final = Counter(r["final_split"] for r in rows)
    levels = Counter(r["evidence_level"] for r in rows)
    return {
        "repo": repo,
        "collected": len(rows),
        "maintainers_inferred": len(maintainers),
        "evidence": {lvl: levels[lvl] for lvl in reversed(EVIDENCE_LEVELS)},
        "signal_ratio": {name: ratio(sum(r[name] for r in rows), len(rows)) for name in FLAG_NAMES},
        "bot_only_ratio": ratio(sum(r["human_comments"] == 0 and r["bot_comments"] > 0 for r in rows), len(rows)),
        "final": {split: final[split] for split in FINAL_SPLITS},
        "index_evidence": dict(Counter(r["evidence_level"] for r in rows if r["final_split"] == "index")),
        "evaluation_evidence": dict(Counter(r["evidence_level"] for r in rows if r["final_split"] == "evaluation")),
        "below_threshold_index": sum("below_threshold" in r["flags"] for r in rows),
        "weak_evaluation": sum("weak_evaluation" in r["flags"] for r in rows),
        "shortfall": {
            "index": max(targets.index - final["index"], 0),
            "evaluation": max(targets.evaluation - final["evaluation"], 0),
        },
        "evaluation_closed_from": min((r["closed_at"] for r in rows if r["final_split"] == "evaluation"), default=None),
        "index_closed_to": max((r["closed_at"] for r in rows if r["final_split"] == "index"), default=None),
        "raw_missing": sum("raw_missing" in r["flags"] for r in rows),
    }


def format_report(report: dict) -> str:
    t = report["targets"]
    lines = [
        f"최종 목표: 색인 {t['index']} / 평가 {t['evaluation']} / 예비 {t['reserve']} — "
        f"색인 기준 {report['min_evidence']} 우선, 부족하면 {report['eval_min_evidence']}까지 / "
        f"평가 기준 {report['eval_min_evidence']} 이상",
        "(strong: 병합 PR/커밋 종료, medium: 유지보수자 상세 댓글, weak: 단서만, none: 근거 없음)",
    ]
    for s in report["repositories"]:
        sig = s["signal_ratio"]
        final = s["final"]
        short = s["shortfall"]
        lines += [
            "",
            f"== {s['repo']} (상세 수집 {s['collected']} / 추정 유지보수자 {s['maintainers_inferred']}명)",
            "증거: " + " / ".join(f"{lvl} {n}" for lvl, n in s["evidence"].items()),
            f"신호: 병합 PR {sig['merged_pr']} / 커밋 종료 {sig['closed_by_commit']} / 유지보수자 상세 {sig['maintainer_detail']}"
            f" / 유지보수자 댓글 {sig['maintainer_comment']} / 해결 신호 댓글 {sig['resolution_comment']}"
            f" / 댓글 공식 문서 {sig['official_doc']}",
            f"connected만 있고 PR 번호 없음 {sig['connected_without_pr']} / 봇 댓글만 {s['bot_only_ratio']}",
            f"최종: 색인 {final['index']} {s['index_evidence']} (기준 미달 {s['below_threshold_index']})"
            f" / 평가 {final['evaluation']} {s['evaluation_evidence']} / 예비 {final['reserve']}"
            f" / 평가일 이후 미사용 {final['after_cutoff']} / 미사용 {final['unused']}",
            f"시간 분리: 색인 종료일 <= {s['index_closed_to']} < 평가 종료일 >= {s['evaluation_closed_from']}"
            + (f" / 부족: 색인 {short['index']} 평가 {short['evaluation']}" if short["index"] or short["evaluation"] else "")
            + (f" / 원본 없음 {s['raw_missing']}" if s["raw_missing"] else ""),
        ]
    tot = report["totals"]
    lines += ["", f"합계: 색인 {tot['index']} (기준 미달 {tot['below_threshold_index']}) / 평가 {tot['evaluation']} "
                  f"(기준 미달 {tot['weak_evaluation']}) / 예비 {tot['reserve']} / 목표 미달 저장소 {tot['repos_short']}"]
    return "\n".join(lines)


def compare_with_existing(final_rows: list[dict], existing_path: Path) -> dict:
    """판정 규칙을 바꾼 뒤 기존 최종 분리와 무엇이 달라지는지 센다."""
    existing = {r["issue_id"]: r for r in read_jsonl(existing_path)} if existing_path.exists() else {}
    current = {r["issue_id"]: r for r in final_rows}
    level_changes = Counter()
    for issue_id, old in existing.items():
        new = current.get(issue_id)
        if new and new["evidence_level"] != old["evidence_level"]:
            level_changes[(old["final_split"], old["evidence_level"], new["evidence_level"])] += 1
    kept = {"index", "evaluation", "reserve"}
    split_changes = Counter(
        (existing[i]["final_split"] if i in existing else "-", r["final_split"])
        for i, r in current.items()
        if (existing[i]["final_split"] if i in existing else "-") != r["final_split"]
        and ((i in existing) or r["final_split"] in kept)
    )
    return {"existing": len(existing), "level_changes": level_changes, "split_changes": split_changes}


def format_comparison(diff: dict) -> str:
    lines = [f"기존 final_selection: {diff['existing']}건 (파일은 바꾸지 않음)", "== 증거 수준 변화 (기존 분리, 이전 → 이후)"]
    lines += [f"  {split}: {old} → {new}  {n}건" for (split, old, new), n in sorted(diff["level_changes"].items())] or ["  없음"]
    lines.append("== 분리 변화 (이전 → 이후)")
    lines += [f"  {old} → {new}  {n}건" for (old, new), n in sorted(diff["split_changes"].items())] or ["  없음"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="상세 수집 점검과 해결 근거 기반 최종 분리")
    parser.add_argument("--selection", type=Path, default=ROOT / "data/manifests/deep_selection.jsonl")
    parser.add_argument("--deep-issues", type=Path, default=ROOT / "data/processed/deep_issues.jsonl")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw/github")
    parser.add_argument("--processed-dir", type=Path, default=ROOT / "data/processed")
    parser.add_argument("--manifest-dir", type=Path, default=ROOT / "data/manifests")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/repositories.yaml")
    parser.add_argument("--profile", default="mvp", help="최종 색인/평가/예비 규모 (selection_profiles)")
    parser.add_argument("--min-evidence", choices=EVIDENCE_LEVELS, default="medium", help="색인 우선 기준")
    parser.add_argument("--eval-min-evidence", choices=EVIDENCE_LEVELS, default="weak",
                        help="평가 기준이자 색인 보충 하한")
    parser.add_argument("--compare-only", action="store_true",
                        help="파일을 쓰지 않고 기존 final_selection.jsonl과 증거 수준·분리 차이만 출력")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    for path in (args.selection, args.deep_issues):
        if not path.exists():
            print(f"입력 파일이 없습니다: {path}", file=sys.stderr)
            return 2
    config = load_yaml(args.config)
    profile = config.get("selection_profiles", {}).get(args.profile)
    if profile is None:
        print(f"selection_profiles에 없는 프로필: {args.profile}", file=sys.stderr)
        return 2
    targets = FinalTargets(profile["index_target_count"], profile["evaluation_holdout_count"], profile["reserve_count"])
    allowlists = {r["repo"]: r.get("docs_allowlist", []) for r in load_repositories(args.config)}

    summaries = {r["issue_id"]: r for r in read_jsonl(args.deep_issues)}
    raw_store = RawStore(args.raw_dir)
    loaded: dict[str, list[tuple[dict, dict, list | None, dict | None]]] = defaultdict(list)
    for row in read_jsonl(args.selection):
        issue = load_raw(raw_store, row["repo"], "issues", row["number"])
        comments = load_raw(raw_store, row["repo"], "comments", row["number"])
        loaded[row["repo"]].append((row, summaries.get(row["issue_id"]), comments, issue))

    final_rows, repo_summaries = [], []
    for repo, items in loaded.items():
        maintainers = infer_maintainers(issue for _, _, _, issue in items if issue)
        rows = []
        for row, summary, comments, issue in items:
            flags = [] if summary is not None and comments is not None else ["raw_missing"]
            evidence = judge_evidence(summary or {"repo": repo}, comments or [],
                                      ((issue or {}).get("user") or {}).get("login"), maintainers, allowlists.get(repo, []))
            rows.append({**{k: row.get(k) for k in SELECTION_FIELDS}, **evidence, "flags": flags})
        finalized = assign_splits(rows, targets, args.min_evidence, args.eval_min_evidence)
        final_rows.extend(finalized)
        repo_summaries.append(summarize(repo, finalized, maintainers, targets))

    if args.compare_only:
        print(format_comparison(compare_with_existing(final_rows, args.manifest_dir / "final_selection.jsonl")))
        return 0

    evidence_fields = ("issue_id", "repo", "number", "final_split", "evidence_level", *FLAG_NAMES,
                       "merged_prs", "linked_prs", "human_comments", "bot_comments")
    write_jsonl_atomic(args.processed_dir / "resolution_evidence.jsonl",
                       ({k: r[k] for k in evidence_fields} for r in final_rows))
    write_jsonl_atomic(args.manifest_dir / "final_selection.jsonl",
                       (r for r in final_rows if r["final_split"] in ("index", "evaluation", "reserve")))

    final = Counter(r["final_split"] for r in final_rows)
    report = {
        "generated_at": utc_now(),
        "profile": args.profile,
        "targets": vars(targets),
        "min_evidence": args.min_evidence,
        "eval_min_evidence": args.eval_min_evidence,
        "repositories": repo_summaries,
        "totals": {
            "index": final["index"],
            "evaluation": final["evaluation"],
            "reserve": final["reserve"],
            "below_threshold_index": sum(s["below_threshold_index"] for s in repo_summaries),
            "weak_evaluation": sum(s["weak_evaluation"] for s in repo_summaries),
            "repos_short": sum(1 for s in repo_summaries if s["shortfall"]["index"] or s["shortfall"]["evaluation"]),
        },
    }
    write_json_atomic(args.manifest_dir / "resolution_report.json", report)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
