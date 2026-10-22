"""로그 진단 탭(F2010)에 넣어 볼 실제 오류 로그를 평가용 이슈에서 뽑는다.

    python scripts/sample_logs.py                       # 저장소마다 1건씩
    python scripts/sample_logs.py --repo keycloak/keycloak --limit 3
    python scripts/sample_logs.py --out data/processed/sample_logs.md

평가용 이슈는 색인과 질문 시점 수집에서 모두 빠져 있어, 그 이슈의 로그로 진단하면 답이 새어 들어가지 않은 상태로
비슷한 다른 이슈를 찾는지 볼 수 있다. 원본 이슈 링크를 함께 출력하니 진단 결과와 실제 해결 내용을 비교하면 된다.
출력에는 이슈 원문 일부가 들어가므로 커밋하지 않는다 (data/processed/는 .gitignore 대상).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collectors.cache import RawStore, read_jsonl  # noqa: E402
from src.config import load_yaml  # noqa: E402
from src.preprocessing.build import load_raw  # noqa: E402
from src.preprocessing.tagger import EXCEPTION_NAME, STACK_TRACE  # noqa: E402
from src.services.assistant import resolve_paths  # noqa: E402

CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
LOG_SIGNAL = re.compile(r"\b(ERROR|FATAL|Exception|Traceback|Caused by)\b")
MIN_LINES = 3
MAX_LINES = 40


def log_blocks(text: str) -> list[str]:
    """코드 블록 중 스택 트레이스나 예외·오류 줄이 있는 것. 너무 짧거나 긴 블록은 다듬는다."""
    blocks = []
    for block in CODE_BLOCK.findall(text or ""):
        lines = [line.rstrip() for line in block.strip().splitlines() if line.strip()]
        if len(lines) < MIN_LINES:
            continue
        joined = "\n".join(lines)
        if STACK_TRACE.search(joined) or (EXCEPTION_NAME.search(joined) and LOG_SIGNAL.search(joined)):
            blocks.append("\n".join(lines[:MAX_LINES]))
    return blocks


def sample(rows: list[dict], raw: RawStore, per_repo: int) -> list[dict]:
    counts: dict[str, int] = {}
    samples = []
    for row in rows:
        repo = row["repo"]
        if counts.get(repo, 0) >= per_repo:
            continue
        issue = load_raw(raw, repo, "issues", row["number"]) or {}
        comments = load_raw(raw, repo, "comments", row["number"]) or []
        texts = [issue.get("body") or "", *(c.get("body") or "" for c in comments)]
        blocks = [b for text in texts for b in log_blocks(text)]
        if not blocks:
            continue
        counts[repo] = counts.get(repo, 0) + 1
        samples.append({"issue_id": row["issue_id"], "title": issue.get("title", ""),
                        "url": issue.get("html_url", ""), "log": max(blocks, key=len)})
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="로그 진단 시험용 오류 로그 추출 (평가용 이슈)")
    parser.add_argument("--repo", help="저장소 하나만 (owner/repo)")
    parser.add_argument("--limit", type=int, default=1, help="저장소당 건수")
    parser.add_argument("--out", help="마크다운 파일로 저장")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    paths = resolve_paths(ROOT, load_yaml(ROOT / "configs/app.yaml"))
    selection = paths["manifest_dir"] / "final_selection.jsonl"
    if not selection.exists():
        print(f"{selection}가 없습니다. 먼저 수집과 선별을 실행하세요.", file=sys.stderr)
        return 2
    rows = [r for r in read_jsonl(selection) if r["final_split"] == "evaluation" and (not args.repo or r["repo"] == args.repo)]
    for row in rows:
        row.setdefault("repo", row["issue_id"].rsplit("#", 1)[0])
        row.setdefault("number", int(row["issue_id"].rsplit("#", 1)[1]))
    samples = sample(rows, RawStore(paths["raw_dir"]), args.limit)

    parts = []
    for s in samples:
        parts.append(f"## {s['issue_id']} {s['title']}\n원본(진단 후 비교용): {s['url']}\n\n```\n{s['log']}\n```\n")
    text = "\n".join(parts) or "로그가 들어 있는 평가용 이슈를 찾지 못했습니다."
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{len(samples)}건을 {args.out}에 저장했습니다.")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
