"""에이전트 코퍼스 구축 (기획서 17.12절 단계 D). 로컬에서 GitHub API와 임베딩 API로 실행한다.

    python scripts/build_agent_corpus.py --repo openai/openai-python --deep 20      # 한두 개로 먼저 확인
    python scripts/build_agent_corpus.py                                            # C 방식: 목록 전체
    python scripts/build_agent_corpus.py --skip-embed                               # 수집·판정만 확인 (임베딩 비용 없음)
    python scripts/build_agent_corpus.py --holdout 5                                # 판정 통과 5건마다 1건은 평가용으로 남김

결과는 agent_index/(raw, chroma, sparse, corpus_state.json)에 쌓인다. 원문이 들어 있어 커밋하지 않는다.
이미 구축한 라이브러리는 --refresh-days가 지나기 전까지 건너뛰고, 지나면 마지막 구축 이후 종료된 이슈만 더한다.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from agent.library_corpus import (LibraryCorpusBuilder, LibraryIndex, default_post, load_state,  # noqa: E402
                                  needs_build, save_state)
from agent.registry import load_corpus  # noqa: E402
from src.collectors.github_client import DEFAULT_API_VERSION, DEFAULT_USER_AGENT, GitHubClient, HttpSettings  # noqa: E402
from src.config import load_yaml  # noqa: E402

DEFAULT_OUT = ROOT / "agent_index"


def format_report(report) -> str:
    kinds = ", ".join(f"{k} {v}" for k, v in sorted(report.kinds.items())) or "없음"
    rejected = ", ".join(f"{k} {v}" for k, v in sorted(report.rejected.items(), key=lambda kv: -kv[1])) or "없음"
    lines = [f"  검색 {report.searched} -> 사전 필터 통과 {report.prefiltered} -> 상세 수집 {report.detailed}",
             f"  색인 이슈 {report.indexed_issues}, Discussions {report.discussions}, 평가용 보류 {report.held_out}, "
             f"청크 {report.chunks}, 요청 {report.requests}회",
             f"  해결 유형: {kinds}", f"  탈락: {rejected}"]
    if report.stopped:
        lines.append(f"  중단: {report.stopped}")
    lines += [f"  오류: {e}" for e in report.errors[:5]]
    return "\n".join(lines)


def main(argv: list[str] | None = None, client: GitHubClient | None = None, post=None, embedder=None) -> int:
    parser = argparse.ArgumentParser(description="에이전트 코퍼스(라이브러리 저장소 해결 이력) 구축")
    parser.add_argument("--corpus", type=Path, default=ROOT / "configs/agent_corpus.yaml")
    parser.add_argument("--repo", action="append", help="이 저장소만 (여러 번 지정 가능)")
    parser.add_argument("--deep", type=int, help="라이브러리당 색인할 이슈 수 (기본 deep_target_local)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--skip-embed", action="store_true", help="임베딩 없이 BM25 색인만 만든다. 메타데이터는 Chroma에 있어 검색에는 임베딩이 필요하다")
    parser.add_argument("--no-discussions", action="store_true")
    parser.add_argument("--holdout", type=int, default=0, help="판정을 통과한 N건마다 1건을 평가용으로 남긴다")
    parser.add_argument("--refresh-days", type=int, default=7)
    parser.add_argument("--force", action="store_true", help="최신이어도 다시 구축(증분)한다")
    parser.add_argument("--request-budget", type=int, help="라이브러리당 GitHub 요청 상한")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    load_dotenv(ROOT / ".env")

    app = load_yaml(ROOT / "configs/app.yaml")
    retrieval = load_yaml(ROOT / "configs/retrieval.yaml")
    token = os.getenv("GITHUB_TOKEN") or None
    client = client or GitHubClient(token=token, api_version=os.getenv("GITHUB_API_VERSION") or DEFAULT_API_VERSION,
                                    user_agent=os.getenv("GITHUB_USER_AGENT") or DEFAULT_USER_AGENT,
                                    settings=HttpSettings.from_dict(app.get("http")))
    if embedder is None and not args.skip_embed:
        from src.indexing.embeddings import LazyOpenAIEmbedder
        embedder = LazyOpenAIEmbedder(retrieval.get("embedding", {}).get("model", "text-embedding-3-small"))
    corpus = load_corpus(args.corpus)
    tagging = load_yaml(ROOT / "configs/repositories.yaml").get("tagging", {})
    index = LibraryIndex(args.out, retrieval, embedder)
    builder = LibraryCorpusBuilder(client, index, tagging, post=None if args.no_discussions else (post or default_post(token)),
                                   shallow_limit=corpus.defaults.get("shallow_candidate_limit", 1000),
                                   holdout_every=args.holdout, request_budget=args.request_budget)
    deep = args.deep or corpus.defaults.get("deep_target_local", 150)

    wanted = {r.lower() for r in args.repo or []}
    libraries = [lib for lib in corpus.libraries if not wanted or lib.repo.lower() in wanted]
    unknown = wanted - {lib.repo.lower() for lib in libraries}
    if unknown:
        print(f"목록에 없는 저장소: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    state = load_state(args.out)
    for number, library in enumerate(libraries, start=1):
        mode = needs_build(state, library.repo, args.refresh_days) or ("incremental" if args.force else None)
        if mode is None:
            print(f"({number}/{len(libraries)}) {library.repo}: 최신 (건너뜀)")
            continue
        print(f"({number}/{len(libraries)}) {library.repo}: {'처음 구축' if mode == 'full' else '증분 갱신'}", flush=True)
        report = builder.build(library, deep, state, mode)
        save_state(args.out, state)  # 중간에 멈춰도 끝난 라이브러리는 남긴다
        print(format_report(report), flush=True)
    print(f"\n색인: {args.out}" + (" (BM25만)" if args.skip_embed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
