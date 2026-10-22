"""에이전트 코퍼스 목록 확인 (기획서 17.12절 단계 C). 로컬에서 GitHub·PyPI API로 실행한다.

    python scripts/check_agent_corpus.py

저장소마다 다음을 확인하고 data/manifests/agent_corpus_report.json(원문 없음)에 저장한다.
- 저장소가 있는지, 이름이 바뀌었는지(API의 full_name), 보관(archived) 상태인지
- 종료 이슈 수, 최근 1년 종료 이슈 수 (수집 가능한 규모)
- Discussions 사용 여부
- 목록의 PyPI 패키지가 실제로 이 저장소를 가리키는지 (project_urls)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from agent.registry import PYPI_URL, default_fetch, github_repo_from_urls, load_corpus  # noqa: E402
from src.collectors.cache import utc_now, write_json_atomic  # noqa: E402
from src.collectors.github_client import (DEFAULT_API_VERSION, DEFAULT_USER_AGENT, GitHubAPIError,  # noqa: E402
                                          GitHubClient, HttpSettings)
from src.config import load_yaml  # noqa: E402

REPORT = ROOT / "data/manifests/agent_corpus_report.json"
# 코퍼스 규모 기준. 해결된 사용법·버그 이력은 오래돼도 쓸모가 있어 전체 규모를 먼저 보고,
# 최근 활동은 유지보수가 사실상 멈췄는지만 확인한다
MIN_CLOSED = 200
MIN_RECENT_CLOSED = 20


def renamed_to(client: GitHubClient, repo: str) -> str | None:
    """이름이 바뀐 저장소는 GitHub API가 새 저장소를 돌려준다. 없으면 None."""
    try:
        return (client.get(f"/repos/{repo}", use_cache=False).data or {}).get("full_name")
    except GitHubAPIError:
        return None


def check_library(client: GitHubClient, fetch, library, since: str) -> dict:
    row = {"repo": library.repo, "category": library.category, "packages": {}, "problems": []}
    try:
        info = client.get(f"/repos/{library.repo}", use_cache=False).data or {}
    except GitHubAPIError as exc:
        row["problems"].append(f"저장소 조회 실패: {exc.status} {exc.reason}")
        return row
    row.update(full_name=info.get("full_name"), archived=info.get("archived"), has_discussions=info.get("has_discussions"),
               has_issues=info.get("has_issues"), license=(info.get("license") or {}).get("spdx_id"))
    if row["full_name"] and row["full_name"].lower() != library.repo.lower():
        row["problems"].append(f"이름이 바뀜: {row['full_name']}")
    if row["archived"]:
        row["problems"].append("보관(archived)된 저장소")
    for label, query in (("closed_issues", f"repo:{library.repo} is:issue is:closed"),
                         ("closed_issues_last_year", f"repo:{library.repo} is:issue is:closed closed:>={since}")):
        try:
            row[label] = (client.get("/search/issues", {"q": query, "per_page": 1}, use_cache=False).data or {}).get("total_count")
        except GitHubAPIError as exc:
            row["problems"].append(f"검색 실패: {exc.status}")
    if (row.get("closed_issues") or 0) < MIN_CLOSED:
        row["problems"].append(f"종료 이슈 {row.get('closed_issues')}건 (기준 {MIN_CLOSED})")
    if (row.get("closed_issues_last_year") or 0) < MIN_RECENT_CLOSED:
        row["problems"].append(f"최근 1년 종료 이슈 {row.get('closed_issues_last_year')}건 (기준 {MIN_RECENT_CLOSED})")
    for package in library.packages:
        data = fetch(PYPI_URL.format(name=package)) or {}
        info = data.get("info") or {}
        found = github_repo_from_urls(info.get("project_urls"), info.get("home_page"))
        row["packages"][package] = found
        if not data:
            row["problems"].append(f"PyPI에 없음: {package}")
        elif found and found.lower() != (row.get("full_name") or library.repo).lower():
            # PyPI에 예전 저장소 이름이 남아 있을 수 있다. GitHub가 옮겨 준 실제 이름으로 다시 비교한다
            current = renamed_to(client, found)
            row["packages"][package] = current or found
            if (current or found).lower() != (row.get("full_name") or library.repo).lower():
                row["problems"].append(f"{package}의 저장소가 다름: {current or found}")
    return row


def main(argv: list[str] | None = None, client: GitHubClient | None = None, fetch=None) -> int:
    parser = argparse.ArgumentParser(description="에이전트 코퍼스 목록 확인")
    parser.add_argument("--corpus", type=Path, default=ROOT / "configs/agent_corpus.yaml")
    parser.add_argument("--out", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    load_dotenv(ROOT / ".env")
    client = client or GitHubClient(token=os.getenv("GITHUB_TOKEN") or None,
                                    api_version=os.getenv("GITHUB_API_VERSION") or DEFAULT_API_VERSION,
                                    user_agent=os.getenv("GITHUB_USER_AGENT") or DEFAULT_USER_AGENT,
                                    settings=HttpSettings.from_dict(load_yaml(ROOT / "configs/app.yaml").get("http")))
    corpus = load_corpus(args.corpus)
    since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    rows = []
    for index, library in enumerate(corpus.libraries, start=1):
        print(f"({index}/{len(corpus.libraries)}) {library.repo}", file=sys.stderr, flush=True)
        rows.append(check_library(client, fetch or default_fetch, library, since))

    print(f"{'저장소':40} {'종료 이슈':>9} {'최근 1년':>8}  Discussions  문제")
    for row in rows:
        print(f"{row['repo']:40} {row.get('closed_issues') or '-':>9} {row.get('closed_issues_last_year') or '-':>8}  "
              f"{'O' if row.get('has_discussions') else 'X':^11}  {'; '.join(row['problems']) or '-'}")
    ok = sum(not r["problems"] for r in rows)
    print(f"\n== {len(rows)}개 중 문제 없음 {ok}개")
    write_json_atomic(args.out, {"checked_at": utc_now(), "since": since, "libraries": rows})
    print(f"보고서: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
