"""얕은 수집 결과 태깅과 상세 수집/색인/평가/예비 분리.

    python scripts/tag_and_select.py                    # MVP: 저장소별 100건 (75/20/5)
    python scripts/tag_and_select.py --profile full     # 저장소별 200건 (150/30/20)
    python scripts/tag_and_select.py --deep-target-count 200 --index-target-count 150 \\
        --evaluation-count 30 --reserve-count 20

출력
    data/processed/tagged_issues.jsonl     전체 태깅 결과 (원문 없음)
    data/processed/rejected_issues.jsonl   제외 이슈와 reject_reasons
    data/manifests/deep_selection.jsonl    상세 수집 대상과 split (collect_issues.py --mode deep 입력)
    data/manifests/selection_report.json   저장소별 분포와 목표 달성 여부
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collectors.cache import read_jsonl, utc_now, write_json_atomic, write_jsonl_atomic  # noqa: E402
from src.config import load_repositories, load_yaml  # noqa: E402
from src.preprocessing.selection import SplitTargets, select_repository  # noqa: E402
from src.preprocessing.tagger import TaggingRules, tag_issue  # noqa: E402

SELECTION_FIELDS = ("issue_id", "repo", "number", "issue_type", "component", "category",
                    "quality_score", "signal_count", "state_reason", "closed_at")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="얕은 수집 결과 태깅과 상세 수집 대상 선별")
    parser.add_argument("--input", type=Path, default=ROOT / "data/processed/shallow_issues.jsonl")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/repositories.yaml")
    parser.add_argument("--processed-dir", type=Path, default=ROOT / "data/processed")
    parser.add_argument("--manifest-dir", type=Path, default=ROOT / "data/manifests")
    parser.add_argument("--group-by", choices=["repo"], default="repo")
    parser.add_argument("--profile", default="mvp", help="repositories.yaml의 selection_profiles 이름")
    parser.add_argument("--deep-target-count", type=int)
    parser.add_argument("--index-target-count", type=int)
    parser.add_argument("--evaluation-count", type=int)
    parser.add_argument("--reserve-count", type=int)
    return parser.parse_args(argv)


def resolve_targets(args: argparse.Namespace, config: dict) -> SplitTargets:
    profiles = config.get("selection_profiles", {})
    if args.profile not in profiles:
        raise ValueError(f"selection_profiles에 없는 프로필: {args.profile}")
    profile = profiles[args.profile]
    return SplitTargets(
        deep=args.deep_target_count or profile["deep_target_count"],
        index=args.index_target_count or profile["index_target_count"],
        evaluation=args.evaluation_count or profile["evaluation_holdout_count"],
        reserve=args.reserve_count or profile["reserve_count"],
    )


def summarize(repo: str, tags: list[dict], result: dict, requested: SplitTargets) -> dict:
    selected = result["selected"]
    components = Counter(t["component"] for t in selected)
    return {
        "repo": repo,
        "total": len(tags),
        "issue_types": dict(Counter(t["issue_type"] for t in tags).most_common()),
        "reject_reasons": dict(Counter(r for t in result["rejected"] for r in t["reject_reasons"]).most_common()),
        "eligible": len(result["eligible"]),
        "eligible_types": dict(Counter(t["issue_type"] for t in result["eligible"]).most_common()),
        "selected": {name: len(items) for name, items in result["splits"].items()},
        "selected_types": dict(Counter(t["issue_type"] for t in selected).most_common()),
        "shortfall": max(requested.deep - len(selected), 0),
        "selected_categories": dict(Counter(t["category"] for t in selected).most_common()),
        "top_components": components.most_common(5),
        "quality_score_min": min((t["quality_score"] for t in selected), default=None),
        "evaluation_closed_from": min((t["closed_at"] for t in result["splits"]["evaluation"]), default=None),
        "index_closed_to": max((t["closed_at"] for t in result["splits"]["index"]), default=None),
    }


def format_report(report: dict) -> str:
    t = report["targets"]
    lines = [f"프로필 {report['profile']}: 상세 {t['deep']} = 색인 {t['index']} + 평가 {t['evaluation']} + 예비 {t['reserve']}"]
    for s in report["repositories"]:
        sel = s["selected"]
        lines += [
            "",
            f"== {s['repo']} (전체 {s['total']} / 적격 {s['eligible']} / 선별 {sum(sel.values())}"
            + (f" / 부족 {s['shortfall']}" if s["shortfall"] else "") + ")",
            f"유형: {s['issue_types']}",
            f"제외 사유: {s['reject_reasons']}",
            f"적격 유형: {s['eligible_types']} / 선별 유형: {s['selected_types']}",
            f"분리: 색인 {sel['index']} / 평가 {sel['evaluation']} / 예비 {sel['reserve']}"
            f" (평가 종료일 >= {s['evaluation_closed_from']}, 색인 종료일 <= {s['index_closed_to']})",
            f"범주: {s['selected_categories']}",
            f"상위 컴포넌트: {s['top_components']} / 최저 점수 {s['quality_score_min']}",
        ]
    totals = report["totals"]
    lines += ["", f"합계: 상세 수집 {totals['deep']} / 색인 {totals['index']} / 평가 {totals['evaluation']} / "
                  f"예비 {totals['reserve']} / 목표 미달 저장소 {totals['repos_below_target']}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if not args.input.exists():
        print(f"입력 파일이 없습니다: {args.input}", file=sys.stderr)
        return 2

    config = load_yaml(args.config)
    try:
        targets = resolve_targets(args, config)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    repo_configs = {r["repo"]: r for r in load_repositories(args.config)}

    by_repo: dict[str, list[dict]] = defaultdict(list)
    for issue in read_jsonl(args.input):
        by_repo[issue["repo"]].append(issue)

    all_tags, rejected, selection, summaries = [], [], [], []
    for repo, issues in by_repo.items():
        repo_config = repo_configs.get(repo, {})
        rules = TaggingRules.from_config(config.get("tagging", {}), repo_config)
        tags = [tag_issue(issue, rules, repo_config) for issue in issues]
        result = select_repository(tags, targets, allow_questions=repo_config.get("allow_questions", False))

        all_tags.extend(tags)
        rejected.extend({k: t[k] for k in ("issue_id", "repo", "number", "issue_type", "quality_score", "reject_reasons")}
                        for t in result["rejected"])
        for split, items in result["splits"].items():
            for rank, tag in enumerate(items, start=1):
                selection.append({**{k: tag[k] for k in SELECTION_FIELDS}, "split": split, "rank": rank})
        summaries.append(summarize(repo, tags, result, targets))

    write_jsonl_atomic(args.processed_dir / "tagged_issues.jsonl", all_tags)
    write_jsonl_atomic(args.processed_dir / "rejected_issues.jsonl", rejected)
    write_jsonl_atomic(args.manifest_dir / "deep_selection.jsonl", selection)

    split_counts = Counter(item["split"] for item in selection)
    report = {
        "generated_at": utc_now(),
        "profile": args.profile,
        "targets": vars(targets),
        "repositories": summaries,
        "totals": {
            "deep": len(selection),
            "index": split_counts["index"],
            "evaluation": split_counts["evaluation"],
            "reserve": split_counts["reserve"],
            "repos_below_target": sum(1 for s in summaries if s["shortfall"]),
        },
    }
    write_json_atomic(args.manifest_dir / "selection_report.json", report)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
