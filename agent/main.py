"""PR 선제 리뷰 에이전트 진입점 (기획서 17.10절).

    python -m agent.main --pr owner/repo#123 --dry-run      # PR 변경 파일을 읽어 리뷰 입력을 출력
    python -m agent.main --diff changes.patch --dry-run     # 로컬 git diff 출력으로
    git diff main | python -m agent.main --diff - --dry-run

GitHub Action에서는 인자 없이 실행하면 GITHUB_EVENT_PATH의 pull_request 이벤트에서 PR을 읽는다.
변경 파일 -> 변경 신호 -> 라이브러리 매핑 -> 리뷰 후보(프롬프트 4) -> 후보별 근거 검색·해결 방법 -> PR 코멘트.
--dry-run이면 코멘트를 게시하지 않고 출력만 한다. 포크에서 온 PR은 리뷰하지 않는다.
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

from agent.comment import compose, publish  # noqa: E402
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


def read_event(env: dict) -> dict:
    path = env.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pr_from_event(env: dict) -> tuple[str, int] | None:
    """Action의 pull_request 이벤트에서 (저장소, PR 번호)."""
    repo, number = env.get("GITHUB_REPOSITORY"), (read_event(env).get("pull_request") or {}).get("number")
    return (repo, int(number)) if repo and number else None


def is_fork_pr(env: dict) -> bool:
    """포크 PR은 secrets를 받지 못하고 쓰기 권한도 없다. pull_request_target으로 우회하지 않고 건너뛴다 (17.9절)."""
    pr = read_event(env).get("pull_request") or {}
    head, base = (pr.get("head") or {}).get("repo") or {}, (pr.get("base") or {}).get("repo") or {}
    return bool(head.get("full_name") and base.get("full_name") and head["full_name"] != base["full_name"])


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


def target_pr(args, env: dict) -> tuple[str, int] | None:
    """리뷰할 PR (저장소, 번호). 로컬 diff면 None."""
    if args.diff:
        return None
    if args.pr:
        match = PR_REF.match(args.pr)
        if not match:
            raise SystemExit("--pr는 owner/repo#번호 형식입니다")
        return match.group(1), int(match.group(2))
    found = pr_from_event(env)
    if not found:
        raise SystemExit("--pr, --diff 중 하나를 주거나 pull_request 이벤트의 Action에서 실행하세요")
    return found


def read_bundle(args, pr: tuple[str, int] | None, client, settings: dict) -> DiffBundle:
    if pr is None:
        text = sys.stdin.read() if args.diff == "-" else Path(args.diff).read_text(encoding="utf-8")
        return parse_unified_diff(text, "표준 입력" if args.diff == "-" else args.diff, settings)
    return fetch_pull_request(client, pr[0], pr[1], settings)


def build_corpus(args, env: dict, settings: dict, corpus, review_result: Review, registry: Registry, embedder) -> list[dict]:
    """A 방식: 근거 검색 대상 후보가 쓰는 큐레이션 라이브러리 중 색인이 없거나 오래된 것만 구축한다 (17.5절)."""
    from agent.library_corpus import LibraryCorpusBuilder, LibraryIndex, default_post, ensure_libraries

    corpus_settings = settings.get("corpus", {})
    resolutions = [registry.resolve(f.library) for f in review_result.rag]
    builder = LibraryCorpusBuilder(
        github_client(env, github_cache(args.index)), LibraryIndex(args.index, load_yaml(ROOT / "configs/retrieval.yaml"), embedder),
        load_yaml(ROOT / "configs/repositories.yaml").get("tagging", {}), post=default_post(env.get("GITHUB_TOKEN")),
        shallow_limit=corpus.defaults.get("shallow_candidate_limit", 1000),
        request_budget=corpus_settings.get("request_budget", 250))
    return ensure_libraries(builder, corpus, resolutions, args.index, corpus.defaults.get("deep_target_action", 60),
                            corpus_settings.get("max_new_libraries", 3), corpus_settings.get("refresh_days", 7))


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
    parser.add_argument("--build-corpus", action="store_true",
                        help="A 방식: 후보가 쓰는 큐레이션 라이브러리 중 색인이 없는 것을 먼저 구축한다 (Action 기본)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if env is None:  # 로컬 실행: .env의 OPENAI_API_KEY, GITHUB_TOKEN을 읽는다 (Action에서는 secrets가 환경 변수로 들어온다)
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    env = dict(os.environ if env is None else env)

    settings = load_settings(args.target_root)
    pr = target_pr(args, env)
    if pr is not None and not args.pr and is_fork_pr(env):
        print("포크에서 온 PR이어서 리뷰하지 않습니다 (secrets와 쓰기 권한이 없음).")
        return 0
    pr_client = github_client(env) if pr is not None else None
    bundle = read_bundle(args, pr, pr_client, settings.get("diff", {}))
    signals = extract_signals(bundle, args.target_root)
    registry = Registry(load_corpus(ROOT / "configs/agent_corpus.yaml"), args.cache_dir,
                        fetch=(lambda url: None) if args.no_pypi else fetch)
    resolutions = registry.resolve_signals(signals) if signals.python_project else []
    sections = [format_bundle(bundle), format_signals(signals)] + ([format_resolutions(resolutions)] if resolutions else [])
    output = {"diff": bundle.as_dict(), "signals": signals.as_dict(), "libraries": registry.as_dict(resolutions)}
    comment, notes = None, []

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
            if embedder is not None and args.build_corpus:
                built = build_corpus(args, env, settings, registry.corpus, result, registry, embedder)
                output["corpus"] = built
                pending = [b["repo"] for b in built if b["status"] in ("deferred", "failed")]
                if pending:
                    notes.append(f"코퍼스 준비 중인 라이브러리: {', '.join(pending)} (다음 실행에서 이어서 구축)")
            if embedder is not None:
                collect_client = None if args.no_collect else (client or github_client(env, github_cache(args.index)))
                researcher = build_researcher(ROOT, args.index, registry.corpus, llm, fast_llm or llm, embedder,
                                              collect_client, review_settings.get("search_budget", 20))
                researches = {r.finding_id: r for r in researcher.run(result.rag)}
        sections.append(format_review(result, researches))
        output.update(review=result.as_dict(), research=[r.as_dict() for r in researches.values()])
        if not review_settings.get("general_review", True):
            result.findings = [f for f in result.findings if f.id in researches and researches[f.id].status == "grounded"]
        comment = compose(bundle, result, researches, pr[0] if pr else None, notes)
        output["comment"] = comment

    sections.append("== PR 코멘트\n" + (comment or "(지적 없음: 새 코멘트를 만들지 않음)"))
    if pr is not None and not args.dry_run and signals.python_project:
        action = publish(pr_client, pr[0], pr[1], comment, bundle.head_sha)
        output["published"] = action
        sections.append(f"게시: {action}")
    elif pr is None and not args.dry_run:
        sections.append("로컬 diff는 게시할 PR이 없어 출력만 했습니다.")
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(sections))
    return 0


if __name__ == "__main__":
    sys.exit(main())
