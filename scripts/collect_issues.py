"""GitHub 이슈 수집 CLI (--mode shallow|deep).

    python scripts/collect_issues.py --mode shallow
    python scripts/collect_issues.py --mode shallow --repo keycloak/keycloak --dry-run
    python scripts/collect_issues.py --mode deep --selection-file data/manifests/deep_selection.jsonl \\
        --include-comments --include-timeline --include-linked-prs
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.collectors.cache import Manifest, RawStore, ResponseCache  # noqa: E402
from src.collectors.github_client import (  # noqa: E402
    DEFAULT_API_VERSION,
    DEFAULT_USER_AGENT,
    GitHubAPIError,
    GitHubClient,
    HttpSettings,
)
from src.collectors.issue_collector import DEFAULT_QUERY  # noqa: E402
from src.collectors.runner import (  # noqa: E402
    DEFAULT_MAX_LINKED_PRS,
    dry_run,
    load_selection,
    run_deep,
    run_shallow,
)
from src.config import load_repositories, load_yaml  # noqa: E402

EXIT_OK, EXIT_FAILURES, EXIT_ABORTED = 0, 1, 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GitHub 종료 이슈 수집기")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/repositories.yaml")
    parser.add_argument("--app-config", type=Path, default=ROOT / "configs/app.yaml")
    parser.add_argument("--mode", choices=["shallow", "deep"], required=True)
    parser.add_argument("--repo", action="append", help="특정 저장소만 수집 (여러 번 지정 가능)")
    parser.add_argument("--query", help="기본값: repositories.yaml의 primary_query")
    parser.add_argument("--candidate-limit-per-repo", type=int, help="저장소별 얕은 수집 후보 상한")
    parser.add_argument("--selection-file", type=Path, help="상세 수집 대상 JSONL (deep 모드 필수)")
    parser.add_argument("--include-comments", action="store_true")
    parser.add_argument("--include-timeline", action="store_true")
    parser.add_argument("--include-linked-prs", action="store_true")
    parser.add_argument("--max-linked-prs", type=int, help=f"이슈당 연결 PR 수집 상한 (기본 {DEFAULT_MAX_LINKED_PRS})")
    parser.add_argument("--refresh", action="store_true", help="ETag 캐시를 무시하고 갱신")
    parser.add_argument("--resume", action="store_true", help="완료된 항목은 건너뛰고 재개")
    parser.add_argument("--dry-run", action="store_true", help="검색 건수와 Rate Limit만 확인")
    parser.add_argument("--max-pages", type=int, help="API 페이지 상한")
    parser.add_argument("--since", help="갱신 수집 기준 시각 (YYYY-MM-DD, shallow 모드)")
    args = parser.parse_args(argv)
    if args.mode == "deep" and not args.dry_run and not args.selection_file:
        parser.error("--mode deep에는 --selection-file이 필요합니다")
    return args


def build_client(app_config: dict, cache_dir: Path, refresh: bool) -> GitHubClient:
    return GitHubClient(
        token=os.getenv("GITHUB_TOKEN") or None,
        api_version=os.getenv("GITHUB_API_VERSION") or DEFAULT_API_VERSION,
        user_agent=os.getenv("GITHUB_USER_AGENT") or DEFAULT_USER_AGENT,
        settings=HttpSettings.from_dict(app_config.get("http")),
        cache=ResponseCache(cache_dir),
        refresh=refresh,
    )


def format_summary(stats: dict) -> str:
    lines = []
    if stats["mode"] == "dry-run":
        for repo in stats["repositories"]:
            lines.append(f"{repo['repo']}: 종료 이슈 {repo['total_closed']} / 후보 상한 {repo['candidate_limit']}")
        for name, info in stats["rate_limit"].items():
            lines.append(f"Rate Limit {name}: {info.get('remaining')}/{info.get('limit')}")
        return "\n".join(lines)

    if stats["mode"] == "shallow":
        for repo in stats["repositories"]:
            lines.append(f"  {repo['repo']}: {repo['status']} {repo['collected']}건")
        lines.append(f"얕은 수집 후보: {stats['totals']['shallow_candidates']}")
        lines.append(f"저장소별 목표 미달: {len(stats['totals']['below_target'])}")
    else:
        issues = stats["issues"]
        lines.append(f"깊은 수집 대상: {issues['selected']}")
        lines.append(f"수집 완료: {issues['collected']} / 건너뜀: {issues['skipped']} / 실패: {issues['failed']}")

    client = stats["client"]
    lines.append(f"API 요청: {client['requests']}")
    lines.append(f"재시도: {client['retries']}")
    lines.append(f"캐시 적중: {client['cache_hits']}")
    remaining = ", ".join(f"{k} {v}" for k, v in client["rate_limit_remaining"].items()) or "-"
    lines.append(f"Rate Limit 잔여량: {remaining}")
    for failure in stats["failures"]:
        lines.append(f"실패: {failure['key']} {failure.get('http_status') or ''} {failure.get('reason', '')}".rstrip())
    if stats["aborted"]:
        lines.append("일시적 오류로 수집을 중단했습니다. --resume으로 재개하세요.")
    return "\n".join(lines)


def save_stats(path: Path, stats: dict) -> None:
    data = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    data[stats["mode"]] = stats
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(ROOT / ".env")
    app_config = load_yaml(args.app_config)
    paths = {name: ROOT / value for name, value in app_config.get("paths", {}).items()}
    repo_config = load_yaml(args.config)
    try:
        repositories = load_repositories(args.config, only=args.repo)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ABORTED
    query = args.query or repo_config.get("defaults", {}).get("primary_query", DEFAULT_QUERY)

    client = build_client(app_config, paths["cache_dir"], args.refresh)
    raw_store = RawStore(paths["raw_dir"])

    try:
        if args.dry_run:
            print(format_summary(dry_run(client, repositories, query=query, since=args.since)))
            return EXIT_OK

        if args.mode == "shallow":
            stats = run_shallow(
                client, repositories, raw_store,
                Manifest(paths["manifest_dir"] / "shallow_manifest.jsonl"),
                paths["processed_dir"],
                query=query,
                candidate_limit=args.candidate_limit_per_repo,
                max_pages=args.max_pages,
                since=args.since,
                resume=args.resume,
            )
        else:
            selection = load_selection(args.selection_file)
            if args.repo:
                wanted = {r["repo"].lower() for r in repositories}
                selection = [(repo, n) for repo, n in selection if repo.lower() in wanted]
            collection = app_config.get("collection", {})
            stats = run_deep(
                client, selection, raw_store,
                Manifest(paths["manifest_dir"] / "deep_manifest.jsonl"),
                paths["processed_dir"],
                docs_allowlists={r["repo"]: r.get("docs_allowlist", []) for r in load_repositories(args.config)},
                include_comments=args.include_comments,
                include_timeline=args.include_timeline,
                include_linked_prs=args.include_linked_prs,
                max_linked_prs=args.max_linked_prs or collection.get("max_linked_prs", DEFAULT_MAX_LINKED_PRS),
                resume=args.resume,
            )
    except GitHubAPIError as exc:
        print(f"GitHub API 오류: {exc}", file=sys.stderr)
        return EXIT_ABORTED

    save_stats(paths["processed_dir"] / "collection_stats.json", stats)
    print(format_summary(stats))
    if stats["aborted"]:
        return EXIT_ABORTED
    return EXIT_FAILURES if stats["failures"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
