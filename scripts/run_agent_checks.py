"""에이전트 검증 PR 세트 실행 (기획서 17.11절). 로컬에서 실제 LLM·에이전트 코퍼스로 dry-run한다.

    python scripts/run_agent_checks.py                       # 전체 사례
    python scripts/run_agent_checks.py --case openai_legacy_call --case logic_only
    python scripts/run_agent_checks.py --no-collect          # GitHub 검색 없이 구축된 코퍼스만

결과: data/manifests/agent_checks.json (사례별 판정과 관측값, 원문 없음)
코멘트 본문: agent_index/checks/{사례}.md (근거 요약이 들어 있어 커밋하지 않는다)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from agent.checks import RecordingLLM, grade, load_cases, run_case  # noqa: E402
from agent.main import main as agent_main  # noqa: E402
from agent.researcher import agent_llms  # noqa: E402
from src.collectors.cache import utc_now, write_json_atomic  # noqa: E402
from src.config import load_yaml  # noqa: E402

REPORT = ROOT / "data/manifests/agent_checks.json"


def main(argv: list[str] | None = None, llm=None, fast_llm=None, embedder=None, client=None) -> int:
    parser = argparse.ArgumentParser(description="에이전트 검증 PR 세트 dry-run")
    parser.add_argument("--case", action="append", help="이 사례만 (여러 번 지정 가능)")
    parser.add_argument("--index", type=Path, default=ROOT / "agent_index")
    parser.add_argument("--no-collect", action="store_true", help="동적 수집(GitHub 검색) 없이 구축된 코퍼스만 쓴다")
    parser.add_argument("--out", type=Path, default=REPORT)
    parser.add_argument("--comments-dir", type=Path, help="코멘트 본문 저장 위치 (기본 {index}/checks)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(ROOT / ".env")

    if llm is None:
        llm, fast_llm = agent_llms(load_yaml(ROOT / "configs/app.yaml"))
        if llm is None:
            print("OPENAI_API_KEY가 없어 규칙 기반 후보로만 확인합니다 (근거 검색은 하지 않음).", file=sys.stderr)
    cases = [c for c in load_cases() if not args.case or c["id"] in args.case]
    comments_dir = args.comments_dir or args.index / "checks"
    extra = ["--index", str(args.index), "--cache-dir", str(args.index / "registry")] + (["--no-collect"] if args.no_collect else [])

    results = []
    for number, case in enumerate(cases, start=1):
        print(f"({number}/{len(cases)}) {case['id']}: {case['title']}", flush=True)
        recorder = RecordingLLM(llm) if llm is not None else None
        fast_recorder = RecordingLLM(fast_llm) if fast_llm is not None else None
        try:
            output, _ = run_case(case, agent_main, extra, env=None, llm=recorder, fast_llm=fast_recorder,
                                 embedder=embedder, client=client)
        except Exception as exc:  # 한 사례가 실패해도 나머지는 확인한다
            from agent.checks import CheckResult
            results.append(CheckResult(case["id"], case["title"], "ERROR", problems=[f"{type(exc).__name__}: {exc}"]))
            print(f"  ERROR {type(exc).__name__}: {exc}", flush=True)
            continue
        prompts = (recorder.prompts if recorder else []) + (fast_recorder.prompts if fast_recorder else [])
        result = grade(case, output, prompts)
        results.append(result)
        if result.comment:
            comments_dir.mkdir(parents=True, exist_ok=True)
            (comments_dir / f"{case['id']}.md").write_text(result.comment, encoding="utf-8")
        o = result.observed
        print(f"  {result.status}  후보 {o['findings'] or '-'} | 근거 기반 {o['rag']} | 일반 {o['general']} | "
              f"코멘트 {'있음' if o['comment'] else '없음'}", flush=True)
        for line in result.problems + result.warnings:
            print(f"    - {line}", flush=True)

    counts = {s: sum(r.status == s for r in results) for s in ("PASS", "WARN", "FAIL", "ERROR")}
    print("\n== " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    write_json_atomic(args.out, {"checked_at": utc_now(), "llm": llm is not None, "no_collect": args.no_collect,
                                 "counts": counts, "cases": [r.as_dict() for r in results]})
    print(f"보고서: {args.out}\n코멘트: {comments_dir}")
    return 1 if counts["FAIL"] or counts["ERROR"] else 0


if __name__ == "__main__":
    sys.exit(main())
