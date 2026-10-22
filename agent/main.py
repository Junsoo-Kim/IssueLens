"""PR 선제 리뷰 에이전트 진입점 (기획서 17.10절).

    python -m agent.main --pr owner/repo#123 --dry-run      # PR 변경 파일을 읽어 리뷰 입력을 출력
    python -m agent.main --diff changes.patch --dry-run     # 로컬 git diff 출력으로
    git diff main | python -m agent.main --diff - --dry-run

GitHub Action에서는 인자 없이 실행하면 GITHUB_EVENT_PATH의 pull_request 이벤트에서 PR을 읽는다.
현재 단계(17.12절 E)까지는 변경 파일, 변경 신호, 라이브러리 매핑, 리뷰 후보(프롬프트 4), 후보별 근거 검색과
해결 방법 초안을 출력한다. 코멘트 작성·게시는 단계 F에서 붙인다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.diff_reader import DiffBundle, fetch_pull_request, parse_unified_diff  # noqa: E402
from agent.registry import Registry, format_resolutions, load_corpus  # noqa: E402
from agent.researcher import Research, agent_llms, build_researcher, github_cache  # noqa: E402
from agent.reviewer import Review, review  # noqa: E402
from agent.signals import extract_signals, format_signals  # noqa: E402
from src.collectors.github_client import DEFAULT_API_VERSION, DEFAULT_USER_AGENT, GitHubClient, HttpSettings  # noqa: E402
from src.config import load_yaml  # noqa: E402

PR_REF = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
TARGET_CONFIG = ".github/issuelens.yml"


def load_settings(target_root: Path | None = None) -> dict:
    """기본 설정에 대상 레포의 .github/issuelens.yml을 덮어쓴다 (최상위 키 단위)."""
    settings = load_yaml(ROOT / "configs/agent.yaml")
    target = (target_root or Path.cwd()) / TARGET_CONFIG
    if target.exists():
        override = load_yaml(target)
        for section in ("diff", "review"):
            settings.setdefault(section, {}).update({k: v for k, v in override.items() if k in settings.get(section, {})})
    return settings


def pr_from_event(env: dict) -> tuple[str, int] | None:
    """Action의 pull_request 이벤트에서 (저장소, PR 번호)."""
    path, repo = env.get("GITHUB_EVENT_PATH"), env.get("GITHUB_REPOSITORY")
    if not path or not repo or not Path(path).exists():
        return None
    with open(path, encoding="utf-8") as f:
        event = json.load(f)
    number = (event.get("pull_request") or {}).get("number")
    return (repo, int(number)) if number else None


def github_client(env: dict, cache=None) -> GitHubClient:
    return GitHubClient(token=env.get("GITHUB_TOKEN") or None,
                        api_version=env.get("GITHUB_API_VERSION") or DEFAULT_API_VERSION,
                        user_agent=env.get("GITHUB_USER_AGENT") or DEFAULT_USER_AGENT,
                        settings=HttpSettings.from_dict(load_yaml(ROOT / "configs/app.yaml").get("http")), cache=cache)


def format_bundle(bundle: DiffBundle) -> str:
    lines = [f"== 변경 파일 ({bundle.source}{', ' + bundle.head_sha[:7] if bundle.head_sha else ''})",
             f"리뷰 {len(bundle.reviewed)}개 / 건너뜀 {len(bundle.skipped)}개"
             + (" / 상한 초과로 일부만 리뷰" if bundle.truncated else "")]
    for change in bundle.reviewed:
        lines.append(f"  {change.status:8} {change.path} (+{change.additions} -{change.deletions}, hunk {len(change.hunks)}개)")
    for change in bundle.skipped:
        lines.append(f"  건너뜀   {change.path} ({change.skipped})")
    masked = ", ".join(f"{k} {v}" for k, v in sorted(bundle.redactions.items()))
    lines.append(f"마스킹: {masked or '없음'}")
    return "\n".join(lines)


def format_review(result: Review, researches: dict[str, Research]) -> str:
    lines = [f"== 리뷰 후보 ({'프롬프트 4' if result.source == 'llm' else '규칙 신호'}"
             f"{', ' + result.error if result.error else ''}): 근거 검색 {len(result.rag)}건, 일반 {len(result.general)}건"]
    for f in result.findings:
        where = f"{f.file}:{f.line}" if f.line else f.file
        target = f" {f.library} -> {f.repo}" if f.library else ""
        lines.append(f"[{f.id}] {where} {f.kind}{target} ({f.severity})")
        lines.append(f"  {f.summary}" + (f" / {f.concern}" if f.concern else ""))
        if f.needs_research:
            lines.append(f"  검색어: {'; '.join(f.search_queries) or '-'} | 정확 토큰: {', '.join(f.exact_tokens) or '-'}")
        research = researches.get(f.id)
        if research is None:
            continue
        if research.status == "grounded":
            lines.append(f"  -> 근거 있음 (시도 {len(research.attempts)}회, 검색 {research.searches}회)")
            lines += [f"     {line}" for line in research.draft.splitlines() if line.strip()]
            lines += [f"     [근거 {e.number}] {e.issue_id} ({e.document_type}{', 유지보수자' if e.author_role == 'maintainer' else ''}) "
                      f"{e.source_url}" for e in research.cited_evidence]
        else:
            note = " (검색 예산 소진, 동적 수집 안 함)" if research.collection_skipped else ""
            lines.append(f"  -> {'근거 부족' if research.status == 'no_evidence' else '건너뜀'}: {research.reason}{note}"
                         + (" -> 일반 리뷰로 내림" if research.status == "no_evidence" else ""))
        lines += [f"  주의: {w}" for w in research.warnings]
    return "\n".join(lines)


def read_bundle(args, env: dict, settings: dict) -> DiffBundle:
    if args.diff:
        text = sys.stdin.read() if args.diff == "-" else Path(args.diff).read_text(encoding="utf-8")
        return parse_unified_diff(text, "표준 입력" if args.diff == "-" else args.diff, settings)
    if args.pr:
        match = PR_REF.match(args.pr)
        if not match:
            raise SystemExit("--pr는 owner/repo#번호 형식입니다")
        repo, number = match.group(1), int(match.group(2))
    else:
        found = pr_from_event(env)
        if not found:
            raise SystemExit("--pr, --diff 중 하나를 주거나 pull_request 이벤트의 Action에서 실행하세요")
        repo, number = found
    return fetch_pull_request(github_client(env), repo, number, settings)


def main(argv: list[str] | None = None, env: dict | None = None, fetch=None, llm=None, fast_llm=None,
         embedder=None, client: GitHubClient | None = None) -> int:
    parser = argparse.ArgumentParser(description="IssueLens PR 선제 리뷰 에이전트")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--pr", help="owner/repo#번호")
    source.add_argument("--diff", help="git diff 출력 파일 (- 이면 표준 입력)")
    parser.add_argument("--dry-run", action="store_true", help="코멘트를 게시하지 않고 출력만 한다")
    parser.add_argument("--target-root", type=Path, help="대상 레포 루트 (.github/issuelens.yml 위치, 기본 현재 폴더)")
    parser.add_argument("--json", action="store_true", help="리뷰 입력을 JSON으로 출력")
    parser.add_argument("--no-pypi", action="store_true", help="큐레이션 목록에 없는 패키지의 PyPI 조회를 하지 않는다")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data/cache", help="패키지 매핑 캐시 위치")
    parser.add_argument("--index", type=Path, default=ROOT / "agent_index", help="에이전트 코퍼스 (C 방식 기본 위치)")
    parser.add_argument("--skip-research", action="store_true", help="리뷰 후보까지만 만들고 근거 검색을 하지 않는다")
    parser.add_argument("--no-collect", action="store_true", help="동적 수집(GitHub 검색) 없이 색인된 근거만 쓴다")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if env is None:  # 로컬 실행: .env의 OPENAI_API_KEY, GITHUB_TOKEN을 읽는다 (Action에서는 secrets가 환경 변수로 들어온다)
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    env = dict(os.environ if env is None else env)

    settings = load_settings(args.target_root)
    bundle = read_bundle(args, env, settings.get("diff", {}))
    signals = extract_signals(bundle, args.target_root)
    registry = Registry(load_corpus(ROOT / "configs/agent_corpus.yaml"), args.cache_dir,
                        fetch=(lambda url: None) if args.no_pypi else fetch)
    resolutions = registry.resolve_signals(signals) if signals.python_project else []
    sections = [format_bundle(bundle), format_signals(signals)] + ([format_resolutions(resolutions)] if resolutions else [])
    output = {"diff": bundle.as_dict(), "signals": signals.as_dict(), "libraries": registry.as_dict(resolutions)}

    if signals.python_project and bundle.reviewed:
        if llm is None:
            llm, fast_llm = agent_llms(load_yaml(ROOT / "configs/app.yaml"), env)
        review_settings = settings.get("review", {})
        result = review(llm, bundle, signals, format_signals(signals), registry.resolve,
                        review_settings.get("max_rag_findings", 5), review_settings.get("max_general_findings", 5))
        researches: dict[str, Research] = {}
        if result.rag and not args.skip_research:
            if embedder is None and llm is not None:
                from src.indexing.embeddings import LazyOpenAIEmbedder
                embedder = LazyOpenAIEmbedder(load_yaml(ROOT / "configs/retrieval.yaml").get("embedding", {})
                                              .get("model", "text-embedding-3-small"))
            if embedder is not None:
                collect_client = None if args.no_collect else (client or github_client(env, github_cache(args.index)))
                researcher = build_researcher(ROOT, args.index, registry.corpus, llm, fast_llm or llm, embedder,
                                              collect_client, review_settings.get("search_budget", 20))
                researches = {r.finding_id: r for r in researcher.run(result.rag)}
        sections.append(format_review(result, researches))
        output.update(review=result.as_dict(), research=[r.as_dict() for r in researches.values()])

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(sections))
    if not args.dry_run:
        print("\n코멘트 게시는 아직 구현 전입니다 (17.12절 단계 F). 지금은 --dry-run과 같게 동작합니다.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
