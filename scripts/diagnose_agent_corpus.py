"""에이전트 코퍼스 판정 진단. 이미 받은 원본(agent_index/raw)만 읽고 네트워크를 쓰지 않는다.

    python scripts/diagnose_agent_corpus.py --repo langchain-ai/langchain

상세 수집한 이슈마다 종료 사유, 라벨, 닫은 사람, 댓글 작성자 구분, 연결 PR, 판정 결과를 출력한다.
이슈 제목·본문·댓글 원문은 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.library_corpus import prefilter, resolution_kind  # noqa: E402
from src.collectors.cache import RawStore, repo_slug  # noqa: E402
from src.collectors.comment_collector import comment_priority  # noqa: E402
from src.collectors.issue_collector import to_shallow_record  # noqa: E402
from src.collectors.pull_request_collector import summarize_pull_request  # noqa: E402
from src.collectors.timeline_collector import extract_links  # noqa: E402
from src.config import load_yaml  # noqa: E402
from src.preprocessing.build import load_raw  # noqa: E402
from src.preprocessing.resolution import infer_maintainers, is_automated_notice, is_bot, judge_evidence  # noqa: E402
from src.preprocessing.tagger import TaggingRules, tag_issue  # noqa: E402


def summary_from_raw(raw: RawStore, repo: str, number: int, issue: dict, comments: list[dict]) -> dict:
    """runner.collect_issue_detail과 같은 요약을 저장된 원본으로 다시 만든다."""
    timeline = load_raw(raw, repo, "timeline", number) or []
    texts = [issue.get("body") or ""] + [c.get("body") or "" for c in comments]
    links = extract_links(timeline, repo, texts, issue_number=number)
    pulls = []
    for ref in links["pull_requests"][:3]:
        pr = load_raw(raw, repo, "pulls", ref["number"])
        if pr is not None:
            pulls.append(summarize_pull_request(pr, load_raw(raw, repo, "pull_files", ref["number"]) or []))
    return {"repo": repo, "number": number, "timeline_events": links["event_counts"], "linked_pull_requests": pulls,
            "closed_by_commit": links["closed_by_commit"]}


def diagnose(raw: RawStore, repo: str, tagging: dict) -> list[dict]:
    folder = raw.root / repo_slug(repo) / "issues"
    numbers = sorted(int(p.stem) for p in folder.glob("*.json")) if folder.exists() else []
    issues = {n: load_raw(raw, repo, "issues", n) for n in numbers}
    maintainers = infer_maintainers(issues.values())
    rules = TaggingRules.from_config(tagging, {})
    rows = []
    for number, issue in issues.items():
        comments = load_raw(raw, repo, "comments", number) or []
        record = to_shallow_record(repo, issue)
        tag = tag_issue(record, rules, {})
        summary = summary_from_raw(raw, repo, number, issue, comments)
        evidence = judge_evidence(summary, comments, (issue.get("user") or {}).get("login"), maintainers)
        human = [c for c in comments if not is_bot(c) and not is_automated_notice(c)]
        rows.append({
            "number": number, "state_reason": issue.get("state_reason"), "type": tag["issue_type"],
            "labels": ",".join(record["labels"])[:40], "closed_by": (issue.get("closed_by") or {}).get("login"),
            "comments": f"{len(human)}/{len(comments)}",
            "associations": ",".join(sorted({c.get("author_association") or "?" for c in human})),
            "signals": ",".join(sorted({s for c in human for s in comment_priority(c)["signals"]})),
            "events": ",".join(f"{k}:{v}" for k, v in sorted((summary["timeline_events"] or {}).items())),
            "prs": f"{len(summary['linked_pull_requests'])}(merged {sum(bool(p.get('merged')) for p in summary['linked_pull_requests'])})",
            "commit": summary["closed_by_commit"], "level": evidence["evidence_level"],
            "prefilter": prefilter(tag, record), "kind": resolution_kind(record, tag, evidence),
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="에이전트 코퍼스 판정 진단 (로컬 원본만 사용)")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--index", type=Path, default=ROOT / "agent_index")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    rows = diagnose(RawStore(args.index / "raw"), args.repo, load_yaml(ROOT / "configs/repositories.yaml").get("tagging", {}))
    if not rows:
        print(f"{args.index / 'raw'}에 {args.repo} 원본이 없습니다")
        return 1
    columns = ["number", "state_reason", "type", "closed_by", "comments", "associations", "signals", "prs", "commit",
               "level", "kind", "labels", "events"]
    print("\t".join(columns))
    for row in rows:
        print("\t".join(str(row[c]) for c in columns))
    print()
    for key in ("state_reason", "level", "kind", "associations", "closed_by"):
        print(f"{key}: " + ", ".join(f"{k} {v}" for k, v in Counter(str(r[key]) for r in rows).most_common(8)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
