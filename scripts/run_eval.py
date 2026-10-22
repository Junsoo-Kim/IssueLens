"""검증 질문 세트를 실행해 기대 결과와 비교한다 (기획서 11.2절, 요구사항 정의서 4장).

    python scripts/run_eval.py                       # data/eval/questions.yaml 전체
    python scripts/run_eval.py --only q01 q13        # 일부만
    python scripts/run_eval.py --no-collect          # 질문 시점 수집 끄기 (GitHub 호출 없음)

점수를 매기지 않고 질문마다 PASS/FAIL과 실패 이유만 출력한다. 모든 답변에 대해 인용 번호가 실제 근거와 맞는지,
로그 질문은 원본 평가용 이슈가 결과에 섞이지 않았는지도 확인한다. 답변 전문은 --out 파일
(기본 data/processed/eval_report.md, 커밋 제외)에 저장한다.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from scripts.ask import format_state  # noqa: E402
from scripts.run_checks import short  # noqa: E402
from scripts.sample_logs import issue_log  # noqa: E402
from src.collectors.cache import RawStore  # noqa: E402
from src.config import load_yaml  # noqa: E402
from src.prompts.answer_generator import NO_EVIDENCE_MESSAGE  # noqa: E402
from src.services.assistant import build_assistant, resolve_paths  # noqa: E402
from src.services.incident_diagnoser import diagnose  # noqa: E402
from src.services.issue_explorer import explore_issues  # noqa: E402

QUESTIONS = ROOT / "data/eval/questions.yaml"


def check_answer(expect: dict, state: dict, selected: str | None = None, original: str | None = None) -> list[str]:
    """RAG 답변 상태를 기대 결과와 비교해 실패 이유 목록을 돌려준다."""
    failures = []
    answered = bool(state.get("answered_from_evidence"))
    wanted = expect.get("answer", "any")
    if wanted is True and not answered:
        failures.append("답변 기대, 거부됨")
    if wanted is False and answered:
        failures.append("거부 기대, 답변함")
    if not answered and state.get("answer") != NO_EVIDENCE_MESSAGE:
        failures.append("거부 문구가 다름")
    if (state.get("citation_check") or {}).get("invalid"):
        failures.append(f"없는 근거 번호 인용 {state['citation_check']['invalid']}")
    cited = [e.issue_id for e in state.get("cited_evidence") or []]
    repos = expect.get("cited_repos")
    if repos and answered:
        others = sorted({i for i in cited if i.rsplit("#", 1)[0] not in repos})
        if others:
            failures.append(f"다른 저장소 근거 인용 {others}")
    if expect.get("cite_any") and answered and not set(expect["cite_any"]) & set(cited):
        failures.append(f"기대 이슈 미인용 {expect['cite_any']}")
    if expect.get("cite_issue") and selected and selected not in cited:
        failures.append(f"선택 이슈 {selected} 미인용")
    if original:
        shown = set(cited) | {n["issue_id"] for n in state.get("nearest_issues") or []}
        if original in shown:
            failures.append(f"평가용 원본 {original} 노출")
    return failures


def check_explore(expect: dict, result: dict) -> list[str]:
    failures = []
    ids = [c["issue_id"] for c in result["issues"]]
    if expect.get("empty") and ids:
        failures.append(f"결과 없음 기대, {len(ids)}건 나옴")
    if expect.get("issues_any") and not set(expect["issues_any"]) & set(ids):
        failures.append(f"기대 이슈 없음 {expect['issues_any']}")
    if expect.get("issues_none") and set(expect["issues_none"]) & set(ids):
        failures.append(f"나오면 안 되는 이슈 {sorted(set(expect['issues_none']) & set(ids))}")
    for key, field, label in (("result_repos", "repo", "저장소"), ("result_types", "issue_type", "유형")):
        if expect.get(key):
            wrong = [c["issue_id"] for c in result["issues"] if c[field] not in expect[key]]
            if wrong:
                failures.append(f"{label} 조건 불일치 {wrong}")
    return failures


def outcome_line(item: dict, result, seconds: float) -> str:
    if item["type"] == "condition":
        return f"결과 {len(result['issues'])}건 {[c['issue_id'] for c in result['issues']][:5]}"
    cited = list(dict.fromkeys(e.issue_id for e in result.get("cited_evidence") or []))
    attempts = "→".join(f"{a['stage'][:4]}{'O' if a['grade']['sufficient'] else 'X'}" for a in result.get("attempts", []))
    status = f"답변 (인용 {', '.join(cited)})" if result.get("answered_from_evidence") else "거부"
    return f"{status} / 시도 {attempts or '-'}"


def run_item(item: dict, assistant, raw: RawStore, categories: dict):
    """(결과, 실패 이유, 건너뜀 사유)."""
    expect = item.get("expect") or {}
    kind = item["type"]
    if kind == "condition":
        pipeline = assistant.pipeline
        result = explore_issues(pipeline.fast_llm, assistant.retriever, item["question"], pipeline.repositories,
                                categories, judge_llm=pipeline.llm).as_dict()
        return result, check_explore(expect, result), None
    if kind == "error_log":
        found = issue_log(raw, item["log_from"])
        if not found:
            return None, [], f"{item['log_from']} 원본 로그 없음"
        state = diagnose(assistant, found["log"]).state
        return state, check_answer(expect, state, original=item["log_from"]), None
    state = assistant.ask(item["question"], selected_issue_id=item.get("issue"), repo=item.get("repo"))
    return state, check_answer(expect, state, selected=item.get("issue")), None


def main(argv: list[str] | None = None, **overrides) -> int:
    parser = argparse.ArgumentParser(description="검증 질문 세트 실행")
    parser.add_argument("--questions", default=str(QUESTIONS), help="질문 세트 YAML")
    parser.add_argument("--only", nargs="*", help="실행할 질문 ID")
    parser.add_argument("--no-collect", action="store_true", help="질문 시점 수집 끄기")
    parser.add_argument("--out", default="data/processed/eval_report.md", help="답변 전문 저장 위치")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    root = overrides.pop("root", ROOT)
    load_dotenv(root / ".env")

    with open(args.questions, encoding="utf-8") as f:
        items = yaml.safe_load(f)["questions"]
    if args.only:
        items = [i for i in items if i["id"] in args.only]
    assistant = build_assistant(root, enable_collection=False if args.no_collect else None, **overrides)
    if assistant.retriever.issue_collection.count() == 0:
        print("색인이 비어 있습니다. 먼저 scripts/build_indexes.py를 실행하세요.", file=sys.stderr)
        return 2
    raw = RawStore(resolve_paths(root, assistant.app_config)["raw_dir"])
    categories = load_yaml(root / "configs/repositories.yaml").get("categories", {})
    safety = assistant.app_config.get("safety_notice", "")

    lines, details, counts = [], [], {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for index, item in enumerate(items, start=1):
        label = item.get("question") or item.get("log_from")
        print(f"({index}/{len(items)}) {item['id']} {item['type']}: {short(label, 50)}", file=sys.stderr, flush=True)
        started = time.perf_counter()
        result, failures, skipped = run_item(item, assistant, raw, categories)
        seconds = time.perf_counter() - started
        verdict = "SKIP" if skipped else ("FAIL" if failures else "PASS")
        counts[verdict] += 1
        lines.append(f"{verdict} {item['id']} [{item['type']}] {short(label, 50)} ({seconds:.0f}s)")
        if skipped:
            lines.append(f"     건너뜀: {skipped}")
            continue
        lines.append(f"     {outcome_line(item, result, seconds)}")
        lines += [f"     실패: {reason}" for reason in failures]
        body = (f"조건: {result['conditions']}\n결과: {[c['issue_id'] for c in result['issues']]}"
                if item["type"] == "condition" else format_state(result, safety))
        details += [f"## {verdict} {item['id']} [{item['type']}] {label}", "", "```text", body, "```", ""]

    lines.append(f"\n== {len(items)}건: PASS {counts['PASS']} / FAIL {counts['FAIL']} / SKIP {counts['SKIP']}")
    print("\n".join(lines))
    out = Path(args.out) if Path(args.out).is_absolute() else root / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(["# IssueLens 검증 질문 세트 결과", "", *lines, "", *details]), encoding="utf-8")
    print(f"\n답변 전문: {out}")
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
