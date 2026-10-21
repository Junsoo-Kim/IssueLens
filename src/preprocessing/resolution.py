"""상세 수집 결과로 해결 근거를 다시 판정하고, 근거 없는 색인 이슈를 예비 이슈로 교체한다.

얕은 수집 단계의 has_resolution_signal은 근사치였다. 여기서는 실제 연결 PR과 댓글로 판정한다.

증거 수준
    strong  병합된 연결 PR 또는 커밋으로 종료됨
    medium  유지보수자 댓글에 해결 신호나 기술 정보(코드, 버전, 오류)가 있음
    weak    유지보수자 댓글, 해결 신호 댓글, 공식 문서 링크 중 하나만 있음
    none    봇 댓글뿐이거나 근거가 없음
"""
from __future__ import annotations

from src.collectors.comment_collector import comment_priority

EVIDENCE_LEVELS = ("none", "weak", "medium", "strong")
LEVEL_RANK = {level: rank for rank, level in enumerate(EVIDENCE_LEVELS)}


def is_bot(comment: dict) -> bool:
    user = comment.get("user") or {}
    return user.get("type") == "Bot" or (user.get("login") or "").endswith("[bot]")


def judge_evidence(summary: dict, comments: list[dict], issue_author: str | None = None) -> dict:
    human = [c for c in comments if not is_bot(c)]
    signals = [set(comment_priority(c, issue_author)["signals"]) for c in human]
    merged_prs = [pr["number"] for pr in summary.get("linked_pull_requests", []) if pr.get("merged")]

    flags = {
        "merged_pr": bool(merged_prs),
        "closed_by_commit": bool(summary.get("closed_by_commit")),
        "maintainer_detail": any("maintainer" in s and s & {"resolution_signal", "technical_detail"} for s in signals),
        "maintainer_comment": any("maintainer" in s for s in signals),
        "resolution_comment": any("resolution_signal" in s for s in signals),
        "official_doc": bool((summary.get("reference_urls") or {}).get("official_docs")),
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
        "human_comments": len(human),
        "bot_comments": len(comments) - len(human),
    }


def meets(row: dict, min_level: str) -> bool:
    return LEVEL_RANK[row["evidence_level"]] >= LEVEL_RANK[min_level]


def finalize_split(rows: list[dict], min_level: str = "medium") -> list[dict]:
    """한 저장소의 선별 행(split, rank, evidence_level 포함)에 final_split을 정한다.

    - 색인 이슈가 기준 미달이면 기준을 넘는 예비 이슈로 교체하고, 원래 이슈는 dropped로 둔다.
      근거가 가장 약한 이슈부터, 근거가 가장 강한 예비 이슈로 교체한다.
    - 예비 이슈가 모자라면 기준 미달 이슈를 색인에 남기고 below_threshold로 표시한다.
    - 평가 이슈는 교체하지 않는다. 예비 이슈는 평가 이슈보다 먼저 종료돼 시간 순서가 깨지기 때문이다.
      기준 미달이면 weak_evaluation으로 표시만 한다.
    """
    result = [{**row, "final_split": row["split"], "flags": []} for row in rows]
    reserves = sorted(
        (r for r in result if r["split"] == "reserve" and meets(r, min_level)),
        key=lambda r: (-LEVEL_RANK[r["evidence_level"]], r["rank"]),
    )
    failing_index = sorted(
        (r for r in result if r["split"] == "index" and not meets(r, min_level)),
        key=lambda r: (LEVEL_RANK[r["evidence_level"]], -r["rank"]),
    )
    for row in failing_index:
        if reserves:
            replacement = reserves.pop(0)
            replacement["final_split"] = "index"
            replacement["replaces"] = row["issue_id"]
            row["final_split"] = "dropped"
            row["replaced_by"] = replacement["issue_id"]
        else:
            row["flags"].append("below_threshold")
    for row in result:
        if row["split"] == "evaluation" and not meets(row, min_level):
            row["flags"].append("weak_evaluation")
    return result
