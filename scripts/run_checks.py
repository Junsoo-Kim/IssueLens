"""대표 질문과 평가용 이슈 로그를 한 번에 돌려 결과를 요약한다. 화면 캡처 없이 동작을 점검할 때 쓴다.

    python scripts/run_checks.py                     # 대표 질문 + 저장소마다 로그 1건
    python scripts/run_checks.py --logs 0            # 대표 질문만
    python scripts/run_checks.py --questions 0 --logs 2
    python scripts/run_checks.py --no-collect        # 질문 시점 수집 끄기 (GitHub 호출 없음)

화면(stdout)에는 원문 없는 요약만 출력한다. 케이스마다 결과(답변/거부), 시도별 판정, 질문 시점 수집 검색어와 건수,
인용 이슈, 처리 시간이 한 덩어리로 나온다. 답변 전문과 근거는 --out 파일(기본 data/processed/check_report.md,
.gitignore 대상)에 저장한다. 로그 케이스는 원본 평가용 이슈가 결과에 섞이지 않았는지도 확인한다.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from scripts.ask import STAGE_LABELS, format_state  # noqa: E402
from scripts.sample_logs import evaluation_samples  # noqa: E402
from src.services.assistant import build_assistant, resolve_paths  # noqa: E402
from src.services.incident_diagnoser import diagnose  # noqa: E402

# 기획서 2.2절 대표 질문을 현재 색인 범위에 맞춘 것 (영어·한국어, 근거 있음·없음 섞음)
QUESTIONS = [
    "sync job fails with timeout on large MySQL tables",
    "동기화 작업이 대용량 테이블에서 타임아웃으로 실패해",
    "Keycloak 업그레이드 후 로그인 시 토큰 검증 오류가 나요",
    "DataHub 인덱스 복원 중 NullPointerException이 발생해",
    "upgrade 후 로그인 시 TokenVerificationException 발생",
]
REASON_CHARS = 120


def short(text: str, limit: int = REASON_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def summarize(kind: str, label: str, state: dict, seconds: float, original: str | None = None) -> list[str]:
    """케이스 하나의 요약. 원문은 넣지 않는다."""
    answered = state.get("answered_from_evidence")
    cited = list(dict.fromkeys(e.issue_id for e in state.get("cited_evidence") or []))
    lines = [f"[{kind}] {label}",
             f"  결과: {'답변 (인용 ' + ', '.join(cited) + ')' if answered else '거부 (근거 부족)'} / {seconds:.0f}s"]
    for attempt in state.get("attempts", []):
        grade = attempt["grade"]
        lines.append(f"  - {STAGE_LABELS.get(attempt['stage'], attempt['stage'])}: 근거 {attempt['evidence_count']} "
                     f"{'충분' if grade['sufficient'] else '부족'} | {short(grade['reason'])}")
    collection = state.get("collection")
    if collection:
        searched = [f"{s['query'][:40]}({s['results']})" for s in collection.get("searched", [])]
        hits = [h["query"][:40] for h in collection.get("cache_hits", [])]
        selection = collection.get("selection") or {}
        lines.append(f"  - 수집: 검색 {searched or '-'} 캐시 {hits or '-'} / 후보 {len(collection.get('candidates', []))} "
                     f"/ 선택 {selection.get('selected_ids') or '-'}")
        lines += [f"  - 수집 오류: {short(e)}" for e in collection.get("errors", [])]
    lines += [f"  - 주의: {short(w)}" for w in state.get("warnings", [])]
    if original:
        leaked = original in cited or original in [n["issue_id"] for n in state.get("nearest_issues") or []]
        lines.append(f"  - 원본 이슈 {original}: {'결과에 나옴 (확인 필요)' if leaked else '결과에 없음 (정상)'}")
    return lines


def main(argv: list[str] | None = None, **overrides) -> int:
    parser = argparse.ArgumentParser(description="대표 질문·평가용 로그 일괄 점검")
    parser.add_argument("--questions", type=int, default=len(QUESTIONS), help="대표 질문 수 (0이면 생략)")
    parser.add_argument("--logs", type=int, default=1, help="저장소당 평가용 로그 수 (0이면 생략)")
    parser.add_argument("--repo", help="로그를 뽑을 저장소 하나 (owner/repo)")
    parser.add_argument("--no-collect", action="store_true", help="질문 시점 수집 끄기")
    parser.add_argument("--out", default="data/processed/check_report.md", help="답변 전문 저장 위치")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    root = overrides.pop("root", ROOT)
    load_dotenv(root / ".env")

    assistant = build_assistant(root, enable_collection=False if args.no_collect else None, **overrides)
    if assistant.retriever.issue_collection.count() == 0:
        print("색인이 비어 있습니다. 먼저 scripts/build_indexes.py를 실행하세요.", file=sys.stderr)
        return 2
    safety = assistant.app_config.get("safety_notice", "")
    cases = [("질문", q, None, None) for q in QUESTIONS[: args.questions]]
    if args.logs:
        samples = evaluation_samples(resolve_paths(root, assistant.app_config), args.repo, args.logs) or []
        cases += [("로그", f"{s['issue_id']} (평가용 원본)", s["log"], s["issue_id"]) for s in samples]

    summary, details, results = [], [], []
    for index, (kind, label, log, original) in enumerate(cases, start=1):
        print(f"({index}/{len(cases)}) {kind}: {short(label, 60)}", file=sys.stderr, flush=True)
        started = time.perf_counter()
        state = diagnose(assistant, log).state if log else assistant.ask(label)
        seconds = time.perf_counter() - started
        results.append((bool(state.get("answered_from_evidence")), bool(state.get("collection")), seconds))
        summary += [*summarize(kind, label, state, seconds, original), ""]
        details += [f"## [{kind}] {label}", "", "```text", format_state(state, safety), "```", ""]

    answered = sum(r[0] for r in results)
    collected = sum(r[1] for r in results)
    average = sum(r[2] for r in results) / len(results) if results else 0
    summary.append(f"== 합계 {len(results)}건: 답변 {answered} / 거부 {len(results) - answered} / "
                   f"질문 시점 수집 {collected}건 / 평균 {average:.0f}s")
    print("\n".join(summary))

    out = Path(args.out) if Path(args.out).is_absolute() else root / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(["# IssueLens 일괄 점검", "", *details]), encoding="utf-8")
    print(f"\n답변 전문: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
