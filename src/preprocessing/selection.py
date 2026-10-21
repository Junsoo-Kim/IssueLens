"""태깅 결과로 상세 수집 대상을 고르고 색인/평가/예비로 나눈다 (기획서 5.4절 2~4단계)."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

EXCLUDED_TYPES = {"invalid", "test", "task", "feature"}
MIN_SIGNALS = 2
MIN_CONTENT_CHARS = 100
# 한 컴포넌트가 상세 수집 대상을 독점하지 않도록 둔 상한 비율
MAX_COMPONENT_SHARE = 0.4
UNKNOWN_COMPONENT = "general"


@dataclass
class SplitTargets:
    deep: int
    index: int
    evaluation: int
    reserve: int

    def __post_init__(self):
        if self.index + self.evaluation + self.reserve != self.deep:
            raise ValueError(
                f"색인({self.index}) + 평가({self.evaluation}) + 예비({self.reserve})가 상세 수집({self.deep})과 같아야 합니다"
            )

    def scaled(self, available: int) -> "SplitTargets":
        """적격 이슈가 목표보다 적으면 같은 비율로 줄인다."""
        if available >= self.deep:
            return self
        evaluation = round(available * self.evaluation / self.deep)
        reserve = round(available * self.reserve / self.deep)
        return SplitTargets(available, available - evaluation - reserve, evaluation, reserve)


def reject_reasons(tag: dict, allow_questions: bool = False) -> list[str]:
    """기획서 5.11절 품질 필터. 빈 목록이면 적격이다."""
    reasons = []
    issue_type = tag["issue_type"]
    if issue_type in EXCLUDED_TYPES:
        reasons.append(f"type:{issue_type}")
    if issue_type == "question" and not (allow_questions and tag["comments"] > 0):
        reasons.append("type:question")
    if tag["state_reason"] == "duplicate":
        reasons.append("duplicate")
    if tag["stale"] and tag["state_reason"] == "not_planned":
        reasons.append("auto_closed")
    if tag["content_length"] < MIN_CONTENT_CHARS:
        reasons.append("body_too_short")
    if tag["signal_count"] < MIN_SIGNALS:
        reasons.append("insufficient_signals")
    return reasons


def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def rank_key(tag: dict) -> tuple:
    # 점수가 같으면 최근 종료 이슈를 우선한다
    return (-tag["quality_score"], -tag["signal_count"], -_timestamp(tag.get("closed_at")), tag["number"])


def pick_diverse(candidates: list[dict], count: int, max_share: float = MAX_COMPONENT_SHARE) -> list[dict]:
    """점수순으로 고르되 한 컴포넌트가 max_share를 넘지 않게 한다. 모자라면 상한 없이 채운다.

    컴포넌트를 알 수 없는 이슈(general)는 실제 컴포넌트가 아니므로 상한을 두지 않는다.
    """
    ranked = sorted(candidates, key=rank_key)
    cap = max(1, int(count * max_share))
    picked, per_component, skipped = [], Counter(), []
    for tag in ranked:
        if len(picked) >= count:
            break
        if tag["component"] != UNKNOWN_COMPONENT and per_component[tag["component"]] >= cap:
            skipped.append(tag)
            continue
        picked.append(tag)
        per_component[tag["component"]] += 1
    picked.extend(skipped[: count - len(picked)])
    return picked


def split_by_time(selected: list[dict], targets: SplitTargets) -> dict[str, list[dict]]:
    """가장 최근에 종료된 이슈를 평가용으로 두어 색인과 평가 사이의 시간 누수를 막는다.

    나머지에서 점수가 낮은 이슈를 예비로 둔다.
    """
    by_time = sorted(selected, key=lambda t: (_timestamp(t.get("closed_at")), t["number"]), reverse=True)
    evaluation = by_time[: targets.evaluation]
    rest = sorted(by_time[targets.evaluation:], key=rank_key)
    index = rest[: targets.index]
    reserve = rest[targets.index: targets.index + targets.reserve]
    return {"index": index, "evaluation": evaluation, "reserve": reserve}


def select_repository(tags: list[dict], targets: SplitTargets, allow_questions: bool = False) -> dict:
    eligible, rejected = [], []
    for tag in tags:
        reasons = reject_reasons(tag, allow_questions)
        if reasons:
            rejected.append({**tag, "reject_reasons": reasons})
        else:
            eligible.append(tag)

    effective = targets.scaled(len(eligible))
    # 질문 이슈는 버그 계열 이슈가 모자랄 때만 채운다
    primary = [t for t in eligible if t["issue_type"] != "question"]
    questions = [t for t in eligible if t["issue_type"] == "question"]
    selected = pick_diverse(primary, effective.deep)
    if len(selected) < effective.deep:
        selected += pick_diverse(questions, effective.deep - len(selected))
    splits = split_by_time(selected, effective)
    return {"eligible": eligible, "rejected": rejected, "selected": selected, "splits": splits, "targets": effective}
