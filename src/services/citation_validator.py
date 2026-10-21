"""답변 인용 번호 파싱과 유효성 검사 (요구사항 F2008).

답변 본문의 [근거 N]을 찾아, 실제 근거 목록에 있는 번호만 화면에 남긴다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CITATION = re.compile(r"\[근거\s*(\d+)\]")


@dataclass
class CitationCheck:
    cited: list[int] = field(default_factory=list)
    invalid: list[int] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.invalid

    @property
    def has_citations(self) -> bool:
        return bool(self.cited)


def check_citations(answer: str, evidence_numbers: set[int] | list[int]) -> CitationCheck:
    numbers = set(evidence_numbers)
    found = list(dict.fromkeys(int(n) for n in CITATION.findall(answer or "")))
    return CitationCheck(cited=[n for n in found if n in numbers], invalid=[n for n in found if n not in numbers])


def remove_citations(answer: str, numbers: list[int]) -> str:
    """존재하지 않는 근거 번호 표기를 지운다. 재생성 후에도 남았을 때만 쓴다."""
    drop = set(numbers)
    return CITATION.sub(lambda m: "" if int(m.group(1)) in drop else m.group(0), answer)
