"""범위 확정 점검 (기획서 5.4절 2단계): 범주·저장소·유형·근거 분포와 확정 기준 충족 여부.

    python scripts/analyze_scope.py

입력: data/manifests/final_selection.jsonl, configs/repositories.yaml의 scope,
      data/processed/tagged_issues.jsonl(있으면 범주 목록으로 대표 범주를 다시 계산)
출력: data/manifests/scope_report.json (원문 없음)
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collectors.cache import read_jsonl, utc_now, write_json_atomic  # noqa: E402
from src.config import load_yaml  # noqa: E402
from src.preprocessing.tagger import primary_category  # noqa: E402


def analyze(rows: list[dict], tagged: dict[str, dict], config: dict) -> dict:
    scope = config.get("scope", {})
    criteria = scope.get("criteria", {})
    in_scope = scope.get("categories", [])
    tag_only = set(config.get("tagging", {}).get("tag_only_categories", []))
    fallbacks = {r["repo"]: r.get("categories", []) for r in config.get("repositories", [])}

    for row in rows:
        tag = tagged.get(row["issue_id"])
        categories = tag["categories"] if tag else [row["category"]]
        row["scope_category"] = primary_category(categories, fallbacks.get(row["repo"], []), tag_only)
        row["tag_only"] = sorted(set(categories) & tag_only)

    index = [r for r in rows if r["final_split"] == "index"]
    evaluation = [r for r in rows if r["final_split"] == "evaluation"]
    by_category = {
        c: {"index": sum(r["scope_category"] == c for r in index), "evaluation": sum(r["scope_category"] == c for r in evaluation)}
        for c in in_scope
    }
    repo_category = defaultdict(Counter)
    for r in index:
        repo_category[r["repo"]][r["scope_category"]] += 1
    strong_or_medium = sum(r["evidence_level"] in ("strong", "medium") for r in index)
    max_share = max((v["index"] for v in by_category.values()), default=0) / len(index) if index else 0

    checks = {
        "evaluation_per_category": {
            "required": criteria.get("min_evaluation_per_category", 10),
            "actual": {c: v["evaluation"] for c, v in by_category.items()},
        },
        "max_category_share": {"required": criteria.get("max_category_share", 0.4), "actual": round(max_share, 3)},
        "strong_or_medium_ratio": {
            "required": criteria.get("min_strong_or_medium_ratio", 0.8),
            "actual": round(strong_or_medium / len(index), 3) if index else 0,
        },
        "out_of_scope_categories": sorted({r["scope_category"] for r in index + evaluation} - set(in_scope)),
    }
    checks["evaluation_per_category"]["passed"] = all(
        n >= checks["evaluation_per_category"]["required"] for n in checks["evaluation_per_category"]["actual"].values())
    checks["max_category_share"]["passed"] = max_share <= checks["max_category_share"]["required"]
    checks["strong_or_medium_ratio"]["passed"] = checks["strong_or_medium_ratio"]["actual"] >= checks["strong_or_medium_ratio"]["required"]

    return {
        "generated_at": utc_now(),
        "scope": scope.get("name"),
        "categories": in_scope,
        "tag_only_categories": sorted(tag_only),
        "category_source": "tagged_issues" if tagged else "final_selection (근사치)",
        "totals": {"index": len(index), "evaluation": len(evaluation)},
        "by_category": by_category,
        "repo_category_index": {repo: dict(c) for repo, c in sorted(repo_category.items())},
        "issue_types_index": dict(Counter(r["issue_type"] for r in index)),
        "evidence_by_category_index": {
            c: dict(Counter(r["evidence_level"] for r in index if r["scope_category"] == c)) for c in in_scope},
        "tag_only_counts": {t: {"index": sum(t in r["tag_only"] for r in index),
                                "evaluation": sum(t in r["tag_only"] for r in evaluation)} for t in sorted(tag_only)},
        "checks": checks,
        "passed": all(v.get("passed", True) for v in checks.values() if isinstance(v, dict)) and not checks["out_of_scope_categories"],
    }


def format_report(report: dict) -> str:
    lines = [f"범위: {report['scope']}", f"범주 출처: {report['category_source']}", "", "== 범주별 (색인 / 평가)"]
    for c, v in report["by_category"].items():
        lines.append(f"  {c:20s} {v['index']:4d} / {v['evaluation']:3d}   근거 {report['evidence_by_category_index'][c]}")
    for t, v in report["tag_only_counts"].items():
        lines.append(f"  (부가 태그) {t}: 색인 {v['index']} / 평가 {v['evaluation']}")
    lines += ["", "== 저장소 x 범주 (색인)"]
    for repo, counts in report["repo_category_index"].items():
        lines.append(f"  {repo:28s} {counts}")
    lines += ["", f"이슈 유형 (색인): {report['issue_types_index']}", "", "== 확정 기준"]
    checks = report["checks"]
    for name in ("evaluation_per_category", "max_category_share", "strong_or_medium_ratio"):
        c = checks[name]
        lines.append(f"  {'PASS' if c['passed'] else 'FAIL'} {name}: 기준 {c['required']} / 실제 {c['actual']}")
    if checks["out_of_scope_categories"]:
        lines.append(f"  FAIL 범위 밖 대표 범주: {checks['out_of_scope_categories']}")
    lines.append(f"\n결과: {'PASS' if report['passed'] else 'FAIL'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="범위 확정 점검")
    parser.add_argument("--selection", type=Path, default=ROOT / "data/manifests/final_selection.jsonl")
    parser.add_argument("--tagged", type=Path, default=ROOT / "data/processed/tagged_issues.jsonl")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/repositories.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "data/manifests/scope_report.json")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if not args.selection.exists():
        print(f"입력 파일이 없습니다: {args.selection}", file=sys.stderr)
        return 2
    tagged = {t["issue_id"]: t for t in read_jsonl(args.tagged)} if args.tagged.exists() else {}
    report = analyze(read_jsonl(args.selection), tagged, load_yaml(args.config))
    write_json_atomic(args.output, report)
    print(format_report(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
