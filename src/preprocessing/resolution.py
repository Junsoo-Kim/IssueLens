"""상세 수집 결과로 해결 근거를 판정하고, 근거를 보고 색인/평가/예비를 나눈다.

얕은 수집 단계의 has_resolution_signal은 근사치였다. 여기서는 실제 연결 PR과 댓글로 판정한다.

증거 수준
    strong  병합된 연결 PR 또는 커밋으로 종료됨
    medium  유지보수자 댓글에 해결 신호나 기술 정보(코드, 버전, 오류)가 있음
    weak    유지보수자 댓글, 해결 신호 댓글, 댓글의 공식 문서 링크 중 하나만 있음
    none    봇 댓글뿐이거나 근거가 없음

유지보수자는 author_association(OWNER/MEMBER/COLLABORATOR)만으로는 놓친다. 조직 멤버십을
공개하지 않으면 NONE/CONTRIBUTOR로 표시되기 때문이다. 그래서 저장소에서 이슈를 여러 번 닫은
계정도 유지보수자로 본다.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from src.collectors.comment_collector import comment_priority
from src.collectors.timeline_collector import extract_reference_urls

EVIDENCE_LEVELS = ("none", "weak", "medium", "strong")
LEVEL_RANK = {level: rank for rank, level in enumerate(EVIDENCE_LEVELS)}
MIN_CLOSURES_FOR_MAINTAINER = 3


def is_bot_user(user: dict | None) -> bool:
    user = user or {}
    return user.get("type") == "Bot" or (user.get("login") or "").endswith("[bot]")


def is_bot(comment: dict) -> bool:
    return is_bot_user(comment.get("user"))


def infer_maintainers(issues: Iterable[dict], min_closures: int = MIN_CLOSURES_FOR_MAINTAINER) -> set[str]:
    """이슈를 min_closures번 이상 닫은 사람 계정을 유지보수자로 본다."""
    closers = Counter(
        issue["closed_by"]["login"]
        for issue in issues
        if issue.get("closed_by") and issue["closed_by"].get("login") and not is_bot_user(issue["closed_by"])
    )
    return {login for login, count in closers.items() if count >= min_closures}


def judge_evidence(
    summary: dict,
    comments: list[dict],
    issue_author: str | None = None,
    maintainers: set[str] | frozenset[str] = frozenset(),
    docs_allowlist: Iterable[str] = (),
) -> dict:
    human = [c for c in comments if not is_bot(c)]
    maintainer_flags, detail_flags, resolution_flags = [], [], []
    for c in human:
        signals = set(comment_priority(c, issue_author)["signals"])
        is_maintainer = "maintainer" in signals or (c.get("user") or {}).get("login") in maintainers
        maintainer_flags.append(is_maintainer)
        detail_flags.append(is_maintainer and bool(signals & {"resolution_signal", "technical_detail"}))
        resolution_flags.append("resolution_signal" in signals)

    # 본문의 링크는 이슈 템플릿 기본 링크일 수 있어 댓글의 공식 문서 링크만 근거로 본다
    comment_docs = extract_reference_urls((c.get("body") or "" for c in human), summary.get("repo", ""), docs_allowlist)
    merged_prs = [pr["number"] for pr in summary.get("linked_pull_requests", []) if pr.get("merged")]
    events = summary.get("timeline_events") or {}

    flags = {
        "merged_pr": bool(merged_prs),
        "closed_by_commit": bool(summary.get("closed_by_commit")),
        "maintainer_detail": any(detail_flags),
        "maintainer_comment": any(maintainer_flags),
        "resolution_comment": any(resolution_flags),
        "official_doc": bool(comment_docs["official_docs"]),
    }
    if flags["merged_pr"] or flags["closed_by_commit"]:
        level = "strong"
    elif flags["maintainer_detail"]:
        level = "medium"
    elif flags["maintainer_comment"] or flags["resolution_comment"] or flags["official_doc"]:
        level = "weak"
    else:
        level = "none"

    return {
        "evidence_level": level,
        **flags,
        "merged_prs": merged_prs,
        "linked_prs": len(summary.get("linked_pull_requests", [])),
        # 사이드바로 연결된 PR은 REST 타임라인에 번호 없이 connected 이벤트만 남는다
        "connected_without_pr": bool(events.get("connected")) and not summary.get("linked_pull_requests"),
        "human_comments": len(human),
        "bot_comments": len(comments) - len(human),
    }


def meets(row: dict, min_level: str) -> bool:
    return LEVEL_RANK[row["evidence_level"]] >= LEVEL_RANK[min_level]


def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _strength_key(row: dict) -> tuple:
    return (-LEVEL_RANK[row["evidence_level"]], -row.get("quality_score", 0), -row.get("signal_count", 0),
            -_timestamp(row.get("closed_at")), row["number"])


@dataclass
class FinalTargets:
    index: int
    evaluation: int
    reserve: int


def assign_splits(
    rows: list[dict],
    targets: FinalTargets,
    min_level: str = "medium",
    eval_min_level: str = "weak",
) -> list[dict]:
    """한 저장소의 상세 수집 이슈에 final_split을 정한다.

    1. 평가: eval_min_level 이상인 이슈 중 가장 최근에 종료된 targets.evaluation건
    2. 색인: 평가 이슈 중 가장 이른 종료일보다 먼저 종료된 이슈만 후보로 두어 시간 누수를 막는다.
       min_level 이상을 근거가 강한 순으로 채우고, 모자라면 eval_min_level 이상으로 채워
       below_threshold로 표시한다. 근거가 없는(none) 이슈는 색인하지 않는다.
    3. 예비: 남은 색인 후보 중 eval_min_level 이상을 근거가 강한 순으로 targets.reserve건
    4. 나머지는 unused, 평가 기준일 이후 종료됐지만 평가에 뽑히지 않은 이슈는 after_cutoff
    """
    result = [{**row, "final_split": "unused", "flags": list(row.get("flags", []))} for row in rows]
    by_time = sorted(result, key=lambda r: (_timestamp(r.get("closed_at")), r["number"]), reverse=True)

    evaluation = [r for r in by_time if meets(r, eval_min_level)][: targets.evaluation]
    for row in evaluation:
        row["final_split"] = "evaluation"
        if not meets(row, min_level):
            row["flags"].append("weak_evaluation")
    cutoff = min((_timestamp(r.get("closed_at")) for r in evaluation), default=float("inf"))

    candidates = []
    for row in result:
        if row["final_split"] == "evaluation":
            continue
        if _timestamp(row.get("closed_at")) >= cutoff:
            row["final_split"] = "after_cutoff"
        else:
            candidates.append(row)

    ranked = sorted((r for r in candidates if meets(r, eval_min_level)), key=_strength_key)
    for row in ranked[: targets.index]:
        row["final_split"] = "index"
        if not meets(row, min_level):
            row["flags"].append("below_threshold")
    for row in ranked[targets.index: targets.index + targets.reserve]:
        row["final_split"] = "reserve"
    return result
